"""Anonymous K3 task dimensions exercise the current paid-call boundary.

K3-04 resumed after interruption; these faults are controlled perturbations, not
claims about that task's actual provider behavior or model quality.
"""
import json
import time

import pytest

from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.index_queue import IndexQueue, SummaryWorkerPaused
from mindie_knowledge.materials.summarizer import SummaryLedger, SummaryConflict, outcome, make_request
from test_material_summarizer import request, returned
from test_public_transcript import pipeline, scanner, append


@pytest.fixture
def ledger(store):
    with store._write_txn():
        result = SummaryLedger(store.db)
    return result


def test_interrupted_call_retains_unknown_usage_and_cannot_be_claimed_twice(store, ledger):
    req = request('K3-04')
    with store._write_txn():
        attempt = ledger.prepare(req)
        ledger.claim(attempt['attempt_id'])
        ledger.recover()
    assert ledger.get(attempt['attempt_id'])['status'] == 'outcome_unknown'
    assert ledger.usage_totals()['unknown_calls'] == 1
    with store._write_txn(), pytest.raises(SummaryConflict):
        ledger.claim(attempt['attempt_id'])


def test_more_than_former_hourly_limit_is_not_a_model_admission_rule(store, ledger):
    req = request('K3-04')
    with store._write_txn():
        for number in range(40):
            current = make_request(task_id=f'{number:064x}', body_version=req['body_version'],
                                   blocks=req['blocks'], prior_navigation=None, identity=req['policy_identity'])
            attempt = IndexQueue(store).claim(dict(status='pending', batch_id='fixture'), current)
            ledger.record(attempt['attempt_id'], returned(current))
        with pytest.raises(SummaryConflict):
            ledger.claim(attempt['attempt_id'])
    assert ledger.usage_totals()['model_calls'] == 40
    assert 'summary_budget' not in Engine(store).status()


def test_returned_result_survives_restart_with_no_model_replay(store, ledger, monkeypatch):
    req = request('K3-04')
    with store._write_txn():
        attempt = ledger.prepare(req)
        ledger.claim(attempt['attempt_id'])
        response = returned(req)
        ledger.record(attempt['attempt_id'], response)
        ledger.local_failure(attempt['attempt_id'], 'scanner_unavailable')
    engine = Engine(store)
    monkeypatch.setattr(engine, 'revoke_stale', lambda: None)
    monkeypatch.setattr(engine.thread, 'start', lambda: None)
    monkeypatch.setattr(engine.outbox_thread, 'start', lambda: None)
    monkeypatch.setattr(engine.summary_thread, 'start', lambda: None)
    engine.start()
    resumed = ledger.get(attempt['attempt_id'])
    assert resumed['status'] == 'returned'
    assert json.loads(resumed['response']) == response
    assert resumed['error'] == 'scanner_unavailable'
    assert ledger.usage_totals()['model_calls'] == 1


def test_failure_in_one_task_does_not_pause_another(store, ledger):
    failed = request('K3-03')
    with store._write_txn():
        first = ledger.prepare(failed)
        ledger.claim(first['attempt_id'])
        ledger.record(first['attempt_id'], outcome(failed, status='failed', error='invalid_result',
                                                   model_calls=1, usage_known=False))
        second = IndexQueue(store).claim(dict(status='pending', batch_id='fixture'), request('K3-04'))
    assert ledger.get(first['attempt_id'])['status'] == 'failed'
    assert ledger.get(second['attempt_id'])['status'] == 'invoking'


def test_shared_fault_stops_new_calls_but_repair_retry_retains_receipts(store, ledger):
    failed = request('K3-03')
    queue = IndexQueue(store)
    batch = dict(status='pending', batch_id='fixture')
    with store._write_txn():
        first = queue.claim(batch, failed)
        receipt = outcome(failed, status='failed', error='invalid_result', model_calls=1,
                          usage_known=True, usage=dict(input_tokens=120, cached_input_tokens=0, output_tokens=30),
                          error_reason='native_output_unreadable')
        ledger.record(first['attempt_id'], receipt)
    with pytest.raises(SummaryWorkerPaused) as caught:
        queue.claim(batch, request('K3-04'))
    assert caught.value.fault['attempt_id'] == first['attempt_id']
    assert ledger.usage_totals()['model_calls'] == 1
    repaired = queue.claim(dict(batch, status='retry-requested'), failed)
    with store._write_txn():
        ledger.record(repaired['attempt_id'], returned(failed))
    next_attempt = queue.claim(batch, request('K3-04'))
    assert next_attempt['status'] == 'invoking'
    retained = json.loads(ledger.get(first['attempt_id'])['response'])
    assert retained == {key: value for key, value in receipt.items() if key not in {'raw_result', 'result'}}


def test_unknown_retry_does_not_clear_fault_and_policy_repair_unblocks_new_work(store, ledger):
    failed = request('K3-03')
    queue = IndexQueue(store)
    batch = dict(status='pending', batch_id='fixture')
    with store._write_txn():
        first = queue.claim(batch, failed)
        ledger.record(first['attempt_id'], outcome(failed, status='failed', error='configuration',
                                                   model_calls=0, error_reason='native_start_failed'))
    retry = queue.claim(dict(batch, status='retry-requested'), failed)
    with store._write_txn():
        ledger.uncertain(retry['attempt_id'])
    with pytest.raises(SummaryWorkerPaused):
        queue.claim(batch, request('K3-04'))
    repaired = request('K3-04')
    from mindie_knowledge.materials.summarizer import policy_identity
    repaired = make_request(task_id=repaired['task_id'], body_version=repaired['body_version'],
                            blocks=repaired['blocks'], prior_navigation=None,
                            identity=policy_identity(model='fixture-small-model', effort='low',
                                                     implementation={'fixture': 'repaired-v2'}))
    assert queue.claim(batch, repaired)['status'] == 'invoking'


def test_shared_fault_parks_later_material_and_explicit_repair_resumes_same_queue(pipeline, tmp_path):
    from test_public_transcript import process
    from material_worker_fixture import command
    from mindie_knowledge.loop.transcript_capture import summarize_due
    engine, store, path = pipeline
    calls = tmp_path / 'calls'
    append(path, 'First complete public observation.')
    process(engine, path, 'first')
    append(path, 'Later correction must survive the first indexing fault.')
    process(engine, path, 'second')
    engine.summary_command = command(calls=calls, fail=True, error_reason='native_output_unreadable')
    engine.last_activity = 0
    summarize_due(engine)
    summarize_due(engine)
    assert calls.read_text() == 'x'
    assert engine.status()['summary_attempts'] == {'failed': 1}
    task = dict(store.db.execute('SELECT * FROM transcript_tasks').fetchone())
    paused = json.loads(task['summary_detail'])
    assert paused['status'] == 'worker-paused'
    assert paused['reason'] == 'native_output_unreadable'
    assert paused['attempt_id'] == store.db.execute('SELECT attempt_id FROM material_summary_attempts').fetchone()[0]
    assert engine.index_queue.due() is None  # Only a free dependency recheck is postponed.
    engine.summary_command = command(calls=calls)
    engine.index_queue.retry(task['entry_id'])
    summarize_due(engine)
    summarize_due(engine)
    assert calls.read_text() == 'xxx'  # Explicit repair plus the distinct later batch.
    assert engine.status()['summary_attempts'] == {'complete': 2, 'failed': 1}
    assert dict(store.db.execute('SELECT * FROM transcript_tasks').fetchone())['summary_status'] == 'complete'
    assert 'Later correction must survive' in store.drafts_changed()[0]['content']


def test_legacy_configuration_is_rejected_and_public_pipeline_is_default(tmp_path, store):
    from mindie_knowledge.loop.cli import validate_config
    import transcript_double
    config = dict(root=str(tmp_path), domain='test', transcript_adapter=transcript_double.__file__,
                  redactor_executable=str(tmp_path / 'gitleaks'))
    assert validate_config(config) == config
    assert Engine(store).capture_mode == 'public-transcript'
    with pytest.raises(ValueError, match='agent_command was removed'):
        validate_config(dict(config, agent_command=['false']))
    with pytest.raises(ValueError, match='only public-transcript'):
        validate_config(dict(config, capture_mode='organize'))
    with pytest.raises(ValueError, match='only public-transcript'):
        Engine(store, capture_mode='organize')


def test_service_without_summary_command_fails_due_work_and_becomes_idle(pipeline):
    engine, store, path = pipeline
    append(path, "Anonymous K3-04 resumed material requires an index before publication.")
    engine.start()
    try:
        engine.capture(session_id="manual-A", turn_id="missing-summary-worker", transcript_path=str(path))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            with store.lock:
                row = store.db.execute("SELECT summary_status,summary_detail FROM transcript_tasks").fetchone()
            if row and row["summary_status"] == "failed":
                break
            time.sleep(.05)
        assert row and row["summary_status"] == "failed"
        assert row["summary_detail"] == "index-summary failed: configuration: summary worker is not configured"
        assert engine.status()["summary_usage"]["model_calls"] == 0
        from mindie_knowledge.loop.export import build_batch
        assert build_batch(store, settings=engine._settings()) is None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            idle = engine.stop_if_idle()
            if idle["idle"]:
                break
            time.sleep(.05)
        assert idle["idle"] and idle["due_summary"] is False
    finally:
        engine.shutdown()
