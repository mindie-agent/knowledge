"""[Role] Background capture, incremental LangMem indexing, and community publication orchestrator (`Engine`).

[Invariants]
- Re-verifies shared `CommunitySettings` (`mindie-community-config/1`), profile
  consent (`mindie-consent/1`), and session `Admission` lease before every capture,
  summary worker call, and outbound publication.
- `GateFault` (damaged/unreadable authority) parks queued work with bounded
  backoff; explicit user revocation (`enabled=False` or non-`contribute` consent)
  cancels unsent work; `unknown` external effects reconcile read-only without replay.

[Timeouts & Locks]
- Coordinates three daemon loops (`mindie-capture`, `mindie-outbox`, `mindie-summary`)
  with cancellable `OperationOwner` scopes and `stop` event signaling.

[Side-Effect Policy]
- Delegates redaction and summary worker subprocesses via `bounded_run` and
  outbound Git/GitHub publication via `PublicationWorker`.
"""

from __future__ import annotations

import hashlib
import os
import queue
import threading
import time
from pathlib import Path

from . import settings as settings_mod
from .activation import activation_epoch, AdmissionUnavailable as AdmissionUnreadable
from .dfx import failure
from .process import MaintenanceCancelled
from .store import canonical, session_key
from .operations import OperationOwner
from .index_queue import IndexQueue
from .publication_worker import PublicationWorker
from ..filesystem import path_aliases, resolved_path

from .transcript_increment import (
    CursorConflict,
    GateFault,
    TranscriptCursorError,
    TranscriptMixin,
    transcript_cursor,
)


class Engine(TranscriptMixin):
    def __init__(self, store, *, settings_path=None,
                 admission=None, state_dir=None, transcript_adapter=None,
                 capture_mode="public-transcript", redactor_executable=None, summary_command=None):
        """``transcript_adapter`` is the already-loaded trusted parser module
        (absolute local module from engine config ``transcript_adapter``)
        exporting ``FileIdentity``/``identify``/``read_material``. Without it
        transcript capture fails explicitly; core never substitutes a summary."""
        self.store = store
        if capture_mode != "public-transcript":
            raise ValueError("only public-transcript capture is supported")
        if summary_command is not None and (not isinstance(summary_command, list) or not summary_command or not all(isinstance(x, str) for x in summary_command)):
            raise ValueError("summary_command must be a nonempty argv list")
        self.capture_mode = capture_mode
        self.redactor_executable = redactor_executable
        self.summary_command = summary_command
        self.settings_path = settings_path
        self.admission = admission
        self.transcript = transcript_adapter
        self.state_dir = Path(state_dir) if state_dir else store.root / "outbox"
        self.queue = queue.Queue(maxsize=8)
        self.stop = threading.Event()
        self.operations = OperationOwner(self.stop)
        self.index_queue = IndexQueue(store)
        self.publisher = PublicationWorker(self)
        self._work_local = threading.local()
        self.thread = threading.Thread(
            target=self.run, name="mindie-capture", daemon=True
        )
        self.outbox_thread = threading.Thread(
            target=self._outbox_loop, name="mindie-outbox", daemon=True
        )
        self.summary_thread = threading.Thread(
            target=self._summary_loop, name="mindie-summary", daemon=True
        )
        self.errors = []
        self.background_errors = {}
        self.last_activity = time.monotonic()
        self._observed_authority = None
        self._worker_failed = False
        self._flush_attempts = 0
        self._flush_not_before = 0.0
        self._reported_faults = set()
        self._fault_lock = threading.Lock()
        # Publication belongs to this package. A broken installation is a
        # startup failure, not a service with silently missing capabilities.
        from mindie_knowledge.community import reconcile_batch, submit_batch
        self.community = dict(submit_batch=submit_batch, reconcile_batch=reconcile_batch)
    def _settings(self):
        return settings_mod.load(self.settings_path)
    def apply_distillation(self, request, decision):
        """Apply an explicit C6 organizer result; never infer completion from Stop."""
        from ..graded.distill import apply_decision
        return apply_decision(self.store, request, decision)
    def distill_selected(self, inputs, *, session_digest, task_state, authorization=None, cancel=None,
                         related_refs=None, proposal=None, topic=None, target_signature=None):
        """Explicit verified milestone/completion; ordinary Stop does not select it."""
        from ..graded.worker import run
        from .operations import CombinedCancellation
        generation = authorization.get("generation") if authorization else None
        with self.operations.scope("index", "distill:" + session_digest, generation) as operation:
            if operation is None:
                raise ValueError("service is not admitting distillation work")
            return run(self, inputs, session_digest=session_digest, task_state=task_state,
                       authorization=authorization, cancel=CombinedCancellation(cancel, operation.cancel),
                       related_refs=related_refs, proposal=proposal,
                       topic=topic, target_signature=target_signature)
    def _error(self, detail):
        self.errors = (self.errors + [detail])[-20:]
    def _fault(self, operation, stage, category, exception=None):
        """Deliver known required-stage failures too, once while unchanged."""
        key = (operation, stage, category)
        with self._fault_lock:
            if key not in self._reported_faults:
                result = failure(operation, stage=stage, category=category, exception=exception,
                                 reportable=False)
                if not result.get('delivery_failed'):
                    self._reported_faults.add(key)
    def _clear_faults(self, operation, stages=None):
        with self._fault_lock:
            self._reported_faults.difference_update({key for key in self._reported_faults
                if key[0] == operation and (stages is None or key[1] in stages)})
    def _unexpected(self, operation, stage, exc):
        """Report an unhandled internal error. Expected cancellation,
        config/caller, authority-fault, and community business errors are not
        incidents."""
        if isinstance(exc, (MaintenanceCancelled, AdmissionUnreadable, CursorConflict,
                            GateFault, OSError, ValueError, TypeError)):
            # A timeout is an explicit failure boundary, not routine local I/O.
            if not isinstance(exc, TimeoutError):
                return
        try:
            from mindie_knowledge.community.common import CommunityError
        except Exception:
            pass
        else:
            if isinstance(exc, CommunityError):
                return
        failure(
            operation, stage=stage, category="internal_exception", exception=exc,
        )
    def start(self):
        with self.store._write_txn():
            from ..materials.summarizer import SummaryLedger
            SummaryLedger(self.store.db).recover()
        self.revoke_stale()
        self.thread.start()
        self.outbox_thread.start()
        # Missing configuration is a required-stage failure. The queue worker
        # must settle it explicitly rather than leaving due material pending
        # forever and preventing an otherwise idle service from stopping.
        self.summary_thread.start()
    def revoke_stale(self):
        """Restart/poll-edge safety net: when sharing is enabled, pending
        batches of any other generation become disabled; when it is disabled,
        every unsent batch and publishable vote is cancelled. Old material is
        never backfilled into a new generation. An unknown authority state
        (fault) is not a revocation: unsent work is held, not cancelled."""
        settings = self._settings()
        block = settings.capture_block_kind()
        if block is None:
            self._cancel_unsent("settings generation changed; unsent work cancelled",
                                keep_generation=settings.generation)
        elif block == "revoked":
            self._cancel_unsent("sharing disabled; unsent work cancelled")
    def run(self):
        while not self.stop.is_set():
            if self._is_frozen():
                self.stop.wait(0.2)
                continue
            try:
                ident = self.queue.get(timeout=0.5)
            except queue.Empty:
                if self._is_frozen() or self.stop.is_set():
                    continue
                try:
                    ident = self.store.due_capture()
                    if ident:
                        self._process(ident)
                except (MaintenanceCancelled, AdmissionUnreadable, CursorConflict,
                        GateFault):
                    continue
                except Exception as exc:
                    self._fault('knowledge.capture', 'worker', 'capture_worker_failed', exc)
                    self._unexpected("knowledge.worker", "run", exc)
                    # Stop this thread and leave rows durable. Do not count
                    # errors or start another worker inside the loop.
                    self._worker_failed = True
                    return
                continue
            try:
                if ident:
                    self._process(ident)
            except (MaintenanceCancelled, AdmissionUnreadable, CursorConflict,
                    GateFault):
                pass
            except Exception as exc:  # never let the worker die silently
                self._fault('knowledge.capture', 'worker', 'capture_worker_failed', exc)
                self._unexpected("knowledge.worker", "run", exc)
                self._error(f"{type(exc).__name__}: {exc}"[:1000])
            finally:
                self.queue.task_done()
        while True:  # unattempted queued work remains durable across restart
            try:
                ident = self.queue.get_nowait()
            except queue.Empty:
                break
            self.queue.task_done()
    def _summary_loop(self):
        """Required transcript metadata runs outside the body capture worker."""
        from .transcript_capture import summarize_due
        while not self.stop.wait(0.5):
            if self._is_frozen():
                continue
            try:
                summarize_due(self)
            except Exception as exc:
                self._fault('knowledge.summary', 'worker', 'summary_worker_failed', exc)
                self._unexpected("knowledge.summary", "run", exc)
                self.background_errors["summary"] = type(exc).__name__
                self._error(f"summary worker stopped: {type(exc).__name__}")
                return
    def _cancel_unsent(self, reason, *, keep_generation=None):
        """Revoke unsent work, retaining any already admitted current generation."""
        self.operations.revoke(keep_generation=keep_generation)
        self.store.revoke_unsent(reason, keep_generation=keep_generation)
    def _submit(self, batch_row):
        return self.publisher.submit(batch_row)
    def _reconcile(self, batch_row):
        return self.publisher.reconcile(batch_row)
    def _flush(self):
        return self.publisher.flush()
    def _flush_with_backoff(self):
        """A repeatedly failing flush backs off exponentially instead of
        hot-looping; the durable cause stays visible until one success
        restores normal scheduling. Quarantine rejections already published
        the rest of the material — they are a result, not a retryable fault."""
        from .export import ExportQuarantinedError
        try:
            self._flush()
        except ExportQuarantinedError:
            raise
        except Exception as exc:
            self._flush_attempts += 1
            self._flush_not_before = time.monotonic() + min(300, 2 ** min(self._flush_attempts, 8))
            self.background_errors["outbox"] = type(exc).__name__
            raise
        else:
            self._flush_attempts = 0
            self._flush_not_before = 0.0
            self.background_errors.pop("outbox", None)
    def _outbox_loop(self):
        """Model-free idle worker: lives for the service lifetime so the
        silent window always elapses before delivery; it never disappears
        after one Stop and never extends a model deadline."""
        while not self.stop.is_set():
            try:
                settings = self._settings()
                block = settings.capture_block_kind()
                generation = settings.generation if block is None else None
                if block == "fault":
                    # Unknown authority state: hold unsent/pending work in
                    # place — no cancel edge, no send, no flush. Work resumes
                    # unchanged once the authority is restored; a generation
                    # that genuinely changed is still caught on the next
                    # non-fault tick by the edge below.
                    if (self.settings_path is not None or self.store.outbox_pending(limit=1)
                            or self.store.outbox_unresolved(limit=1) or self.store.outbox_unavailable(limit=1)):
                        self._fault('knowledge.publish', 'authorization', 'authority_unavailable')
                elif ((block, generation) != self._observed_authority
                      or self.operations.has_foreign_contribution(generation)):
                    self._cancel_unsent(
                        "sharing disabled; unsent work cancelled" if block == "revoked" else
                        "settings generation changed; unsent work cancelled",
                        keep_generation=generation,
                    )
                if block != "fault":
                    self._observed_authority = (block, generation)
                    self._clear_faults('knowledge.publish', {'authorization'})
                # ReMe refreshes on query or an explicit index operation. The
                # retired FTS scheduler must not import/index a whole library
                # under the state lock during capture or service startup.
                if generation is not None and not self._is_frozen():
                    for row in self.store.outbox_unresolved(limit=2, due_only=True):
                        if self._is_frozen():
                            break
                        if self.store.reconcile_due(row["batch_id"]):
                            self._reconcile(row)
                    # Transient send failures resume automatically with
                    # persisted backoff: the stored batch payload is retried,
                    # never rebuilt from scratch and never silently dropped.
                    for row in self.store.outbox_unavailable(limit=2, due_only=True):
                        if self._is_frozen():
                            break
                        if self.store.retry_due(row["batch_id"]):
                            self._submit(row)
                    for row in self.store.outbox_pending(limit=2):
                        if self._is_frozen():
                            break
                        self._submit(row)
                    material = self.store.has_changed_drafts(
                        generation=generation, ready_only=True
                    ) or self.store.graded.pending(generation) or self.store.unbatched_votes(generation=generation)
                    if material:
                        idle_for = time.monotonic() - self.last_activity
                        deactivated = (
                            self.admission is not None
                            and not self.admission.leases()
                        )
                        if (
                            (idle_for >= settings.idle_seconds or deactivated)
                            and not self._is_frozen()
                            and time.monotonic() >= self._flush_not_before
                        ):
                            self._flush_with_backoff()
                self._clear_faults('knowledge.publish', {'worker'})
            except Exception as exc:
                self._fault('knowledge.publish', 'worker', 'publication_worker_failed', exc)
                self._unexpected("knowledge.outbox", "tick", exc)
                self._error(f"outbox: {type(exc).__name__}: {exc}"[:500])
            self.stop.wait(1.0)
    def shutdown(self):
        self.operations.shutdown()
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass
        self.thread.join(timeout=10)
        self.outbox_thread.join(timeout=5)
        if self.summary_thread.ident is not None:
            self.summary_thread.join(timeout=10)
    def begin_work(self):
        """Admit one actual service/worker operation. False when frozen or stopping."""
        operation = self.operations.begin("capability")
        if operation is None:
            return False
        if not hasattr(self._work_local, "stack"):
            self._work_local.stack = []
        self._work_local.stack.append(operation)
        return True
    def end_work(self):
        self.operations.end(self._work_local.stack.pop())
    def _is_frozen(self):
        return self.operations.frozen()
    def stop_if_idle(self):
        """Atomically freeze new admission and report whether anything is
        actually running or queued. Unknown/pending receipts and idle task
        grants are not activity. Never cancels in-flight work: a busy result
        unfreezes so the service continues."""
        activity = self.operations.freeze()
        queued = self.queue.unfinished_tasks
        if self.stop.is_set():
            return dict(idle=True, status="stopping", activity=activity,
                        queued_captures=queued, due_capture=False)
        try:
            due = self.store.due_capture() is not None
            due_summary = self.index_queue.has_due()
        except BaseException:
            self.operations.resume()
            raise
        busy = activity > 0 or queued > 0 or due or due_summary
        if busy:
            self.operations.resume()
            return dict(
                idle=False, status="busy", activity=activity,
                queued_captures=queued, due_capture=due, due_summary=due_summary,
            )
        return dict(
            idle=True, status="stopping", activity=0,
            queued_captures=0, due_capture=False, due_summary=False,
        )
    def status(self):
        settings = self._settings()
        # Passive local prerequisite visibility. Executables being present
        # proves neither authentication nor permission to publish.
        import shutil
        transport = settings.as_dict().get("transport", "gh")
        required = ["git", "gh"] if transport == "gh" else ["git"]
        missing = []
        for name in required:
            if name == "git":
                from mindie_knowledge.gitbin import GitUnavailableError, resolve_git
                try:
                    resolve_git()
                except GitUnavailableError:
                    missing.append(name)
            elif shutil.which(name) is None:
                missing.append(name)
        activity = self.operations.snapshot()
        from ..materials.summarizer import SummaryLedger
        with self.store._write_txn():
            ledger = SummaryLedger(self.store.db)
            summary_usage = ledger.usage_totals()
            summary_attempts = dict(self.store.db.execute(
                'SELECT status,count(*) FROM material_summary_attempts GROUP BY status').fetchall())
        return dict(
            **self.store.status(),
            capture_pipeline=self.capture_mode,
            summary_mode="required-model" if self.summary_command else "configuration-error",
            maintenance_pending=self.queue.unfinished_tasks,
            summary_usage=summary_usage,
            summary_attempts=summary_attempts,
            sharing=settings.public_status(),
            community_package=self.community is not None,
            publication_runtime=dict(
                state="unavailable" if missing else "executables-present",
                missing=missing, authentication="not-checked",
            ),
            errors=self.errors,
            background_errors=dict(self.background_errors),
            activity=activity['activity'],
            admission_frozen=activity['frozen'],
            worker_alive=self.thread.is_alive() and not self._worker_failed,
            worker_failed=bool(self._worker_failed),
            outbox_alive=self.outbox_thread.is_alive(),
            summary_alive=self.summary_thread.is_alive(),
        )
