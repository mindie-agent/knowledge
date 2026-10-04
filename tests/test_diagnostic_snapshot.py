"""Diagnostic safety checks; controlled local mechanisms, not native host acceptance."""
import json
from contextlib import closing
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
    from lane_support import parser_path
    data = dict(root=str(tmp_path / "state"), domain="test",
                admission_path=str(tmp_path / "admission.sqlite3"),
                transcript_adapter=str(parser_path('codex')),
                redactor_executable=str(tmp_path / 'configured-scanner'), **extra)
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
    data["summary_command"] = [sys.executable, "different-installation"]
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


def test_scoped_records_and_failure_counts_are_diagnostic_only(tmp_path):
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
        # This read-only projection fixture writes only small metadata. No send
        # candidate or remote operation is involved.
        with store._write_txn():
            for ident in [*(f'own-{i}' for i in range(6)), 'foreign', 'vote-only']:
                store.db.execute("INSERT INTO outbox(batch_id,revision,batch,status,detail,created,updated) "
                                 "VALUES(?,?,?,'failed',?,?,?)", (ident, 'a' * 64,
                                 json.dumps({'entry_refs': [ref] if ident.startswith('own-') else []}),
                                 'ValueError: private exception', time.time(), time.time()))
        with store.db:
            store.db.execute("INSERT INTO votes VALUES(?,?,?,?,?,?,?,?)", (opaque, "not-owned", "r", "down", "private body", 1, "vote-only", time.time()))
    finally:
        store.close()
    for _ in range(3):
        admission.finish("task-A", lease["token"], False)
    dbpath = tmp_path / "state/test/state-v4.sqlite3"
    before = dbpath.read_bytes()
    result = diagnostics.snapshot(path, session="task-A")
    assert before == dbpath.read_bytes()
    assert [row["id"] for row in result["captures"]] == [own["id"]]
    assert result["captures"][0]["category"] == "deadline"
    assert result["captures"][0]["exit_code"] == 124
    assert len(result["contributions"]) == 5
    assert "vote-only" in {row["batch_id"] for row in result["contributions"]}
    assert "foreign" not in {row["batch_id"] for row in result["contributions"]}
    # Failure counts are observability only: the binding stays active.
    assert result["admission"]["status"] == "active" and result["admission"]["failures"] == 3
    raw = json.dumps(result)
    assert all(secret not in raw for secret in ("private body", "private exception", "private pause reason", lease["token"], other["token"], "task-B", ref))
    assert admission.active_lease("task-A")["failures"] == 3
    assert diagnostics.snapshot(path)["captures"] == diagnostics.snapshot(path)["contributions"] == []


def test_actual_database_lock_is_bounded(tmp_path):
    path, data = config(tmp_path)
    store = Store(data["root"], "test"); store.close()
    db = sqlite3.connect(tmp_path / "state/test/state-v4.sqlite3", isolation_level=None)
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


def test_summary_failure_is_scoped_visible_and_read_only(tmp_path):
    path, data = config(tmp_path)
    store = Store(data['root'], 'test')
    try:
        with store._write_txn():
            for session in ('task-A', 'task-B'):
                store.db.execute('INSERT INTO transcript_tasks '
                    '(task_key,entry_id,capture_id,body_digest,summary_status,summary_detail,updated,summary_due,authorization) '
                    'VALUES(?,?,?,?,?,?,?,?,?)',
                    (session, session, '', 'digest', 'failed', 'configuration: summary worker is not configured', 1, 0,
                     json.dumps({'session': session})))
        result = diagnostics.snapshot(path, session='task-A')
        assert result['summaries'] == [{'status': 'failed', 'category': 'configuration'}]
        assert any('publication is blocked' in hint for hint in result['hints'])
        assert 'task-B' not in json.dumps(result)
        assert store.db.execute('SELECT COUNT(*) FROM transcript_tasks').fetchone()[0] == 2
        with store._write_txn():
            for index in range(6):
                store.db.execute('INSERT INTO transcript_tasks '
                    '(task_key,entry_id,capture_id,body_digest,summary_status,summary_detail,updated,summary_due,authorization) '
                    'VALUES(?,?,?,?,?,?,?,?,?)',
                    (f'complete-{index}', f'complete-{index}', '', 'digest', 'complete', '', 2+index, 0,
                     json.dumps({'session': 'task-A'})))
        newer = diagnostics.snapshot(path, session='task-A')
        assert newer['summaries'][0] == {'status': 'failed', 'category': 'configuration'}
        assert any('publication is blocked' in hint for hint in newer['hints'])
    finally:
        store.close()


def test_missing_material_batch_diagnostic_is_scoped_and_read_only(tmp_path):
    path, data = config(tmp_path)
    with closing(Store(data['root'], 'test')) as store:
        with store._write_txn():
            for key, session in [('missing', 'task-A'), ('valid', 'task-A'), ('foreign', 'task-B')]:
                store.db.execute('INSERT INTO transcript_tasks VALUES(?,?,?,?,?,?,?,?,?)',
                    (key, key, '', 'digest', 'pending', '', 1, 0, json.dumps({'session': session})))
            store.db.execute('INSERT INTO material_batches VALUES(?,?,?,?,?,?,?,?)',
                             ('batch', 'valid', 'valid', '[]', 'pending', '', '{}', 1))
        changes = store.db.total_changes
        result = diagnostics.snapshot(path, session='task-A')
        assert store.db.total_changes == changes
        assert [row['status'] for row in result['summaries']] == ['missing', 'pending']
        missing = result['summaries'][0]
        assert missing['stage'] == 'index-queue' and missing['reason'] == 'missing-material-batch'
        assert 'batch job is missing' in missing['message']
        assert any('publication is blocked' in hint for hint in result['hints'])
        assert 'task-B' not in json.dumps(result)
        assert store.db.execute('SELECT COUNT(*) FROM material_batches').fetchone()[0] == 1
        assert store.db.execute("SELECT COUNT(*) FROM transcript_tasks WHERE summary_status='pending'").fetchone()[0] == 3


def test_summary_ledger_scopes_usage_and_unknown_without_loading_private_outputs(tmp_path, monkeypatch):
    from mindie_knowledge.materials.summarizer import SummaryLedger
    from test_material_summarizer import request, returned
    path, data = config(tmp_path)
    admission = Admission(data['admission_path'])
    admission.activate('task-A', project_root=str(tmp_path), root_session='root-A')
    with closing(Store(data['root'], 'test')) as store:
        with store._write_txn():
            ledger = SummaryLedger(store.db)
            for case, authorization in [('K3-01', {'session': 'task-A'}),
                                        ('K3-02', {'session': 'child-A', 'root_session': session_key('root-A')}),
                                        ('K3-03', {'session': 'task-B'})]:
                req = request(case)
                store.db.execute('INSERT INTO transcript_tasks '
                    '(task_key,entry_id,capture_id,body_digest,summary_status,summary_detail,updated,summary_due,authorization) '
                    'VALUES(?,?,?,?,?,?,?,?,?)',
                    (case, req['task_id'], '', 'digest', 'outcome_unknown' if case == 'K3-02' else 'complete', '', 1, 0,
                     json.dumps(authorization)))
                attempt = ledger.prepare(req)
                ledger.claim(attempt['attempt_id'])
                if case == 'K3-02':
                    ledger.uncertain(attempt['attempt_id'], 'deadline')
                else:
                    response = returned(req)
                    response['raw_result'] = 'private-model-output-canary'
                    ledger.record(attempt['attempt_id'], response)
            expected = dict(scope='task', model_calls=2, unknown_calls=1,
                            input_tokens=120, cached_input_tokens=20, output_tokens=30)
        queries = []
        read = diagnostics._ro
        def traced(file):
            db = read(file)
            db.set_trace_callback(queries.append)
            return db
        monkeypatch.setattr(diagnostics, '_ro', traced)
        before = store.db.total_changes
        result = diagnostics.snapshot(path, session='task-A')
        assert store.db.total_changes == before
        assert result['summary_usage'] == expected
        assert [x['status'] for x in result['summary_attempts']] == ['outcome_unknown', 'returned']
        assert result['summary_attempts'][0]['category'] == 'deadline'
        assert result['summaries'][0]['status'] == 'outcome_unknown'
        assert 'private-model-output-canary' not in json.dumps(result)
        assert 'task-B' not in json.dumps(result)
        assert any('billing remains uncertain' in x for x in result['hints'])
        receipt_queries = [query for query in queries if 'FROM material_summary_attempts' in query]
        assert receipt_queries and all('json_extract(m.response' in query for query in receipt_queries)
        assert all('SELECT *' not in query for query in receipt_queries)
        assert diagnostics.snapshot(path)['summary_usage'] is None
        # Observing an unknown paid call must not prepare, claim, or complete it.
        assert ledger.usage_totals()['model_calls'] == 3
        assert store.db.execute("SELECT count(*) FROM material_summary_attempts WHERE status='outcome_unknown'").fetchone()[0] == 1


@pytest.mark.parametrize('receipt', ['{broken', '{"model_calls":1,"billing_status":"reported","usage_known":true,"usage":{"input_tokens":-1}}'])
def test_corrupt_summary_usage_is_unavailable_not_zero(tmp_path, receipt):
    from mindie_knowledge.materials.summarizer import SummaryLedger
    from test_material_summarizer import request
    path, data = config(tmp_path)
    with closing(Store(data['root'], 'test')) as store:
        with store._write_txn():
            ledger = SummaryLedger(store.db)
            req = request('K3-01')
            attempt = ledger.prepare(req)
            ledger.claim(attempt['attempt_id'])
            store.db.execute('INSERT INTO transcript_tasks '
                '(task_key,entry_id,capture_id,body_digest,summary_status,summary_detail,updated,summary_due,authorization) '
                'VALUES(?,?,?,?,?,?,?,?,?)',
                ('one', req['task_id'], '', 'digest', 'failed', '', 1, 0, json.dumps({'session': 'task-A'})))
            store.db.execute("UPDATE material_summary_attempts SET status='complete',response=?", (receipt,))
        result = diagnostics.snapshot(path, session='task-A')
        assert result['store'] == dict(status='unavailable', error_class='ValueError')
        assert result['summary_usage'] is None
        assert store.db.execute('SELECT response FROM material_summary_attempts').fetchone()[0] == receipt


def test_summary_duplicate_block_failure_has_safe_stage_and_reason(tmp_path):
    from mindie_knowledge.materials.summarizer import SummaryLedger, outcome, failure_detail
    from test_material_summarizer import request
    path, data = config(tmp_path)
    with closing(Store(data['root'], 'test')) as store:
        with store._write_txn():
            ledger = SummaryLedger(store.db)
            req = request('K3-02')
            attempt = ledger.prepare(req)
            ledger.claim(attempt['attempt_id'])
            response = outcome(req, status='failed', error='invalid_result',
                error_reason='block_count_mismatch', model_calls=1, usage_known=True,
                usage=dict(input_tokens=100, cached_input_tokens=10, output_tokens=20),
                raw_result='private duplicate model output')
            ledger.record(attempt['attempt_id'], response)
            store.db.execute('INSERT INTO transcript_tasks '
                '(task_key,entry_id,capture_id,body_digest,summary_status,summary_detail,updated,summary_due,authorization) '
                'VALUES(?,?,?,?,?,?,?,?,?)',
                ('one', req['task_id'], '', 'digest', 'failed', json.dumps(failure_detail(response)), 1, 0, json.dumps({'session': 'task-A'})))
        result = diagnostics.snapshot(path, session='task-A')
        projected = result['summary_attempts'][0]
        assert projected['category'] == 'invalid_result'
        assert projected['reason'] == 'block_count_mismatch'
        assert projected['stage'] == 'index-validation'
        assert projected['message'] == 'returned block count differs from the admitted batch'
        assert result['summaries'][0]['reason'] == 'block_count_mismatch'
        assert result['summaries'][0]['stage'] == 'index-validation'
        assert result['summary_usage']['model_calls'] == 1
        assert result['summary_usage']['output_tokens'] == 20
        assert 'private duplicate model output' not in json.dumps(result)
