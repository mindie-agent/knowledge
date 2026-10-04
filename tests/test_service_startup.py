"""Service startup with an actual configured transcript adapter module."""

import json
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest

import transcript_double


def _configuration(root):
    return dict(root=str(root), domain="test", capture_mode="public-transcript",
                transcript_adapter=transcript_double.__file__,
                redactor_executable=str(root.parent / "gitleaks"))


def test_service_start_waits_for_a_transient_lock_observer(tmp_path):
    from mindie_knowledge.loop.cli import connection_path, rpc
    from mindie_knowledge.loop.locks import StartLock

    config = _configuration(tmp_path / 'data')
    path = tmp_path / 'engine.json'
    path.write_text(json.dumps(config), encoding='utf-8')
    attempted = tmp_path / 'attempted'
    bootstrap = (
        'import runpy; from pathlib import Path; from mindie_knowledge.loop import locks\n'
        'real = locks._lock_file_nb\n'
        'def observed(fd):\n'
        f'    Path({str(attempted)!r}).write_text("attempted")\n'
        '    return real(fd)\n'
        'locks._lock_file_nb = observed\n'
        'runpy.run_module("mindie_knowledge.loop.cli", run_name="__main__")\n'
    )
    process = None
    try:
        with StartLock(connection_path(config).with_name('consumer.lock')):
            process = subprocess.Popen([sys.executable, '-c', bootstrap, 'serve', '--config', str(path)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 5
            while not attempted.exists() and time.monotonic() < deadline:
                assert process.poll() is None
                time.sleep(.01)
            assert attempted.exists()
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=.15)
        deadline = time.monotonic() + 5
        while not connection_path(config).exists() and time.monotonic() < deadline:
            assert process.poll() is None
            time.sleep(.01)
        connection = json.loads(connection_path(config).read_text())
        assert rpc(connection, 'status', timeout=1)['worker_alive']
        assert rpc(connection, 'stop_if_idle', timeout=1)['idle']
        assert process.wait(timeout=3) == 0
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=3)


def test_cold_wake_spawn_failure_is_visible_and_releases_ownership(tmp_path, monkeypatch):
    from mindie_knowledge.loop import handoff, process
    from mindie_knowledge.loop.locks import lock_held

    config = tmp_path / 'engine.json'
    root = tmp_path / 'data'
    config.write_text(json.dumps(_configuration(root)), encoding='utf-8')
    event = 'a' * 64

    def denied(*args, **kwargs):
        raise PermissionError('private-path-and-provider-detail')

    monkeypatch.setattr(process, 'spawn_service', denied)
    assert handoff.run_wake(config, event=event) == 0
    diagnostic = (root / 'test/latest-delivery.json').read_text(encoding='utf-8')
    payload = json.loads(diagnostic)
    assert payload['cause'] == 'wake-failed'
    assert payload['stage'] == 'wake' and payload['event'] == event
    assert 'private-path-and-provider-detail' not in diagnostic
    assert lock_held(root / 'test/start.lock') is False


def test_cold_wake_uses_its_existing_startup_budget(tmp_path, monkeypatch):
    """A real 2.4 s interpreter startup used to be killed after 3 x .5 s."""
    from mindie_knowledge.loop import handoff, process
    from mindie_knowledge.loop.cli import connect
    from mindie_knowledge.loop.transport import rpc
    config = tmp_path / 'engine.json'
    value = _configuration(tmp_path / 'data')
    config.write_text(json.dumps(value), encoding='utf-8')
    original = process.spawn_service
    owned = []
    def delayed(command, **options):
        child = original([sys.executable, '-c',
            "import time,runpy; time.sleep(2.4); runpy.run_module('mindie_knowledge.loop.cli', run_name='__main__')",
            *command[3:]], **options)
        owned.append(child)
        return child
    monkeypatch.setattr(process, 'spawn_service', delayed)
    try:
        handoff.run_wake(config)
        assert len(owned) == 1
        assert owned[0].poll() is None, 'starter killed its owned service before the configured deadline'
        assert rpc(connect(value), 'status', timeout=1)['worker_alive']
        assert rpc(connect(value), 'stop_if_idle', timeout=1)['idle']
        owned[0].wait(timeout=5)
    finally:
        for child in owned:
            if child.poll() is None:
                process.terminate_tree(child)
            child.wait(timeout=3)


def test_serve_starts_with_actual_configured_parser(tmp_path):
    adapter = tmp_path / "adapter_parser.py"
    shutil.copy(transcript_double.__file__, adapter)
    config = tmp_path / "engine.json"
    root = tmp_path / "root"
    config.write_text(json.dumps(dict(
        root=str(root), domain="test",
        admission_path=str(tmp_path / "admission.sqlite3"),
        transcript_adapter=str(adapter), redactor_executable=str(tmp_path / "gitleaks"),
        capture_mode="public-transcript",
    )))
    diagnostics = tmp_path / "service.log"
    bootstrap = (
        "import faulthandler, runpy, socket; "
        "socket.getfqdn = lambda *a, **k: (_ for _ in ()).throw("
        "RuntimeError('loopback bind must not reverse-DNS')); "
        "faulthandler.dump_traceback_later(8, repeat=False); "
        "runpy.run_module('mindie_knowledge.loop.cli', run_name='__main__')"
    )
    with diagnostics.open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-c", bootstrap, "serve", "--config", str(config)],
            stdout=log, stderr=log, text=True,
        )
        try:
            connection_path = root / "test" / "connection.json"
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not connection_path.is_file():
                if process.poll() is not None:
                    raise AssertionError("service exited: " + diagnostics.read_text())
                time.sleep(0.1)
            assert connection_path.is_file(), "service never became ready: " + diagnostics.read_text()
            from mindie_knowledge.loop.transport import rpc

            connection = json.loads(connection_path.read_text())
            status = rpc(connection, "status", timeout=5)
            assert status["domain"] == "test"
            result = rpc(connection, "stop_if_idle", timeout=5)
            assert result["idle"] is True
            assert result["status"] == "stopping"
            process.wait(timeout=5)
            assert process.returncode == 0
            from mindie_knowledge.loop.handoff import _probe
            from mindie_knowledge.loop.locks import lock_held

            # The persistent connection file survives shutdown. A short TCP
            # timeout on Windows must not turn a released consumer into an
            # ambiguous live service and prevent the next startup.
            assert connection_path.is_file()
            assert lock_held(connection_path.with_name("consumer.lock")) is False
            assert _probe(json.loads(config.read_text()), .01) == "absent"
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)


def test_lifetime_lock_observation_is_conservative_and_preserves_metadata(tmp_path):
    from mindie_knowledge.loop.locks import StartLock, lock_held

    path = tmp_path / "consumer.lock"
    assert lock_held(path) is None
    assert not path.exists()
    with StartLock(path):
        stamp = path.stat().st_mtime_ns
        assert lock_held(path) is True
        assert path.stat().st_mtime_ns == stamp
    metadata = path.read_bytes()
    assert lock_held(path) is False
    assert path.read_bytes() == metadata


def test_loopback_bind_skips_reverse_dns(tmp_path, monkeypatch):
    """Loopback service must bind without socket.getfqdn reverse-DNS."""
    from mindie_knowledge.loop.engine import Engine
    from mindie_knowledge.loop.store import Store
    from mindie_knowledge.loop.transport import Service, rpc

    def forbid_fqdn(*args, **kwargs):
        raise AssertionError("loopback bind must not reverse-DNS")

    monkeypatch.setattr(socket, "getfqdn", forbid_fqdn)
    store = Store(tmp_path / "store", "test")
    engine = Engine(store)
    service = Service(engine, connection_path=tmp_path / "connection.json")
    assert service.http.server_name == "127.0.0.1"
    assert service.http.server_port == int(service.connection["url"].rsplit(":", 1)[-1])
    assert service.http.slots is not None and service.http.slot_wait > 0
    thread = threading.Thread(target=service.serve, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (tmp_path / "connection.json").is_file():
            time.sleep(0.05)
        assert (tmp_path / "connection.json").is_file()
        connection = json.loads((tmp_path / "connection.json").read_text())
        assert connection["url"].startswith("http://127.0.0.1:")
        status = rpc(connection, "status", timeout=5)
        assert status["domain"] == "test"
    finally:
        service.close()
        thread.join(timeout=5)
    store.close()


def test_stop_if_idle_refuses_in_flight_and_queued_work(tmp_path):
    from mindie_knowledge.loop.engine import Engine
    from mindie_knowledge.loop.store import Store

    store = Store(tmp_path / "store", "test")
    engine = Engine(store)
    # Durable unknown receipts and idle grants are not activity.
    with store._write_txn():
        store.db.execute(
            "INSERT INTO outbox VALUES(?,?,?,?,?,NULL,NULL,1.0,NULL,1.0,0,NULL,NULL)",
            ("batch-u", "r" * 64, "{}", "unknown", ""),
        )
    idle = engine.stop_if_idle()
    assert idle["idle"] is True
    engine._frozen = False
    engine.stop.clear()

    assert engine.begin_work() is True
    busy = engine.stop_if_idle()
    assert busy["idle"] is False and busy["status"] == "busy"
    engine.end_work()

    captured = store.add_capture(
        root_session="rh", session="s", turn="queued-1",
        transcript=None, summary="queued work",
    )
    engine.queue.put(captured["id"])
    busy = engine.stop_if_idle()
    assert busy["idle"] is False
    assert busy["queued_captures"] >= 1
    store.close()


def test_stop_if_idle_rpc_refuses_in_flight_outbox(tmp_path):
    import threading
    import time

    from mindie_knowledge.loop.engine import Engine
    from mindie_knowledge.loop.store import Store
    from mindie_knowledge.loop.transport import Service, rpc

    store = Store(tmp_path / "store", "test")
    engine = Engine(store)
    service = Service(engine, connection_path=tmp_path / "connection.json")
    thread = threading.Thread(target=service.serve, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (tmp_path / "connection.json").is_file():
            time.sleep(0.05)
        connection = json.loads((tmp_path / "connection.json").read_text())
        assert engine.begin_work() is True  # in-flight publication/RPC
        result = rpc(connection, "stop_if_idle", timeout=5)
        assert result["idle"] is False
        engine.end_work()
        result = rpc(connection, "stop_if_idle", timeout=5)
        assert result["idle"] is True
    finally:
        service.close()
        thread.join(timeout=5)
    store.close()
