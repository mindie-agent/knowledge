"""Authorized public material capture and one shared incremental index queue."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .process import bounded_run
from .dfx import failure as diagnostic_failure
from .store import canonical, digest
from .index_queue import SummaryWorkerPaused
from .transcript_redaction import ScannerUnavailable, redact
from ..materials.ingest import prepare_increment
from ..materials.summarizer import (
    SummaryLedger, MAX_RESPONSE_BYTES, make_request,
    partition_blocks, validate_identity, validate_result, validate_outcome, batch_identity, failure_detail, outcome,
)

MODE = 'public-transcript'
SUMMARY_SETTLE_SECONDS = 3


def pending_header(_text):
    return 'Task experience awaiting indexing', 'Reference material; indexing is pending.'


def capture(engine, row, text, region, *, cancel):
    from .engine import CursorConflict
    store, inc = engine.store, region.get('inc') or {}
    if not inc or inc.get('coverage') or inc.get('discarded_records'):
        raise ValueError('public transcript is incomplete; source cursor not advanced')
    stream_key = digest(['activated-transcript/2', store.domain, row['session'],
                         row['generation'], row['activation_epoch']])
    entry_id = digest(['task-experience/1', store.domain, stream_key])
    prior = store.material_stream(stream_key)
    prepared = prepare_increment(task_id=entry_id, text=text, start=inc['start'], end=inc['end'],
                                 source_digest=inc['digest'],
                                 scanner_state=json.loads(prior['redaction_state']) if prior else {},
                                 executable=engine.redactor_executable, key=store.redaction_key(),
                                 private_paths=(row['scope'], str(Path.home())), cancel=cancel)
    engine._revalidate(row)
    with store._write_txn():
        engine._revalidate(row)
        reserved = region['reserve'](inc['start'], inc['end'], inc['digest'], status='succeeded')
        if reserved is None:
            raise CursorConflict('cursor changed before material commit')
        doc = store.commit_material_increment(stream_key=stream_key, entry_id=entry_id,
                                               prepared=prepared, start=inc['start'], end=inc['end'],
                                               source_identity=inc.get('identity', ''), authorization=row,
                                               owner=store.opaque_for(row['root_session']), observed_stream=prior)
        store.finish_region(reserved, 'succeeded', 'redacted material committed; model calls=0')
        store.record_capture_material(row['id'], dict(
            pipeline=MODE, refs=[store.ref(entry_id, doc['revision'])],
            redaction_rules=prepared['redaction_rules'], body_model_calls=0))
        if inc.get('more'):
            store.defer_capture(row['id'], due=time.time(), reason='more public transcript bytes')
    engine.last_activity = time.monotonic()


def summarize_due(engine):
    """Process one complete new batch; a saved response resumes locally only."""
    from .engine import AdmissionUnreadable, GateFault
    from .process import MaintenanceCancelled
    if time.monotonic() - engine.last_activity < SUMMARY_SETTLE_SECONDS:
        return
    store = engine.store
    batch = engine.index_queue.due()
    if batch is None:
        return
    attempt, ledger, operation = None, None, None
    try:
        authorization = json.loads(batch['authorization'])
        operation = engine.operations.begin('index', batch['batch_id'], authorization['generation'])
        if operation is None:
            return
        cancel = operation.cancel
        engine._gate_live()
        engine._revalidate(authorization)
        row = store._row(batch['entry_id'])
        task = store.materials.read_task(batch['entry_id'], revision=row['draft_revision'], source='draft')
        wanted = set(json.loads(batch['block_ids']))
        blocks = [store.materials.read_block(batch['entry_id'], b['block_id'], revision=row['draft_revision'])
                  for b in task['blocks'] if b['block_id'] in wanted and not b['indexed']]
        if not blocks:
            engine.index_queue.settle(batch['entry_id'])
            return
        prior_navigation = (dict(title=task['entry']['title'], summary=task['navigation'])
                            if any(b['indexed'] for b in task['blocks']) else None)
        selected = partition_blocks(blocks, prior_navigation)[0]
        ledger = SummaryLedger(store.db)
        attempt = engine.index_queue.latest(batch['entry_id'], batch_identity(selected))
        invoke = False
        if attempt is None or attempt['status'] == 'prepared' or batch['status'] == 'retry-requested':
            if not engine.summary_command:
                raise ValueError('configuration: summary worker is not configured')
            identity = validate_identity(json.loads(bounded_run([*engine.summary_command, '--identity'], '{}',
                                                                timeout=None, max_output=8192, cancel=cancel)))
            request = make_request(task_id=batch['entry_id'], body_version=batch_identity(selected), blocks=selected,
                                   prior_navigation=prior_navigation, identity=identity)
            attempt = engine.index_queue.claim(batch, request)
            engine._clear_faults('index.summary', {'worker-paused'})
            invoke = True
        if invoke:
            try:
                cleanup_receipt = {}
                def save_result(raw):
                    response = json.loads(raw)
                    validate_outcome(response, request)
                    try:
                        with store._write_txn():
                            ledger.record(attempt['attempt_id'], response)
                    except Exception as exc:
                        exc.mindie_execution = 'completed'
                        exc.mindie_result_status = response['status']
                        diagnostic_failure('index.summary', stage='receipt', category='summary_completed_recording_failed', exception=exc)
                        raise
                store.db.assert_authority()
                raw = bounded_run(engine.summary_command, canonical(request), timeout=None,
                                  max_output=MAX_RESPONSE_BYTES * 3, cancel=cancel,
                                  cleanup_receipt=cleanup_receipt, on_result=save_result)
                response = json.loads(raw)
                if ledger.get(attempt['attempt_id'])['status'] == 'invoking':
                    save_result(raw)
                if cleanup_receipt.get('cleanup_failed'):
                    response['cleanup_failed'] = True
                    with store._write_txn():
                        store.db.execute('UPDATE material_summary_attempts SET response=? WHERE attempt_id=?',
                                         (canonical(response), attempt['attempt_id']))
                if response.get('cleanup_failed'):
                    diagnostic_failure('index.summary', stage='cleanup', category='cleanup_failed')
            except Exception as exc:
                try:
                    with store._write_txn():
                        current = ledger.get(attempt['attempt_id'])
                        if current['status'] == 'invoking':
                            if getattr(exc, 'mindie_execution', None) == 'not_started':
                                ledger.record(attempt['attempt_id'], outcome(request, status='failed',
                                    error='configuration', model_calls=0, error_reason='native_start_failed'))
                            else:
                                category = getattr(exc, 'mindie_category', 'unknown')
                                ledger.uncertain(attempt['attempt_id'], category if category in {
                                    'deadline', 'cancelled', 'invalid_result', 'output_limit', 'native'} else 'unknown')
                except Exception as receipt_error:
                    exc.add_note('summary attempt receipt also failed: ' + type(receipt_error).__name__)
                    raise exc from receipt_error
                raise
        attempt = ledger.get(attempt['attempt_id'])
        if attempt['status'] != 'returned':
            receipt = json.loads(attempt['response']) if attempt['response'] else {}
            failure = failure_detail(receipt) if receipt else None
            reason = canonical(failure) if failure else attempt['error'] or 'unknown'
            engine.index_queue.finish(batch, attempt['status'], reason)
            diagnostic_failure('index.summary',
                stage=failure['stage'] if failure else 'native-execution',
                category=failure['reason'] if failure else 'outcome_unknown')
            return
        response = json.loads(attempt['response'])
        result = validate_result(response['result'], [b['block_id'] for b in selected])
        # This is local deterministic processing. The raw output/usage are
        # already durable, so scanner failure cannot cause another paid call.
        for header in [*result['blocks'], result['navigation']]:
            for field in ('title', 'summary'):
                header[field], _ = redact(header[field], executable=engine.redactor_executable,
                                          key=store.redaction_key(),
                                          private_paths=(authorization['scope'], str(Path.home())),
                                          cancel=cancel)
        validate_result(result, [b['block_id'] for b in selected])
        engine._gate_live()
        engine._revalidate(authorization)
        engine.index_queue.apply(batch, attempt, result, authorization['generation'],
                                 revalidate=lambda: engine._revalidate(authorization))
    except SummaryWorkerPaused as exc:
        engine._fault('index.summary', stage='worker-paused', category=exc.fault['reason'])
        engine.index_queue.postpone(batch['entry_id'], canonical(dict(
            status='worker-paused', attempt_id=exc.fault['attempt_id'], **exc.fault['detail'])), time.time() + 30)
    except (ScannerUnavailable, AdmissionUnreadable, GateFault) as exc:
        diagnostic_failure('index.summary', stage='local-apply', category='dependency_unavailable', exception=exc)
        with store._write_txn():
            if ledger and attempt and ledger.get(attempt['attempt_id'])['status'] == 'returned':
                ledger.local_failure(attempt['attempt_id'], 'scanner_unavailable' if isinstance(exc, ScannerUnavailable)
                                     else 'authority_unavailable')
            engine.index_queue.postpone(batch['entry_id'], type(exc).__name__ + '; no model replay', time.time() + 30)
    except MaintenanceCancelled:
        engine.index_queue.interrupted(batch, attempt, stopping=engine.stop.is_set())
    except Exception as exc:
        diagnostic_failure('index.summary', stage='process', category='summary_failed', exception=exc)
        if getattr(exc, 'metadata_committed', False):
            engine._error('Material metadata committed; current-file promotion or cleanup failed (' + type(exc).__name__ + ').')
            return
        try:
            with store._write_txn():
                state = ledger.get(attempt['attempt_id'])['status'] if ledger and attempt else 'failed'
                if state == 'returned':
                    ledger.local_failure(attempt['attempt_id'], 'apply_failed')
                    engine.index_queue.postpone(batch['entry_id'],
                        'local index apply failed: ' + type(exc).__name__, time.time() + 30)
                else:
                    state = 'outcome_unknown' if state in {'invoking', 'outcome_unknown'} else 'failed'
                    detail = ('configuration: summary worker is not configured'
                              if not engine.summary_command and isinstance(exc, ValueError)
                              else type(exc).__name__)
                    engine.index_queue.finish(batch, state, detail)
        except Exception as receipt_error:
            exc.add_note('summary state could not record this failure: ' + type(receipt_error).__name__)
            raise exc from receipt_error
    finally:
        if operation is not None:
            engine.operations.end(operation)
