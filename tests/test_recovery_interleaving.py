"""Real process/SQLite interleavings for the current material index worker.

Source capture commits complete redacted files before indexing. Uncertain
model calls never restart automatically; a late loser cannot undo a winner.
"""
import json
from pathlib import Path
import sys
import threading
import time

import pytest

from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store
from mindie_knowledge.loop.transcript_capture import summarize_due
from mindie_knowledge.materials.summarizer import SummaryLedger
from material_worker_fixture import command as summary_command
from test_history_import import contribute
from test_recovery_git import _import_pipeline


def _wait(path, thread, timeout=15):
    end = time.monotonic() + timeout
    while not path.exists() and thread.is_alive() and time.monotonic() < end:
        time.sleep(.02)
    assert path.exists(), f"worker did not reach {path.name}"


def _due(engine):
    engine.last_activity = 0
    with engine.store._write_txn():
        engine.store.db.execute('UPDATE transcript_tasks SET summary_due=0')
    summarize_due(engine)


def _sibling(engine, command):
    return Engine(engine.store, settings_path=engine.settings_path, admission=engine.admission,
                  transcript_adapter=engine.transcript, redactor_executable=engine.redactor_executable,
                  summary_command=command)


def test_interrupted_index_worker_keeps_material_and_does_not_repeat_call(tmp_path):
    engine, source, _ = _import_pipeline(tmp_path)
    store = engine.store
    started, release, calls = (tmp_path / name for name in ('started', 'release', 'calls'))
    engine.summary_command = summary_command(started=started, release=release, calls=calls)
    work = None
    try:
        contribute((engine, source))
        before = store.drafts_changed()[0]
        cursor = dict(store.db.execute('SELECT * FROM material_streams').fetchone())
        work = threading.Thread(target=_due, args=(engine,))
        work.start()
        _wait(started, work)
        engine._summary_cancel.set()  # real bounded_run terminates its owned worker process
        work.join(timeout=10)
        assert not work.is_alive()
        with store.lock:
            ledger = SummaryLedger(store.db)
            attempt = dict(store.db.execute('SELECT * FROM material_summary_attempts').fetchone())
        assert attempt['status'] == 'outcome_unknown'
        assert ledger.usage_totals()['model_calls'] == 1
        assert ledger.usage_totals()['unknown_calls'] == 1
        assert store.get(store.ref(before['entry_id']))['content'] == before['content']
        assert dict(store.db.execute('SELECT * FROM material_streams').fetchone()) == cursor
        # Restart does not convert an uncertain paid call into an automatic retry.
        restarted = _sibling(engine, summary_command(calls=calls))
        for _ in range(2):
            _due(restarted)
        assert calls.read_text() == 'x'
        assert store.db.execute('SELECT summary_status FROM transcript_tasks').fetchone()[0] == 'outcome_unknown'
    finally:
        release.touch()
        engine._summary_cancel.set()
        if work:
            work.join(timeout=5)
        store.close()


def test_late_summary_claim_loser_cannot_overwrite_completed_winner(tmp_path):
    engine, source, _ = _import_pipeline(tmp_path)
    store = engine.store
    identity_wait, identity_go, calls = (tmp_path / name for name in ('identity-wait', 'identity-go', 'calls'))
    fixture = Path(__file__).with_name('material_worker_fixture.py')
    wrapper = tmp_path / 'slow-identity.py'
    wrapper.write_text(
        'import json,runpy,sys,time\nfrom pathlib import Path\n'
        'if "--identity" in sys.argv:\n'
        f'    Path({str(identity_wait)!r}).touch()\n'
        f'    while not Path({str(identity_go)!r}).exists(): time.sleep(.02)\n'
        f'sys.argv[0] = {str(fixture)!r}\n'
        f'runpy.run_path({str(fixture)!r}, run_name="__main__")\n', encoding='utf-8')
    slow = summary_command(calls=calls)
    slow[1] = str(wrapper)
    loser = _sibling(engine, slow)
    winner = _sibling(engine, summary_command(calls=calls))
    work = None
    try:
        contribute((engine, source))
        work = threading.Thread(target=_due, args=(loser,))
        work.start()
        _wait(identity_wait, work)
        _due(winner)
        assert store.db.execute('SELECT status FROM material_summary_attempts').fetchone()[0] == 'complete'
        identity_go.touch()
        work.join(timeout=10)
        assert not work.is_alive()
        assert calls.read_text() == 'x'  # the loser never invokes a second model
        assert store.db.execute('SELECT status FROM material_summary_attempts').fetchone()[0] == 'complete'
        assert store.db.execute('SELECT status FROM material_batches').fetchone()[0] == 'complete'
        assert store.db.execute('SELECT summary_status FROM transcript_tasks').fetchone()[0] == 'complete'
    finally:
        identity_go.touch()
        loser._summary_cancel.set()
        if work:
            work.join(timeout=5)
        store.close()


def test_shutdown_after_saved_response_resumes_only_local_application(tmp_path, monkeypatch):
    engine, source, _ = _import_pipeline(tmp_path)
    store = engine.store
    calls = tmp_path / 'calls'
    engine.summary_command = summary_command(calls=calls)
    original_record = SummaryLedger.record
    def stop_after_record(ledger, attempt_id, response):
        original_record(ledger, attempt_id, response)
        engine.stop.set()
    monkeypatch.setattr(SummaryLedger, 'record', stop_after_record)
    try:
        contribute((engine, source))
        _due(engine)
        assert calls.read_text() == 'x'
        assert store.db.execute('SELECT status FROM material_summary_attempts').fetchone()[0] == 'returned'
        assert store.db.execute('SELECT status FROM material_batches').fetchone()[0] == 'pending'
        assert store.db.execute('SELECT summary_status FROM transcript_tasks').fetchone()[0] == 'pending'
        restarted = _sibling(engine, summary_command(calls=calls))
        _due(restarted)
        assert calls.read_text() == 'x'
        assert store.db.execute('SELECT status FROM material_summary_attempts').fetchone()[0] == 'complete'
        assert store.db.execute('SELECT summary_status FROM transcript_tasks').fetchone()[0] == 'complete'
    finally:
        store.close()
