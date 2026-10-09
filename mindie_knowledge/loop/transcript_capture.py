"""Authorized public material capture and one shared incremental index queue."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .process import bounded_run
from .dfx import failure as diagnostic_failure
from .store import canonical, digest
from .transcript_redaction import ScannerUnavailable, redact
from ..materials.ingest import prepare_increment
from ..materials.summarizer import (
    SummaryLedger, MAX_RESPONSE_BYTES, make_request,
    partition_blocks, validate_identity, validate_result, validate_outcome, batch_identity, failure_detail, outcome,
)

MODE = 'public-transcript'
SUMMARY_SETTLE_SECONDS = 3


def _admit_summary(engine):
    """A rolling envelope for new calls; returned-result recovery is free."""
    from .budget import BudgetExceeded
    limits = engine._settings().as_dict().get('summary_budget', {})
    calls = limits.get('calls_per_hour', 32)
    tokens = limits.get('input_tokens_per_hour', 512000)
    if type(calls) is not int or calls < 1 or type(tokens) is not int or tokens < 32768:
        raise ValueError('invalid summary budget configuration')
    rows = engine.store.db.execute("SELECT created,response FROM material_summary_attempts "
                                    "WHERE status!='prepared' AND created>?", (time.time() - 3600,)).fetchall()
    used, invoked = 0, []
    for row in rows:
        response = json.loads(row['response']) if row['response'] else {}
        model_calls = response.get('model_calls', 1)
        if type(model_calls) is not int or model_calls not in (0, 1):
            raise ValueError('invalid persisted summary model-call receipt')
        if model_calls == 0:
            continue  # Proven refusal/spawn failure consumed no model call.
        invoked.append(row['created'])
        usage = response.get('usage')
        used += usage['input_tokens'] if isinstance(usage, dict) and type(usage.get('input_tokens')) is int else 32768
    if len(invoked) >= calls or used + 32768 > tokens:
        retry_at = min(invoked) + 3601 if invoked else time.time() + 3600
        raise BudgetExceeded('summary rolling budget reached; material retained', retry_at=retry_at)


def pending_header(_text):
    return 'Task experience awaiting indexing', 'Reference material; indexing is pending.'


def capture(engine, row, text, region):
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
                                 private_paths=(row['scope'], str(Path.home())), cancel=engine._cancel)
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


def _settle_task(store, entry_id):
    """Derive queue status from current block indexes, never from process liveness."""
    row = store._row(entry_id)
    task = store.materials.read_task(entry_id, revision=row['draft_revision'], source='draft')
    indexed = {b['block_id'] for b in task['blocks'] if b['indexed']}
    for batch in store.db.execute("SELECT batch_id,block_ids FROM material_batches WHERE entry_id=? AND status!='complete'",
                                  (entry_id,)).fetchall():
        if set(json.loads(batch['block_ids'])) <= indexed:
            store.db.execute("UPDATE material_batches SET status='complete',detail='' WHERE batch_id=?", (batch['batch_id'],))
    batches = store.db.execute('SELECT status,detail FROM material_batches WHERE entry_id=?', (entry_id,)).fetchall()
    statuses = {r['status'] for r in batches}
    status = 'complete' if statuses == {'complete'} else ('outcome_unknown' if 'outcome_unknown' in statuses else
             'failed' if 'failed' in statuses else 'cancelled' if 'cancelled' in statuses else 'pending')
    details = sorted({r['detail'] for r in batches if r['status'] == status and r['detail']})
    detail = '' if status in {'complete', 'pending'} else ('index-summary ' + status + ': ' +
              ('; '.join(details) if details else 'explicit recovery required'))[:500]
    if status not in {'complete', 'pending'} and details and details[0].startswith('{'):
        detail = details[0]
    store.db.execute('UPDATE transcript_tasks SET summary_status=?,summary_detail=?,body_digest=?,summary_due=? WHERE entry_id=?',
                     (status, detail, task['entry']['material_digest'], time.time(), entry_id))


def summarize_due(engine):
    """Process one complete new batch; a saved response resumes locally only."""
    from .engine import AdmissionUnreadable, GateFault
    from .process import MaintenanceCancelled
    from .budget import BudgetExceeded
    if time.monotonic() - engine.last_activity < SUMMARY_SETTLE_SECONDS:
        return
    store = engine.store
    with store.lock:
        found = store.db.execute("SELECT b.* FROM material_batches b JOIN transcript_tasks t ON t.entry_id=b.entry_id "
                                 "WHERE b.status IN ('pending','retry-requested') AND t.summary_due<=? ORDER BY b.created,b.batch_id LIMIT 1",
                                 (time.time(),)).fetchone()
    if found is None or not engine.begin_work():
        return
    batch, attempt, ledger = dict(found), None, None
    try:
        authorization = json.loads(batch['authorization'])
        engine._summary_cancel.clear()
        engine._gate_live()
        engine._revalidate(authorization)
        row = store._row(batch['entry_id'])
        task = store.materials.read_task(batch['entry_id'], revision=row['draft_revision'], source='draft')
        wanted = set(json.loads(batch['block_ids']))
        blocks = [store.materials.read_block(batch['entry_id'], b['block_id'], revision=row['draft_revision'])
                  for b in task['blocks'] if b['block_id'] in wanted and not b['indexed']]
        if not blocks:
            with store._write_txn():
                _settle_task(store, batch['entry_id'])
            return
        prior_navigation = (dict(title=task['entry']['title'], summary=task['navigation'])
                            if any(b['indexed'] for b in task['blocks']) else None)
        selected = partition_blocks(blocks, prior_navigation)[0]
        with store._write_txn():
            ledger = SummaryLedger(store.db)
            attempt = ledger.latest(batch['entry_id'], batch_identity(selected))
        invoke = False
        if attempt is None or attempt['status'] == 'prepared' or batch['status'] == 'retry-requested':
            if not engine.summary_command:
                raise ValueError('configuration: summary worker is not configured')
            identity = validate_identity(json.loads(bounded_run([*engine.summary_command, '--identity'], '{}',
                                                                timeout=None, max_output=8192, cancel=engine._summary_cancel)))
            request = make_request(task_id=batch['entry_id'], body_version=batch_identity(selected), blocks=selected,
                                   prior_navigation=prior_navigation, identity=identity)
            with store._write_txn():
                _admit_summary(engine)
                if batch['status'] == 'retry-requested' and attempt and attempt['status'] in {'failed', 'outcome_unknown'}:
                    attempt = ledger.retry(attempt['attempt_id'], request)
                else:
                    attempt = ledger.prepare(request)
                store.db.execute('UPDATE material_summary_attempts SET created=? WHERE attempt_id=? AND status=\'prepared\'',
                                 (time.time(), attempt['attempt_id']))
                ledger.claim(attempt['attempt_id'])
                attempt = ledger.get(attempt['attempt_id'])
                store.db.execute("UPDATE material_batches SET status='pending' WHERE batch_id=?", (batch['batch_id'],))
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
                                  max_output=MAX_RESPONSE_BYTES * 3, cancel=engine._summary_cancel,
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
            with store._write_txn():
                store.db.execute('UPDATE material_batches SET status=?,detail=? WHERE batch_id=?',
                                 (attempt['status'], reason, batch['batch_id']))
                _settle_task(store, batch['entry_id'])
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
                                          cancel=engine._summary_cancel)
        validate_result(result, [b['block_id'] for b in selected])
        engine._gate_live()
        engine._revalidate(authorization)
        with store._write_txn():
            engine._revalidate(authorization)
            store.apply_material_indexes(entry_id=batch['entry_id'], indexes=result['blocks'],
                                          navigation=result['navigation'], generation=authorization['generation'])
            ledger.complete(attempt['attempt_id'])
            _settle_task(store, batch['entry_id'])
    except BudgetExceeded as exc:
        with store._write_txn():
            store.db.execute('UPDATE transcript_tasks SET summary_detail=?,summary_due=? WHERE entry_id=?',
                             ('summary budget deferred; no call made', exc.retry_at, batch['entry_id']))
    except (ScannerUnavailable, AdmissionUnreadable, GateFault) as exc:
        diagnostic_failure('index.summary', stage='local-apply', category='dependency_unavailable', exception=exc)
        with store._write_txn():
            if ledger and attempt and ledger.get(attempt['attempt_id'])['status'] == 'returned':
                ledger.local_failure(attempt['attempt_id'], 'scanner_unavailable' if isinstance(exc, ScannerUnavailable)
                                     else 'authority_unavailable')
            store.db.execute("UPDATE transcript_tasks SET summary_detail=?,summary_due=? WHERE entry_id=?",
                             (type(exc).__name__ + '; no model replay', time.time() + 30, batch['entry_id']))
    except MaintenanceCancelled:
        with store._write_txn():
            attempted = ledger.get(attempt['attempt_id']) if ledger and attempt else None
            if attempted and attempted['status'] == 'outcome_unknown':
                # Process cancellation does not prove an attempted external
                # model call failed or was never billed. Keep its uncertainty.
                store.db.execute("UPDATE material_batches SET status='outcome_unknown',detail=? WHERE batch_id=?",
                                 ('summary call interrupted; external outcome unknown', batch['batch_id']))
            elif engine.stop.is_set():
                # Shutdown is not revocation. A prepared call or saved return
                # remains pending; restart revalidates authority before work.
                store.db.execute("UPDATE material_batches SET status='pending',detail='service stopped; local work retained' "
                                 "WHERE batch_id=?", (batch['batch_id'],))
            else:
                store.db.execute("UPDATE material_batches SET status='cancelled',detail='contribution authority revoked' "
                                 "WHERE entry_id=? AND status IN ('pending','retry-requested')", (batch['entry_id'],))
            _settle_task(store, batch['entry_id'])
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
                    store.db.execute('UPDATE transcript_tasks SET summary_detail=?,summary_due=? WHERE entry_id=?',
                                     ('local index apply failed: ' + type(exc).__name__, time.time() + 30, batch['entry_id']))
                else:
                    state = 'outcome_unknown' if state in {'invoking', 'outcome_unknown'} else 'failed'
                    detail = 'configuration: summary worker is not configured' if not engine.summary_command else type(exc).__name__
                    store.db.execute('UPDATE material_batches SET status=?,detail=? WHERE batch_id=?',
                                     (state, detail, batch['batch_id']))
                    _settle_task(store, batch['entry_id'])
        except Exception as receipt_error:
            exc.add_note('summary state could not record this failure: ' + type(receipt_error).__name__)
            raise exc from receipt_error
    finally:
        engine.end_work()
