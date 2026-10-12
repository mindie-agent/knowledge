"""Incremental index stages on the existing material/attempt transaction.

Queue readiness is derived from current block indexes and saved attempt facts.
Model execution stays outside transactions. Applying its returned result and
completing the attempt share the body metadata transaction; promotion failures
remain a separate, observable outcome after that commit.
"""
from __future__ import annotations

import json
import time

from ..materials.summarizer import SummaryLedger


class SummaryWorkerPaused(RuntimeError):
    def __init__(self, fault):
        self.fault = fault
        super().__init__('summary worker paused after ' + fault['reason'] + '; no call made')


class IndexQueue:
    def __init__(self, store):
        self.store = store

    def due(self):
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT b.* FROM material_batches b JOIN transcript_tasks t ON t.entry_id=b.entry_id "
                "WHERE b.status IN ('pending','retry-requested') AND t.summary_due<=? "
                "ORDER BY b.created,b.batch_id LIMIT 1", (time.time(),)).fetchone()
            return dict(row) if row is not None else None

    def has_due(self):
        with self.store.lock:
            return self.store.db.execute(
                "SELECT 1 FROM material_batches b JOIN transcript_tasks t ON t.entry_id=b.entry_id "
                "WHERE b.status IN ('pending','retry-requested') AND t.summary_due<=? LIMIT 1",
                (time.time(),)).fetchone() is not None

    def latest(self, task_id, batch_id):
        with self.store.lock:
            return SummaryLedger(self.store.db).latest(task_id, batch_id)

    def claim(self, batch, request):
        with self.store._write_txn():
            ledger = SummaryLedger(self.store.db)
            previous = ledger.latest(request['task_id'], request['batch_id'])
            explicit_retry = (batch['status'] == 'retry-requested' and previous
                              and previous['status'] in {'failed', 'outcome_unknown'})
            fault = ledger.worker_fault(request['policy_identity'])
            if fault and not explicit_retry:
                raise SummaryWorkerPaused(fault)
            if explicit_retry:
                attempt = ledger.retry(previous['attempt_id'], request)
            else:
                attempt = ledger.prepare(request)
            self.store.db.execute(
                "UPDATE material_summary_attempts SET created=? WHERE attempt_id=? AND status='prepared'",
                (time.time(), attempt['attempt_id']))
            ledger.claim(attempt['attempt_id'])
            self.store.db.execute("UPDATE material_batches SET status='pending' WHERE batch_id=?",
                                  (batch['batch_id'],))
            return ledger.get(attempt['attempt_id'])

    def apply(self, batch, attempt, result, generation, *, revalidate):
        with self.store._write_txn():
            revalidate()
            self.store.apply_material_indexes(entry_id=batch['entry_id'], indexes=result['blocks'],
                                              navigation=result['navigation'], generation=generation)
            SummaryLedger(self.store.db).complete(attempt['attempt_id'])
            self.settle(batch['entry_id'])

    def settle(self, entry_id):
        store = self.store
        with store._write_txn():
            row = store._row(entry_id)
            task = store.materials.read_task(entry_id, revision=row['draft_revision'], source='draft')
            indexed = {b['block_id'] for b in task['blocks'] if b['indexed']}
            for batch in store.db.execute(
                    "SELECT batch_id,block_ids FROM material_batches WHERE entry_id=? AND status!='complete'",
                    (entry_id,)).fetchall():
                if set(json.loads(batch['block_ids'])) <= indexed:
                    store.db.execute("UPDATE material_batches SET status='complete',detail='' WHERE batch_id=?",
                                     (batch['batch_id'],))
            batches = store.db.execute('SELECT status,detail FROM material_batches WHERE entry_id=?',
                                       (entry_id,)).fetchall()
            statuses = {r['status'] for r in batches}
            status = ('complete' if statuses == {'complete'} else
                      'outcome_unknown' if 'outcome_unknown' in statuses else
                      'failed' if 'failed' in statuses else 'cancelled' if 'cancelled' in statuses else 'pending')
            details = sorted({r['detail'] for r in batches if r['status'] == status and r['detail']})
            detail = '' if status in {'complete', 'pending'} else ('index-summary ' + status + ': ' +
                      ('; '.join(details) if details else 'explicit recovery required'))[:500]
            if status not in {'complete', 'pending'} and details and details[0].startswith('{'):
                detail = details[0]
            store.db.execute(
                'UPDATE transcript_tasks SET summary_status=?,summary_detail=?,body_digest=?,summary_due=? WHERE entry_id=?',
                (status, detail, task['entry']['material_digest'], time.time(), entry_id))

    def postpone(self, entry_id, detail, due):
        with self.store._write_txn():
            self.store.db.execute('UPDATE transcript_tasks SET summary_detail=?,summary_due=? WHERE entry_id=?',
                                  (detail, due, entry_id))

    def finish(self, batch, status, detail):
        with self.store._write_txn():
            self.store.db.execute('UPDATE material_batches SET status=?,detail=? WHERE batch_id=?',
                                  (status, detail, batch['batch_id']))
            self.settle(batch['entry_id'])

    def interrupted(self, batch, attempt, *, stopping):
        with self.store._write_txn():
            current = SummaryLedger(self.store.db).get(attempt['attempt_id']) if attempt else None
            if current and current['status'] == 'outcome_unknown':
                status, detail = 'outcome_unknown', 'summary call interrupted; external outcome unknown'
            elif stopping:
                status, detail = 'pending', 'service stopped; local work retained'
            else:
                status, detail = 'cancelled', 'contribution authority revoked'
            self.finish(batch, status, detail)

    def retry(self, entry_id):
        """Explicit retry only; prior paid-attempt receipts remain intact."""
        with self.store._write_txn():
            self.store.db.execute(
                "UPDATE material_batches SET status='retry-requested',detail='' "
                "WHERE entry_id=? AND status IN ('failed','outcome_unknown')", (entry_id,))
            self.settle(entry_id)
