import json
import socket
import threading
import time
from types import SimpleNamespace

from mindie_knowledge.loop import cli, handoff, process, service_host
from mindie_knowledge.loop.locks import StartLock
from mindie_knowledge.loop.lifecycle import config_fingerprint
from tests.test_capture_handoff import _event
from tests.test_review_followup import _ready


def test_silent_listener_leaves_capture_durable_and_returns_unknown(tmp_path):
    path, admission, *_ = _ready(tmp_path)
    config = cli.config_at(path)
    stop = threading.Event()
    connected = threading.Event()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(3)
    def silent():
        client = None
        try:
            client, _ = listener.accept()
            connected.set()
            stop.wait(3)
        finally:
            if client is not None:
                client.close()
    thread = threading.Thread(target=silent)
    thread.start()
    connection = cli.connection_path(config)
    connection.write_text(json.dumps(dict(
        url="http://127.0.0.1:" + str(listener.getsockname()[1]),
        token="synthetic", domain=config["domain"],
        config_fingerprint=config_fingerprint(path, config))))
    try:
        with StartLock(connection.with_name("consumer.lock")):
            before = time.monotonic()
            result = cli.capture_hook(path, _event(admission))
            elapsed = time.monotonic() - before
        assert connected.is_set()
        assert elapsed < 1.5
        assert result["capture_id"] and result["stage"] == "accepted-local"
        assert result["runtime"] == "unknown" and result["wake"] == "not-requested"
        from mindie_knowledge.loop.store import Store
        store = Store(config["root"], config["domain"])
        try:
            assert store.capture_row(result["capture_id"])["status"] == "queued"
        finally:
            store.close()
    finally:
        stop.set()
        thread.join(timeout=4)
        listener.close()


def test_linux_short_wake_only_submits_existing_detached_manager_request(tmp_path, monkeypatch):
    path, *_ = _ready(tmp_path)
    calls = []
    monkeypatch.setattr(handoff, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(handoff, "_probe", lambda *a, **k: "absent")
    def manager_must_not_block(*args):
        raise AssertionError("short Hook must not wait for manager readiness")
    monkeypatch.setattr(service_host, "platform_backend", lambda: SimpleNamespace(
        request_start=manager_must_not_block))
    monkeypatch.setattr(process, "spawn_service", lambda argv: (
        calls.append(argv), SimpleNamespace(pid=123))[1])
    result = handoff.request_wake(path, session_id="manual-A")
    assert result["wake"] == "requested"
    assert len(calls) == 1 and calls[0][3] == "wake"
