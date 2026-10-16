"""Publication stages and receipts; scheduling and authority belong to Engine.

A frozen batch owns its original remote intent. Read-only reconciliation never
creates a new write; a returned result survives receipt or cleanup failures.
"""
from .store import Store


def record_publication_receipt(store, batch_row, receipt, *, attempted=False):
    """Keep the publisher's exact result if recording it fails locally."""
    try:
        confirmed = receipt.get("status") in Store.CONFIRMED_BATCH
        for key in ("batch_id", "revision"):
            actual = receipt.get(key)
            if (confirmed or actual is not None) and actual != batch_row[key]:
                raise ValueError("publication receipt does not identify the current " + key)
        store.mark_batch(
            batch_row["batch_id"], receipt.get("status", "unknown"),
            attempted=attempted, detail=receipt.get("detail", ""),
            pr_url=receipt.get("pr_url"), head_sha=receipt.get("head_sha"),
            actual_files=receipt.get("files"), retry_at=receipt.get("retry_at"),
        )
    except Exception as exc:
        exc.mindie_publication_result = receipt
        exc.add_note(
            "publication result was returned before runtime receipt failure: "
            + receipt.get("status", "unknown"))
        raise


class PublicationWorker:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store

    def submit(self, batch):
        with self.engine.operations.scope("publish", batch["batch_id"], batch["generation"]) as operation:
            if operation is not None:
                return self._submit(batch, operation.cancel)

    def reconcile(self, batch):
        with self.engine.operations.scope("reconcile", batch["batch_id"]) as operation:
            if operation is not None:
                self._reconcile(batch, operation.cancel)

    def flush(self):
        settings = self.engine._settings()
        with self.engine.operations.scope("package", generation=settings.generation) as operation:
            if operation is not None:
                self._flush()

    def _submit(self, batch_row, cancel):
        settings = self.engine._settings()
        if not settings.allows_capture():
            return
        if batch_row["generation"] != settings.generation:
            self.store.mark_batch(
                batch_row["batch_id"], "disabled",
                detail="settings generation changed before send",
            )
            return
        if self.engine.community is None:
            self.store.mark_batch(
                batch_row["batch_id"], "unavailable", attempted=True,
                detail="mindie_knowledge.community is not installed; "
                       "the contribution cannot be sent (dependency failure)",
            )
            return
        from ..materials.publication import load_batch_payload
        try:
            batch = load_batch_payload(self.store, batch_row)
        except Exception as exc:
            self.engine._fault('knowledge.publish', 'payload', 'publication_payload_unavailable', exc)
            try:
                self.store.mark_batch(batch_row['batch_id'], 'unavailable',
                                      detail='local frozen contribution could not be loaded: ' + type(exc).__name__)
            except Exception as receipt_error:
                exc.add_note('payload failure receipt also failed: ' + type(receipt_error).__name__)
                raise exc from receipt_error
            self.engine._error('Publication did not start: frozen contribution load failed (' + type(exc).__name__ + ').')
            return
        self.engine._clear_faults('knowledge.publish', {'payload'})
        try:
            withdrawn = any(self.store.is_withdrawn(ref)
                            for ref in batch["entry_refs"])
        except ValueError:
            self.store.mark_batch(batch_row["batch_id"], "needs_review",
                                  detail="pending contribution reference no longer resolves")
            return
        if withdrawn:
            self.store.mark_batch(batch_row["batch_id"], "disabled",
                                  detail="upstream withdrew pending contribution material")
            return
        try:
            receipt = self.engine.community["submit_batch"](
                batch, settings.as_dict(), self.engine.state_dir, cancel=cancel,
                authority_guard=self.store.db.assert_authority,
            )
        except Exception as exc:
            self.engine._fault('knowledge.publish', 'submit', 'publication_failed', exc)
            self.engine._unexpected("knowledge.publish", "submit", exc)
            try:
                self.store.mark_batch(
                    batch_row["batch_id"], "unknown", attempted=True,
                    detail=f"{type(exc).__name__}: {exc}"[:500],
                )
            except Exception as receipt_error:
                exc.add_note('publication runtime receipt also failed: ' + type(receipt_error).__name__)
                raise exc from receipt_error
            return
        self._record_publication_receipt(batch_row, receipt, attempted=True)
        if receipt.get("status") in Store.CONFIRMED_BATCH:
            # Confirmed upload retires staging. The candidate remains readable
            # until this revision is confirmed in the synchronized public feed.
            try:
                cleanup = self.store.compact_confirmed(batch_row["batch_id"])
            except Exception as exc:
                exc.mindie_publication_result = receipt
                exc.add_note('Publication confirmed before local cleanup failure')
                raise
            if cleanup.get('cleanup_status') == 'failed':
                self.engine._error('Publication confirmed; local cleanup failed: ' + cleanup.get('cleanup_error', 'unknown'))
        return receipt

    def _reconcile(self, batch_row, cancel):
        if self.engine.community is None:
            return  # stay unresolved; the dependency failure is reported on submit
        settings = self.engine._settings()
        if not settings.allows_capture():
            if settings.capture_block_kind() == 'fault':
                self.engine._fault('knowledge.publish', 'authorization', 'authority_unavailable')
            return  # A paused read establishes no new fact about the original write.
        try:
            receipt = self.engine.community["reconcile_batch"](
                batch_row["batch_id"], settings.as_dict(), self.engine.state_dir,
                cancel=cancel, authority_guard=self.store.db.assert_authority,
            )
        except Exception as exc:
            self.engine._fault('knowledge.publish', 'reconcile', 'publication_reconciliation_failed', exc)
            self.engine._unexpected("knowledge.publish", "reconcile", exc)
            # A failed read establishes no new remote fact. Keep the original
            # intent/result and report this separate reconciliation failure.
            self.engine._error(f"publication reconciliation failed: {type(exc).__name__}")
            return
        self._record_publication_receipt(batch_row, receipt)
        self.engine._clear_faults('knowledge.publish', {'reconcile'})
        if receipt.get("status") in Store.CONFIRMED_BATCH:
            cleanup = self.store.compact_confirmed(batch_row["batch_id"])
            if cleanup.get('cleanup_status') == 'failed':
                self.engine._error('Publication confirmed; local cleanup failed: ' + cleanup.get('cleanup_error', 'unknown'))

    def _record_publication_receipt(self, batch_row, receipt, *, attempted=False):
        """A returned write or read result survives a local recording failure."""
        try:
            record_publication_receipt(self.store, batch_row, receipt, attempted=attempted)
        except Exception as exc:
            self.engine._fault('knowledge.publish', 'receipt', 'publication_recording_failed', exc)
            raise
        self.engine._clear_faults('knowledge.publish', {'receipt'})

    def _flush(self):
        """Coalesce all pending material into one batch and send it."""
        from .export import build_batch, ExportQuarantinedError
        from ..materials.publication import CleanupReceiptError

        settings = self.engine._settings()
        if not settings.allows_capture():
            return
        rejection = None
        try:
            built = build_batch(self.store, settings=settings)
        except ExportQuarantinedError as exc:
            rejection = exc
            self.engine._fault('knowledge.publish', 'content-scan', 'publication_content_rejected', exc)
            self.engine._error(str(exc))
            if exc.built is None:
                raise
            batch_id = exc.built[0]
        except CleanupReceiptError as exc:
            batch_id = exc.batch_id
            self.engine._error("New contribution staged; prior staging cleanup receipt failed: " + str(exc))
        else:
            if built is None:
                return
            batch_id, _revision, _batch, _ids, _votes = built
            cleanup = self.store.feed_get("publication-cleanup:" + batch_id)
            if cleanup and cleanup.get("status") == "failed":
                self.engine._error("New contribution staged; prior staging cleanup failed: " + cleanup.get("detail", "unknown"))
        try:
            receipt = self.submit(self.store.batch(batch_id))
        except Exception as exc:
            if rejection is not None:
                receipt = getattr(exc, 'mindie_publication_result', None)
                if receipt is not None:
                    rejection.publication = {key: receipt.get(key) for key in ('status', 'pr_url', 'head_sha')}
                    rejection.publication['batch_id'] = batch_id
                rejection.add_note('Unrelated package submission also failed: ' + type(exc).__name__)
                raise rejection from exc
            raise
        if rejection is not None:
            try:
                row = receipt if receipt is not None else self.store.batch(batch_id)
            except Exception as exc:
                rejection.add_note('Unrelated publication receipt read failed: ' + type(exc).__name__)
                raise rejection from exc
            rejection.publication = {key: row.get(key) for key in ('status', 'pr_url', 'head_sha')}
            rejection.publication['batch_id'] = batch_id
            raise rejection
