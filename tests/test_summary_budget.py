"""Anonymous K3 task dimensions exercise the current paid-call boundary.

K3-04 resumed after interruption; these faults are controlled perturbations, not
claims about that task's actual provider behavior or model quality.
"""
import json
import time
from types import SimpleNamespace

import pytest

from mindie_knowledge.loop.budget import BudgetExceeded
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.transcript_capture import _admit_summary
from mindie_knowledge.materials.summarizer import SummaryLedger, SummaryConflict, outcome
from test_material_summarizer import request, returned


@pytest.fixture
def ledger(store):
    with store._write_txn():
        result = SummaryLedger(store.db)
    return result


def engine_for(store, limits):
    engine = Engine(store)
    engine._settings = lambda: SimpleNamespace(as_dict=lambda: dict(summary_budget=limits))
    return engine


def test_prepared_work_costs_nothing_but_interrupted_call_consumes_budget(store, ledger):
    req = request('K3-04')
    with store._write_txn():
        attempt = ledger.prepare(req)
        _admit_summary(engine_for(store, dict(calls_per_hour=1)))
        ledger.claim(attempt['attempt_id'])
        ledger.recover()
        with pytest.raises(BudgetExceeded) as caught:
            _admit_summary(engine_for(store, dict(calls_per_hour=1)))
    assert caught.value.retry_at > time.time()
    assert ledger.get(attempt['attempt_id'])['status'] == 'outcome_unknown'
    assert ledger.usage_totals()['unknown_calls'] == 1
    with store._write_txn(), pytest.raises(SummaryConflict):
        ledger.claim(attempt['attempt_id'])


def test_known_and_unknown_input_usage_both_limit_new_calls(store, ledger):
    req = request('K3-04')
    with store._write_txn():
        attempt = ledger.prepare(req)
        ledger.claim(attempt['attempt_id'])
        ledger.record(attempt['attempt_id'], returned(req))
        with pytest.raises(BudgetExceeded):
            _admit_summary(engine_for(store, dict(input_tokens_per_hour=32768)))
        # Passage of the rolling window permits new material, never an old attempt.
        store.db.execute('UPDATE material_summary_attempts SET created=?', (time.time() - 3602,))
        _admit_summary(engine_for(store, dict(input_tokens_per_hour=32768)))
        with pytest.raises(SummaryConflict):
            ledger.claim(attempt['attempt_id'])


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
        second = ledger.prepare(request('K3-04'))
        _admit_summary(engine_for(store, {}))
        ledger.claim(second['attempt_id'])
    assert ledger.get(first['attempt_id'])['status'] == 'failed'
    assert ledger.get(second['attempt_id'])['status'] == 'invoking'


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
