"""Diagnostic safety checks; controlled local mechanisms, not native host acceptance."""
import json
import os
import sqlite3
import sys
import time

import pytest

from mindie_knowledge.loop import cli, diagnostics
from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.process import MaintenanceCancelled, bounded_run
from mindie_knowledge.loop.store import Store, session_key


def config(tmp_path, **extra):
    path = tmp_path / "engine.json"
    data = dict(root=str(tmp_path / "state"), domain="test",
                admission_path=str(tmp_path / "admission.sqlite3"), **extra)
    path.write_text(json.dumps(data))
    return path, data


def test_missing_does_not_initialize_and_operator_cli(tmp_path, capsys):
    path = tmp_path / "missing.json"
    assert cli.main(["diagnostic-status", "--config", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["configuration"]["status"] == "absent"
    assert not list(tmp_path.iterdir())
    path, _ = config(tmp_path)
    before = list(tmp_path.iterdir())
    result = diagnostics.snapshot(path)
    assert result["store"]["status"] == "absent"
    assert result["captures"] == result["contributions"] == []
    assert list(tmp_path.iterdir()) == before


@pytest.mark.parametrize("bad", [None, [], {}, 1, True])
def test_connection_url_type_is_structured(tmp_path, bad):
    path, data = config(tmp_path)
    folder = tmp_path / "state" / "test"
    folder.mkdir(parents=True)
    (folder / "connection.json").write_text(json.dumps(dict(url=bad, token="test", domain="test")))
    assert diagnostics.snapshot(path)["service"] == dict(status="unavailable", stage="probe", error_class="ValueError")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX named pipes")
def test_metadata_fifo_never_waits_for_writer_and_regular_symlink_works(tmp_path):
    path = tmp_path / "engine.json"
    os.mkfifo(path)
    started = time.monotonic()
    assert diagnostics.snapshot(path)["configuration"]["status"] == "unavailable"
    assert time.monotonic() - started < 1
    path.unlink()
    path, _ = config(tmp_path)
    target = tmp_path / "real.json"
    path.rename(target)
    path.symlink_to(target)
    assert diagnostics.snapshot(path)["configuration"]["status"] == "ok"
    folder = tmp_path / "state" / "test"
    folder.mkdir(parents=True)
    for name, key in (("connection.json", "service"), ("latest-startup.json", "startup")):
        os.mkfifo(folder / name)
        started = time.monotonic()
        assert diagnostics.snapshot(path)[key]["status"] == "unavailable"
        assert time.monotonic() - started < 1


def test_snapshot_parses_config_once_and_startup_record_is_bound(tmp_path, monkeypatch):
    path, data = config(tmp_path)
    real = diagnostics._bytes
    reads = []

    def bounded_read(source, limit=diagnostics.MAX_JSON):
        if source == path:
            reads.append(source)
        return real(source, limit)

    monkeypatch.setattr(diagnostics, "_bytes", bounded_read)
    diagnostics.record_startup_failure(path, data, "engine", RuntimeError("private exception"))
    assert diagnostics.snapshot(path)["startup"]["error_class"] == "RuntimeError"
    assert len(reads) == 1
    data["agent_command"] = [sys.executable, "different-installation"]
    path.write_text(json.dumps(data))
    assert diagnostics.snapshot(path)["startup"]["status"] == "absent"
    diagnostics.record_startup_failure(path, data, "service", ValueError("private exception"))
    diagnostics.clear_startup_failure(path, data)
    assert diagnostics.snapshot(path)["startup"]["status"] == "absent"


@pytest.mark.parametrize("field,value", [("stage", []), ("error_class", {}), ("cause", [])])
def test_malformed_startup_projection_is_bounded(tmp_path, field, value):
    path, data = config(tmp_path)
    diagnostics.record_startup_failure(path, data, "engine", RuntimeError("private exception"))
    marker = tmp_path / "state/test/latest-startup.json"
    record = json.loads(marker.read_text()); record[field] = value
    marker.write_text(json.dumps(record))
    result = diagnostics.snapshot(path)["startup"]
    assert result["status"] in {"failed", "unavailable"}
    assert "private exception" not in json.dumps(result)


def test_scoped_records_batches_pause_and_readonly(tmp_path):
    path, data = config(tmp_path)
    admission = Admission(data["admission_path"])
    lease = admission.activate("task-A", project_root=str(tmp_path), root_session="root-A")
    other = admission.activate("task-B", project_root=str(tmp_path))
    store = Store(data["root"], "test")
    try:
        own = store.add_capture(root_session=session_key("root-A"), session="child-A", turn="one", transcript=None, summary="private body")
        foreign = store.add_capture(root_session=session_key("task-B"), session="task-B", turn="two", transcript=None, summary="private body")
        store.mark_capture(own["id"], "failed", "RuntimeError: maintenance agent exited 124; category=deadline; elapsed=120.100s")
        store.mark_capture(foreign["id"], "failed", "ValueError: private exception")
        opaque = store.opaque_for(session_key("root-A"))
        doc = store.create_draft(kind="experience", title="Local fixture", summary="Diagnostic fixture.", content="private body", owner=opaque)
        ref = store.ref(doc["entry_id"], doc["revision"])
        for i in range(6):
            store.create_batch(batch_id=f"own-{i}", revision="r", batch={"entry_refs": [ref]}, entry_ids=[doc["entry_id"]], vote_keys=[])
            store.mark_batch(f"own-{i}", "failed", detail="ValueError: private exception")
        store.create_batch(batch_id="foreign", revision="r", batch={"entry_refs": []}, entry_ids=[], vote_keys=[])
        store.create_batch(batch_id="vote-only", revision="r", batch={"entry_refs": []}, entry_ids=[], vote_keys=[])
        with store.db:
            store.db.execute("INSERT INTO votes VALUES(?,?,?,?,?,?,?,?)", (opaque, "not-owned", "r", "down", "private body", 1, "vote-only", time.time()))
            store.db.execute("INSERT OR REPLACE INTO state VALUES('maintenance_paused','private pause reason')")
    finally:
        store.close()
    for _ in range(3):
        admission.finish("task-A", lease["token"], False)
    dbpath = tmp_path / "state/test/store-v3.sqlite3"
    before = dbpath.read_bytes()
    result = diagnostics.snapshot(path, session="task-A")
    assert before == dbpath.read_bytes()
    assert [row["id"] for row in result["captures"]] == [own["id"]]
    assert result["captures"][0]["category"] == "deadline"
    assert result["captures"][0]["exit_code"] == 124
    assert len(result["contributions"]) == 5
    assert "vote-only" in {row["batch_id"] for row in result["contributions"]}
    assert "foreign" not in {row["batch_id"] for row in result["contributions"]}
    assert result["admission"]["status"] == "paused" and result["admission"]["failures"] == 3
    assert result["maintenance"]["paused"] is True
    raw = json.dumps(result)
    assert all(secret not in raw for secret in ("private body", "private exception", "private pause reason", lease["token"], other["token"], "task-B", ref))
    assert not admission.active_lease("task-A")
    assert diagnostics.snapshot(path)["captures"] == diagnostics.snapshot(path)["contributions"] == []


def test_actual_database_lock_is_bounded(tmp_path):
    path, data = config(tmp_path)
    store = Store(data["root"], "test"); store.close()
    db = sqlite3.connect(tmp_path / "state/test/store-v3.sqlite3", isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        assert diagnostics.snapshot(path)["store"] == dict(status="unavailable", error_class="OperationalError")
        assert time.monotonic() - started < 1
    finally:
        db.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-tree mechanism; Windows needs real acceptance")
def test_connection_write_failure_cancels_real_child_and_workers(tmp_path, monkeypatch):
    """Inject only worker workload; use actual process group and filesystem failure."""
    path, data = config(tmp_path)
    pidfile = tmp_path / "child-pids.json"
    child = tmp_path / "child.py"
    child.write_text("import json,os,subprocess,sys,time\nfrom pathlib import Path\n"
                     "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                     "Path(sys.argv[1]).write_text(json.dumps([os.getpid(),p.pid]))\ntime.sleep(60)\n")
    engines = []

    class BusyEngine(Engine):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            engines.append(self)

        def run(self):
            try:
                bounded_run([sys.executable, str(child), str(pidfile)], "", timeout=10, max_output=1024, cancel=self._cancel)
            except MaintenanceCancelled:
                pass

        def start(self):
            # The normal off path pre-cancels capture. This controlled workload
            # deliberately starts active work to exercise startup cleanup.
            self.thread.start()
            self.outbox_thread = __import__("threading").Thread(target=lambda: self.stop.wait(10), daemon=True)
            self.outbox_thread.start()
            deadline = time.monotonic() + 4
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert pidfile.exists(), "controlled child failed to start"

    folder = tmp_path / "state/test/connection.json"
    folder.mkdir(parents=True)  # Real atomic file replacement must fail.
    monkeypatch.setattr(cli, "Engine", BusyEngine)
    with pytest.raises(OSError):
        cli._serve(path, data)
    assert not engines[0].thread.is_alive() and not engines[0].outbox_thread.is_alive()
    pids = json.loads(pidfile.read_text())
    deadline = time.monotonic() + 3
    alive = []
    while time.monotonic() < deadline:
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0); alive.append(pid)
            except ProcessLookupError:
                pass
        if not alive:
            break
        time.sleep(.02)
    assert not alive, "owned controlled child or grandchild survived startup failure"
    assert diagnostics.snapshot(path)["startup"]["stage"] == "service"
