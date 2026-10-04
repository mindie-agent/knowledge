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


@pytest.mark.parametrize('fault', ['scanner', 'authority', 'apply', 'stopping'])
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
    assert attempt['error'] == dict(scanner='scanner_unavailable', authority='authority_unavailable',
                                    apply='apply_failed', stopping=None)[fault]
    assert store.drafts_changed()[0]['content'] == before
    if fault == 'stopping':
        assert store.db.execute('SELECT summary_status FROM transcript_tasks').fetchone()[0] == 'pending'
        engine.stop.clear()
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
