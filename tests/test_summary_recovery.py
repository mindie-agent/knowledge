"""Returned K3-derived index outputs resume locally, even without the worker.

The fault placements are synthetic protocol checks. They neither read the real
K3 history nor claim a historical failure or model-quality result.
"""
import json
import time

import pytest

from mindie_knowledge.loop.engine import GateFault
from mindie_knowledge.loop.transcript_capture import summarize_due
from mindie_knowledge.materials.summarizer import SummaryLedger
from material_worker_fixture import command
from test_public_transcript import pipeline, scanner, append, process


@pytest.mark.parametrize('fault', ['scanner', 'scanner-cancel', 'authority', 'apply', 'stopping'])
def test_returned_response_recovers_without_current_worker(pipeline, tmp_path, monkeypatch, fault):
    engine, store, path = pipeline
    calls = tmp_path / 'native-invocations'
    engine.summary_command = command(calls=calls, title='Resumed precision evidence')
    append(path, 'Anonymous K3-04 continuation: precision acceptance remains unresolved.')
    process(engine, path, 'returned-result-boundary')
    before = store.drafts_changed()[0]['content']
    original_scanner = engine.redactor_executable
    engine.last_activity = 0
    with store._write_txn():
        store.db.execute('UPDATE transcript_tasks SET summary_due=0')
    with monkeypatch.context() as faults:
        if fault == 'scanner':
            engine.redactor_executable = str(tmp_path / 'missing-scanner')
        elif fault == 'scanner-cancel':
            from mindie_knowledge.loop.process import MaintenanceCancelled
            def cancel_scanner(_text, **options):
                assert options['cancel'] is engine._summary_cancel
                engine.stop.set()
                engine._summary_cancel.set()
                raise MaintenanceCancelled('scanner cancelled by owner')
            faults.setattr('mindie_knowledge.loop.transcript_capture.redact', cancel_scanner)
        elif fault == 'authority':
            original_gate, gate_calls = engine._revalidate, []
            def gate(row):
                gate_calls.append(1)
                if len(gate_calls) > 1:
                    raise GateFault('synthetic unavailable authority after model return')
                return original_gate(row)
            faults.setattr(engine, '_revalidate', gate)
        elif fault == 'stopping':
            original_live, live_calls = engine._gate_live, []
            def stopping():
                live_calls.append(1)
                if len(live_calls) > 1:
                    engine.stop.set()
                return original_live()
            faults.setattr(engine, '_gate_live', stopping)
        else:
            original_apply = store.apply_material_indexes
            def apply_then_fail(**kwargs):
                original_apply(**kwargs)
                raise OSError('synthetic local transaction failure')
            faults.setattr(store, 'apply_material_indexes', apply_then_fail)
        summarize_due(engine)
    engine.redactor_executable = original_scanner
    assert calls.read_text() == 'x'
    attempt = dict(store.db.execute('SELECT * FROM material_summary_attempts').fetchone())
    assert attempt['status'] == 'returned'
    response = json.loads(attempt['response'])
    assert response['raw_result'] and response['usage']['input_tokens'] == 120
    assert attempt['error'] == {'scanner': 'scanner_unavailable', 'scanner-cancel': None,
                               'authority': 'authority_unavailable', 'apply': 'apply_failed', 'stopping': None}[fault]
    assert store.drafts_changed()[0]['content'] == before
    if fault in {'stopping', 'scanner-cancel'}:
        assert store.db.execute('SELECT summary_status FROM transcript_tasks').fetchone()[0] == 'pending'
        engine.stop.clear()
        engine._summary_cancel.clear()
    # A removed/upgraded executable cannot obstruct recovery of returned output.
    engine.summary_command = [str(tmp_path / 'worker-no-longer-installed')]
    with store._write_txn():
        store.db.execute('UPDATE transcript_tasks SET summary_due=0')
    summarize_due(engine)
    complete = dict(store.db.execute('SELECT * FROM material_summary_attempts').fetchone())
    assert complete['status'] == 'complete'
    assert 'raw_result' not in json.loads(complete['response'])
    assert calls.read_text() == 'x'
    assert SummaryLedger(store.db).usage_totals()['model_calls'] == 1
    assert store.drafts_changed()[0]['title'] == 'Resumed precision evidence'
    assert store.drafts_changed()[0]['content'] == before


def test_known_summary_failure_is_pending_for_agent_without_replay(pipeline, tmp_path, monkeypatch):
    from mindie_knowledge.loop import agent_diagnostics
    monkeypatch.setenv('MINDIE_DIAGNOSTICS_ROOT', str(tmp_path / 'diagnostics'))
    engine, store, path = pipeline
    calls = tmp_path / 'native-invocations'
    engine.summary_command = command(calls=calls, fail=True)
    append(path, 'Synthetic input whose worker returns a known invalid result.')
    process(engine, path, 'known-failure')
    engine.last_activity = 0
    with store._write_txn():
        store.db.execute('UPDATE transcript_tasks SET summary_due=0')
    summarize_due(engine)
    assert calls.read_text() == 'x'
    projection = agent_diagnostics.pending()
    assert any(item['code'] == 'result_contract_invalid' for item in projection['items'])
    assert store.db.execute('SELECT summary_status FROM transcript_tasks').fetchone()[0] == 'failed'
    summarize_due(engine)
    assert calls.read_text() == 'x'
    assert agent_diagnostics.pending() == projection  # No later capability call, no ACK.


@pytest.mark.parametrize('when', ['before-spawn', 'while-waiting'])
def test_identity_stage_obeys_owner_cancellation_without_execution_deadline(pipeline, tmp_path, monkeypatch, when):
    import sys
    import threading
    from mindie_knowledge.loop import transcript_capture
    engine, store, path = pipeline
    started = tmp_path / 'identity-started'
    worker = tmp_path / 'identity_worker.py'
    worker.write_text("import pathlib, sys, time\n"
                      "assert sys.argv[1] == '--identity'\n"
                      f"pathlib.Path({str(started)!r}).touch()\n"
                      "time.sleep(30)\n")
    engine.summary_command = [sys.executable, str(worker)]
    append(path, 'Synthetic summary whose identity helper is cancelled by its owner.')
    process(engine, path, 'identity-cancellation')
    engine.last_activity = 0
    with store._write_txn():
        store.db.execute('UPDATE transcript_tasks SET summary_due=0')
    observed, errors = [], []
    original = transcript_capture.bounded_run
    def run(command, payload, **options):
        observed.append(options.get('cancel') is engine._summary_cancel)
        assert options.get('timeout') is None
        if when == 'before-spawn':
            engine.stop.set()
            engine._summary_cancel.set()
        return original(command, payload, **options)
    monkeypatch.setattr(transcript_capture, 'bounded_run', run)
    def cancel_waiting():
        until = time.monotonic() + 5  # Test watchdog, never a runtime deadline.
        while not started.exists() and time.monotonic() < until:
            time.sleep(.01)
        if not started.exists():
            errors.append('identity helper did not start')
        engine.stop.set()
        engine._summary_cancel.set()
    watcher = threading.Thread(target=cancel_waiting, daemon=True) if when == 'while-waiting' else None
    if watcher:
        watcher.start()
    summarize_due(engine)
    if watcher:
        watcher.join(timeout=5)
        assert not watcher.is_alive()
    assert observed == [True] and not errors
    assert started.exists() == (when == 'while-waiting')
    assert store.db.execute('SELECT count(*) FROM material_summary_attempts').fetchone()[0] == 0
    assert store.db.execute('SELECT summary_status FROM transcript_tasks').fetchone()[0] == 'pending'
