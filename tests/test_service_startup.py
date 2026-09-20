"""Service startup with an actual configured transcript adapter module."""

import json
import shutil
import subprocess
import sys
import time

import transcript_double


def test_serve_starts_with_actual_configured_parser(tmp_path):
    adapter = tmp_path / "adapter_parser.py"
    shutil.copy(transcript_double.__file__, adapter)
    config = tmp_path / "engine.json"
    root = tmp_path / "root"
    config.write_text(json.dumps(dict(
        root=str(root), domain="test",
        admission_path=str(tmp_path / "admission.sqlite3"),
        transcript_adapter=str(adapter),
    )))
    diagnostics = tmp_path / "service.log"
    bootstrap = (
        "import faulthandler, runpy; "
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
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)


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
