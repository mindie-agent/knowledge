"""Service startup with an actual configured transcript adapter module."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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


def test_prepare_observation_timeout_never_starts_a_second_owner(tmp_path, monkeypatch):
    from mindie_knowledge.loop import cli, process, service_host
    from mindie_knowledge.loop.lifecycle import config_fingerprint
    from mindie_knowledge.loop.locks import StartLock, lock_held

    release = threading.Event()
    requested = threading.Event()

    class SlowStatus(BaseHTTPRequestHandler):
        def do_POST(self):
            requested.set()
            release.wait(5)
            self.close_connection = True

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowStatus)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = _configuration(tmp_path / "data")
    path = tmp_path / "engine.json"
    path.write_text(json.dumps(config))
    connection = cli.connection_path(config)
    connection.parent.mkdir(parents=True)
    connection.write_text(json.dumps(dict(
        domain="test", token="test", url=f"http://127.0.0.1:{server.server_port}",
        config_fingerprint=config_fingerprint(path, config))))

    def forbidden(*_args, **_kwargs):
        pytest.fail("a slow observed owner must not cause a new startup")

    monkeypatch.setattr(process, "spawn_service", forbidden)
    monkeypatch.setattr(service_host, "platform_backend", forbidden)
    try:
        with StartLock(connection.with_name("consumer.lock")):
            started = time.monotonic()
            with pytest.raises(RuntimeError, match="unknown"):
                cli.prepare_service(path, probe_timeout=.1)
            assert time.monotonic() - started < 2
            assert requested.is_set()
            assert lock_held(connection.with_name("consumer.lock"))
            assert lock_held(connection.with_name("start.lock")) is False
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("timeout", [False, 0, -1, float("nan"), float("inf"), "30"])
def test_prepare_rejects_invalid_probe_timeout_before_configuration(tmp_path, timeout):
    from mindie_knowledge.loop.cli import prepare_service

    with pytest.raises(ValueError, match="probe_timeout"):
        prepare_service(tmp_path / "missing.json", probe_timeout=timeout)
    assert list(tmp_path.iterdir()) == []


def test_service_start_waits_for_a_transient_lock_observer(tmp_path):
    from mindie_knowledge.loop.cli import connection_path, rpc
    from mindie_knowledge.loop.locks import StartLock

    config = _configuration(tmp_path / 'data')
    path = tmp_path / 'engine.json'
    path.write_text(json.dumps(config), encoding='utf-8')
    evidence = tmp_path / 'startup-stages'
    diagnostics = tmp_path / 'service.log'
    connection_file = connection_path(config)
    consumer_file = connection_file.with_name('consumer.lock')

    def wait_for(target, stage, deadline):
        while not target.exists() and time.monotonic() < deadline:
            assert process.poll() is None, diagnostics.read_text(encoding='utf-8')
            time.sleep(.01)
        assert target.exists(), f"{stage} not observed:\n{diagnostics.read_text(encoding='utf-8')}"

    process = None
    with diagnostics.open('w', encoding='utf-8') as log:
        try:
            with StartLock(consumer_file):
                process = subprocess.Popen(
                    [sys.executable, str(Path(__file__).with_name('startup_observer.py')),
                     str(path), str(consumer_file), str(evidence)],
                    stdout=log, stderr=log,
                )
                wait_for(evidence / 'consumer_busy', 'exact consumer contention',
                         time.monotonic() + 5)
                assert not (evidence / 'consumer_acquired').exists()
                assert not connection_file.exists()
                with pytest.raises(subprocess.TimeoutExpired):
                    process.wait(timeout=.15)
            deadline = time.monotonic() + 5
            wait_for(evidence / 'consumer_acquired', 'consumer acquisition after release', deadline)
            wait_for(connection_file, 'service readiness', deadline)
            connection = json.loads(connection_file.read_text())
            assert rpc(connection, 'status', timeout=1)['worker_alive']
            assert rpc(connection, 'stop_if_idle', timeout=1)['idle']
            assert process.wait(timeout=3) == 0, diagnostics.read_text(encoding='utf-8')
        except BaseException:
            print(diagnostics.read_text(encoding='utf-8'), file=sys.stderr)
            raise
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


def test_cold_wake_waits_for_healthy_initialization_past_the_old_deadline(tmp_path, monkeypatch):
    """A silent initializer may take longer than the removed five-second cap."""
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
            "import time,runpy; time.sleep(6.2); runpy.run_module('mindie_knowledge.loop.cli', run_name='__main__')",
            *command[3:]], **options)
        owned.append(child)
        return child
    monkeypatch.setattr(process, 'spawn_service', delayed)
    try:
        handoff.run_wake(config)
        assert len(owned) == 1
        assert owned[0].poll() is None, 'starter killed healthy initialization based on elapsed time'
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
        "import builtins, faulthandler, runpy, socket\n"
        "original_import = builtins.__import__\n"
        "def without_retrieval(name, *args, **kwargs):\n"
        "    if name == 'reme' or name.startswith('reme.'):\n"
        "        raise RuntimeError('startup must not initialize retrieval')\n"
        "    return original_import(name, *args, **kwargs)\n"
        "builtins.__import__ = without_retrieval\n"
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
            assert status["background_errors"] == {}
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
    assert service.http.slots is not None and service.http.slot_wait == 0
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
    engine.operations.resume()
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


def test_service_start_fails_on_real_exit_without_restarting(tmp_path, monkeypatch):
    from mindie_knowledge.loop import cli, process
    config = tmp_path / 'engine.json'
    config.write_text(json.dumps(_configuration(tmp_path / 'data')), encoding='utf-8')
    spawn = process.spawn_service
    children = []
    def failed_start(_argv, **kwargs):
        child = spawn([sys.executable, '-c', 'raise SystemExit(7)'], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(process, 'spawn_service', failed_start)
    with pytest.raises(RuntimeError, match='exited during startup; no retry'):
        cli.ensure_service(config)
    assert len(children) == 1 and children[0].returncode == 7
