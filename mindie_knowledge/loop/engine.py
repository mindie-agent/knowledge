"""Authorized public transcript capture, incremental indexing and publication.

Capture stores complete deterministically redacted material. The separate summary
worker indexes complete new blocks plus prior navigation using the bounded
LangMem protocol. Its durable ledger owns model outcomes and local result recovery;
this engine never rewrites a body or automatically replays a paid attempt.
Authority faults park work, revocation cancels unsent work, and uncertain remote
writes remain subject to read-only reconciliation.
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from . import settings as settings_mod
from .activation import activation_epoch, AdmissionUnavailable as AdmissionUnreadable
from .budget import BudgetExceeded
from .dfx import failure
from .process import MaintenanceCancelled
from .store import Store, canonical, session_key

Store_confirmed = Store.CONFIRMED_BATCH

_EOF_SETTLE_LIMIT = 3
_PARTIAL_LIMIT = 4


class GateFault(Exception):
    """The settings/consent authority is in an unknown state (missing,
    unreadable, corrupt or malformed). This is not a revocation: no new
    read/model/write happens, and durably received work or saved results are
    parked with a bounded backoff — never cancelled, never dropped — until
    the authority is restored and revalidation passes again."""


class CursorConflict(Exception):
    """The shared cursor moved. Do not call the model or count a failure."""


class Engine:
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
        self._cancel = threading.Event()
        self._summary_cancel = threading.Event()
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
        self._generation = None
        self._worker_failed = False
        self._activity_lock = threading.Lock()
        self._activity = 0
        self._frozen = False
        # Publication belongs to this package. A broken installation is a
        # startup failure, not a service with silently missing capabilities.
        from mindie_knowledge.community import reconcile_batch, submit_batch
        self.community = dict(submit_batch=submit_batch, reconcile_batch=reconcile_batch)

    # -------------------------------------------------------------- settings

    def _settings(self):
        return settings_mod.load(self.settings_path)

    def _error(self, detail):
        self.errors = (self.errors + [detail])[-20:]

    def _unexpected(self, operation, stage, exc):
        """Report an unhandled internal error. Expected cancellation, budget,
        config/caller, authority-fault, and community business errors are not
        incidents."""
        if isinstance(exc, (MaintenanceCancelled, AdmissionUnreadable, CursorConflict,
                            GateFault, BudgetExceeded, OSError, ValueError, TypeError)):
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

    # --------------------------------------------------------------- capture

    def capture(self, *, session_id, turn_id, transcript_path=None, summary="",
                cwd=None, harness=""):
        """Admit one Stop event into the capture table.

        Community off short-circuits before any row. The Stop hook commits
        through the same identity before it talks to this process.
        """
        settings = self._settings()
        if not settings.allows_capture():
            return dict(status="skipped",
                        reason=settings.contribution_block_reason())
        if self.admission is None:
            return dict(status="skipped",
                        reason="no adapter admission is configured; identity unknown")
        lease = self.admission.active_lease(session_id)
        if lease is None:
            return dict(status="skipped", reason="session is not bound; invoke the entry once")
        if not lease.get("capture_schema"):
            return dict(status="skipped",
                        reason="adapter lease store predates the capture schema")
        scope = self.admission.scope_root(session_id)
        if not scope or not settings.in_scope(scope):
            return dict(status="skipped",
                        reason="the lease's project root is outside the authorized scope")
        root_session = lease.get("root_session") or session_id
        root_hash = session_key(root_session)
        activated_at = lease.get("activated_at")
        boundary = max(
            settings.enabled_at,
            activated_at if type(activated_at) in (int, float) else 0,
            self.store.capture_floor,
        )
        if self._is_frozen():
            return dict(status="skipped",
                        reason="service is not admitting new work")
        namespace = harness if isinstance(harness, str) else ""
        if not transcript_path:
            return dict(status="skipped", reason="public transcript reference is required")
        # The native transcript is the only raw source; the queue stores no body.
        summary = ""
        captured = self.store.add_capture(
            root_session=root_hash, session=session_id, turn=turn_id,
            transcript=transcript_path, summary=summary or "",
            generation=settings.generation, boundary=boundary, scope=scope,
            namespace=namespace,
            activation_epoch=activation_epoch(lease["token"]),
        )
        if captured.get("revoked"):
            return captured
        if captured["status"] in {"queued", "pending", "deferred"}:
            try:
                self.queue.put_nowait(captured["id"])
            except queue.Full:
                self.store.defer_capture(
                    captured["id"], due=time.time() + 1,
                    reason="bounded memory queue full; persisted for later scan",
                )
            self.last_activity = time.monotonic()
        return captured

    # -------------------------------------------------------------- capture gates

    def _gate_live(self):
        if self.stop.is_set():
            raise MaintenanceCancelled("service is stopping")
        block = self._settings().capture_block_kind()
        if block == "fault":
            raise GateFault("consent/settings authority is unavailable")
        if block == "revoked":
            raise MaintenanceCancelled("sharing disabled before model spawn")

    def _revalidate(self, row):
        """The capture's persisted authorization must still hold exactly:
        same settings generation, live lease, unchanged authorized scope.
        Anything else is a revocation — the material never reaches a child.
        An unknown authority state is a GateFault, not a revocation: the
        material stays parked and the saved result is never dropped."""
        settings = self._settings()
        block = settings.capture_block_kind()
        if block == "fault":
            raise GateFault("consent/settings authority is unavailable")
        if block == "revoked":
            raise MaintenanceCancelled("sharing disabled")
        if row["generation"] is not None and settings.generation != row["generation"]:
            raise MaintenanceCancelled("settings generation changed since admission")
        if self.admission is not None:
            lease = self.admission.active_lease(row["session"])
            if lease is None:
                inspected = self.admission.inspect(row["session"])
                if inspected.get("status") == "unavailable":
                    raise AdmissionUnreadable("admission unreadable; not reading transcript")
                raise MaintenanceCancelled("task deactivated")
            epoch = row["activation_epoch"] if "activation_epoch" in row.keys() else None
            if not epoch or activation_epoch(lease["token"]) != epoch:
                raise MaintenanceCancelled("activation epoch does not match this lease")
            scope = self.admission.scope_root(row["session"])
            if row["scope"] and scope != row["scope"]:
                raise MaintenanceCancelled("authorized project scope changed")
            if row["scope"] and not settings.in_scope(row["scope"]):
                raise MaintenanceCancelled("project scope is no longer allowed")
        return settings


    def _defer_counted(self, ident, prefix, limit, *, dormant_reason=None, terminal=None):
        """Finite model-free waits. The last state is dormant or a terminal status."""
        reason = self.store.continuation_reason(ident) or ""
        try:
            count = int(reason.split(":", 1)[1]) if reason.startswith(prefix + ":") else 0
        except ValueError:
            count = 0
        count += 1
        if count > limit:
            if dormant_reason:
                self.store.dormant_capture(ident, reason=dormant_reason)
            elif terminal:
                terminal()
            return
        self.store.defer_capture(
            ident, due=time.time() + min(8, 2 ** (count - 1)),
            reason=f"{prefix}:{count}", eligible=1,
        )

    def _defer_partial(self, ident):
        """An unfinished tail is not an empty capture. Automatic waits are finite."""
        reason = self.store.continuation_reason(ident) or ""
        if reason == "incomplete-tail":
            self.store.dormant_capture(ident, reason="incomplete-tail")
            return
        self._defer_counted(
            ident, "partial-tail", _PARTIAL_LIMIT, dormant_reason="incomplete-tail",
        )

    def _defer_eof(self, ident):
        """A clean EOF may still be unflushed. Recheck a few times, with no model."""
        self._defer_counted(
            ident, "eof-settle", _EOF_SETTLE_LIMIT,
            terminal=lambda: self.store.finish_capture_eof(ident),
        )

    def _defer_admission(self, ident):
        reason = self.store.continuation_reason(ident) or ""
        if reason == "admission-unreadable":
            self.store.dormant_capture(ident, reason="admission-unreadable")
            return
        self._defer_counted(
            ident, "admission-unreadable", _PARTIAL_LIMIT,
            dormant_reason="admission-unreadable",
        )

    def _defer_gate_fault(self, ident):
        """Park durably received work while the settings/consent authority is
        in an unknown state. Nothing is cancelled and no saved result is
        dropped: the capture stays pending with a persisted, capped backoff
        and resumes once the authority is restored and revalidation passes.
        """
        reason = self.store.continuation_reason(ident) or ""
        try:
            count = (
                int(reason.split(":", 1)[1])
                if reason.startswith("gate-fault:")
                else 0
            )
        except ValueError:
            count = 0
        count += 1
        self.store.defer_capture(
            ident,
            due=time.time() + min(3600.0, 30.0 * 2.0 ** min(count - 1, 6)),
            reason=f"gate-fault:{count}", eligible=1,
        )

    def _defer_reread(self, ident):
        """Finite wait after a cursor conflict. Not an immediate busy loop."""
        reason = self.store.continuation_reason(ident) or ""
        if reason == "cursor-conflict":
            self.store.dormant_capture(ident, reason="cursor-conflict")
            return
        self._defer_counted(
            ident, "cursor-conflict", _PARTIAL_LIMIT, dormant_reason="cursor-conflict",
        )


    def _transcript_increment(self, row, settings, lease, region):
        """Read/filter the authorized increment; reserves regions as it goes.
        Returns complete public text or None when there is no material."""
        parser = self.transcript
        if parser is None:
            self.store.mark_capture(row["id"], "failed", "no transcript adapter is configured")
            return None
        boundary = row["boundary"] or settings.enabled_at
        key = str(Path(row["transcript"]).resolve(strict=False))
        cursor = self.store.cursor(key)
        observed_cursor = None
        if cursor is not None:
            observed_cursor = {
                "identity": cursor.get("identity") or "",
                "finish": int(cursor["finish"]),
            }
        identity = parser.identify(row["transcript"])
        idtext = identity.serialize() if identity else ""

        def reserve(start, finish, rdigest, status="attempted", detail=""):
            return self.store.reserve_region(
                capture_id=row["id"], file_identity=key, identity=idtext,
                start=start, finish=finish, region_digest=rdigest,
                status=status, detail=detail, observed_cursor=observed_cursor,
            )

        if cursor is None and boundary is None:
            self.store.mark_capture(row["id"], "failed", "no reliable authorization boundary")
            return None
        start = cursor["finish"] if cursor else 0
        expected = None
        if cursor:
            expected = parser.FileIdentity.unserialize(
                cursor["identity"], key
            )
            if expected is None:
                # A missing/malformed persisted identity fails closed: the
                # segment is never reset to start=0 or read unsafely.
                self.store.mark_capture(
                    row["id"], "failed",
                    "persisted transcript identity is unusable; not rereading",
                )
                return None
        inc = parser.read_material(
            row["transcript"], start, session_id=row["session"],
            not_before=boundary, expected=expected,
        )
        idtext = inc.get("identity", idtext)
        status = inc["status"]
        region.update(inc=inc, key=key, reserve=reserve)
        if inc.get("discarded_records"):
            self.store.mark_capture(row["id"], "failed", canonical(dict(
                error="incomplete transcript page; cursor unchanged",
                records=inc["discarded_records"],
            )))
            return None
        if status == "ok" and inc["text"].strip():
            if not inc.get("timestamps_reliable", True):
                region_id = reserve(inc["start"], inc["end"], inc["digest"], status="failed")
                if region_id is None:
                    self._defer_reread(row["id"])
                    return None
                self.store.finish_region(
                    region_id, "failed", "unreliable transcript timestamps",
                )
                self.store.mark_capture(
                    row["id"], "failed", "unreliable transcript timestamps",
                )
                return None
            return inc["text"]
        if inc.get("partial") and inc["end"] == start:
            self._defer_partial(row["id"])
            return None
        if status in {"ok", "unchanged"} and inc["end"] == start:
            # Nothing new was observed. A clean EOF can still precede a flush.
            if inc.get("more"):
                self.store.defer_capture(
                    row["id"], due=time.time() + 1,
                    reason="no new bytes in this page; more authorized bytes remain",
                )
            else:
                self._defer_eof(row["id"])
            return None
        if status in {"ok", "unchanged"}:
            if status == "ok" and inc["end"] > inc["start"]:
                # Consumed bytes held no public material; consume them visibly.
                detail = canonical(dict(note="no public material",
                                        discarded_records=inc.get('discarded_records', [])))
                region_id = reserve(inc["start"], inc["end"], inc["digest"],
                                    status="succeeded", detail=detail)
                if region_id is None:
                    self._defer_reread(row["id"])
                    return None
                self.store.finish_region(region_id, "succeeded", detail)
            if inc.get("more") and inc["end"] > inc["start"]:
                self.store.defer_capture(row["id"], due=time.time()+1,
                                         reason="noise scanned; more authorized bytes remain")
            else:
                self._defer_eof(row["id"])
            return None
        established = (
            cursor is not None
            or inc.get("session_match") is True
            or inc.get("format_established") is True
        )
        if (
            status == "unknown-format"
            and established
            and inc.get("more")
            and inc["end"] > inc["start"]
        ):
            # Reliable native identity is enough. start==0 and a missing cursor
            # row do not make this a permanent format failure. A positive
            # offset by itself is not that evidence.
            region_id = reserve(
                inc["start"], inc["end"], inc["digest"], status="succeeded",
                detail="unrecognized page; more bytes remain",
            )
            if region_id is None:
                self._defer_reread(row["id"])
                return None
            self.store.finish_region(region_id, "succeeded")
            self.store.defer_capture(
                row["id"], due=time.time()+1,
                reason="unrecognized page; more authorized bytes remain",
            )
            return None
        if status == "unknown-format":
            region_id = reserve(inc["start"], inc["end"], inc["digest"])
            if region_id is None:
                self._defer_reread(row["id"])
                return None
            region["region_id"] = region_id
            self.store.finish_region(
                region_id, "failed", "unknown transcript format",
            )
            self.store.mark_capture(
                row["id"], "failed", "unknown transcript format",
            )
            return None
        if status == "replaced":
            size = identity.size if identity else 0
            region_id = reserve(
                0, size, "0" * 64, status="failed",
                detail="transcript replaced or truncated; segment parsing stopped",
            )
            if region_id is None:
                self.store.mark_capture(
                    row["id"], "failed", "transcript replaced; cursor not regressed",
                )
                return None
            region["region_id"] = region_id
            self.store.mark_capture(row["id"], "failed", "transcript replaced or truncated")
            return None
        if status == "wrong-task":
            self.store.mark_capture(row["id"], "failed",
                                    "transcript identity mismatch; not read")
            return None
        if status in {
            "invalid-record", "invalid-boundary", "oversize",
        }:
            self.store.mark_capture(row["id"], "failed",
                                    "public transcript " + status + "; cursor unchanged")
            return None
        self.store.mark_capture(row["id"], "failed", "transcript read failed: " + status)
        return None


    def _process(self, ident):
        row = self.store.capture_row(ident)
        if row is None or row["status"] not in {"queued", "pending", "deferred"}:
            return
        region = {}
        try:
            settings = self._revalidate(row)
            lease = self.admission.active_lease(row["session"]) if self.admission else None
            if not row["transcript"]:
                raise ValueError("public transcript reference is required; summary fallback is forbidden")
            if not self.redactor_executable:
                raise ValueError("public transcript capture requires an installed redactor")
            text = self._transcript_increment(row, settings, lease, region)
            if text is None:
                return
            from .transcript_capture import capture
            capture(self, row, text, region)
        except CursorConflict:
            self._defer_reread(ident)
        except AdmissionUnreadable:
            self._defer_admission(ident)
        except MaintenanceCancelled:
            if self.stop.is_set():
                self.store.defer_capture(ident, due=time.time(),
                                         reason="service stopped; local work retained")
            else:
                self.store.mark_capture(ident, "cancelled", "capture authority revoked")
        except GateFault:
            self._defer_gate_fault(ident)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"[:1000]
            self._error(detail)
            if getattr(exc, "metadata_committed", False):
                # The body/cursor committed; cleanup is a separate visible fault.
                self.background_errors["material-cleanup"] = type(exc).__name__
                return
            if isinstance(exc, OSError):
                # Retry only deterministic local work whose transaction failed.
                previous = self.store.continuation_reason(ident) or ""
                try:
                    attempt = int(previous.split(":")[1]) if previous.startswith("public-io:") else 0
                except (ValueError, IndexError):
                    attempt = 0
                attempt += 1
                self.store.defer_capture(ident, due=time.time() + min(300, 2 ** min(attempt, 8)),
                                         reason=f"public-io:{attempt}:{type(exc).__name__}")
                return
            self._unexpected("knowledge.capture", "process", exc)
            if region.get("region_id"):
                self.store.finish_region(region["region_id"], "failed", detail)
            self.store.mark_capture(ident, "failed", detail)

    # ---------------------------------------------------------------- worker


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
            self.store.disable_foreign_pending(settings.generation)
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
                    if ident and self.begin_work():
                        try:
                            self._process(ident)
                        finally:
                            self.end_work()
                except (MaintenanceCancelled, AdmissionUnreadable, CursorConflict,
                        GateFault):
                    continue
                except Exception as exc:
                    self._unexpected("knowledge.worker", "run", exc)
                    # Stop this thread and leave rows durable. Do not count
                    # errors or start another worker inside the loop.
                    self._worker_failed = True
                    return
                continue
            try:
                if ident and self.begin_work():
                    try:
                        self._process(ident)
                    finally:
                        self.end_work()
            except (MaintenanceCancelled, AdmissionUnreadable, CursorConflict,
                    GateFault):
                pass
            except Exception as exc:  # never let the worker die silently
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
                self._unexpected("knowledge.summary", "run", exc)
                self.background_errors["summary"] = type(exc).__name__
                self._error(f"summary worker stopped: {type(exc).__name__}")
                return

    # ---------------------------------------------------------------- outbox

    def _cancel_unsent(self, reason):
        """Sharing revocation cancels pending timers and unsent contributions."""
        self._cancel.set()
        self._summary_cancel.set()
        self.store.cancel_pending(reason)
        with self.store._write_txn():
            self.store.db.execute(
                "UPDATE votes SET publishable=0 WHERE batch_id IS NULL"
            )

    def _submit(self, batch_row):
        settings = self._settings()
        if not settings.allows_capture():
            return
        if batch_row["generation"] != settings.generation:
            self.store.mark_batch(
                batch_row["batch_id"], "disabled",
                detail="settings generation changed before send",
            )
            return
        if self.community is None:
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
            self.store.mark_batch(batch_row['batch_id'], 'unavailable',
                                  detail='local frozen contribution could not be loaded: ' + type(exc).__name__)
            self._error('Publication did not start: frozen contribution load failed (' + type(exc).__name__ + ').')
            return
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
            receipt = self.community["submit_batch"](
                batch, settings.as_dict(), self.state_dir, cancel=self._cancel
            )
        except Exception as exc:
            self._unexpected("knowledge.publish", "submit", exc)
            self.store.mark_batch(
                batch_row["batch_id"], "unknown", attempted=True,
                detail=f"{type(exc).__name__}: {exc}"[:500],
            )
            return
        self.store.mark_batch(
            batch_row["batch_id"], receipt.get("status", "unknown"),
            attempted=True, detail=receipt.get("detail", ""),
            pr_url=receipt.get("pr_url"), head_sha=receipt.get("head_sha"),
            actual_files=receipt.get("files"), retry_at=receipt.get("retry_at"),
        )
        if receipt.get("status") in Store_confirmed:
            # Confirmed upload retires staging. The candidate remains readable
            # until this revision is confirmed in the synchronized public feed.
            cleanup = self.store.compact_confirmed(batch_row["batch_id"])
            if cleanup.get('cleanup_status') == 'failed':
                self._error('Publication confirmed; local cleanup failed: ' + cleanup.get('cleanup_error', 'unknown'))

    def _reconcile(self, batch_row):
        if self.community is None:
            return  # stay unresolved; the dependency failure is reported on submit
        try:
            receipt = self.community["reconcile_batch"](
                batch_row["batch_id"], self._settings().as_dict(), self.state_dir
            )
        except Exception as exc:
            self._unexpected("knowledge.publish", "reconcile", exc)
            self.store.mark_batch(batch_row["batch_id"], "unknown",
                                  detail=f"reconciliation failed: {type(exc).__name__}")
            self._error(f"publication reconciliation failed: {type(exc).__name__}")
            return
        self.store.mark_batch(
            batch_row["batch_id"], receipt.get("status", "unknown"),
            detail=receipt.get("detail", ""), pr_url=receipt.get("pr_url"),
            head_sha=receipt.get("head_sha"),
            actual_files=receipt.get("files"), retry_at=receipt.get("retry_at"),
        )
        if receipt.get("status") in Store_confirmed:
            cleanup = self.store.compact_confirmed(batch_row["batch_id"])
            if cleanup.get('cleanup_status') == 'failed':
                self._error('Publication confirmed; local cleanup failed: ' + cleanup.get('cleanup_error', 'unknown'))

    def _flush(self):
        """Coalesce all pending material into one batch and send it."""
        from .export import build_batch
        from ..materials.publication import CleanupReceiptError

        settings = self._settings()
        if not settings.allows_capture():
            return
        try:
            built = build_batch(self.store, settings=settings)
        except CleanupReceiptError as exc:
            batch_id = exc.batch_id
            self._error("New contribution staged; prior staging cleanup receipt failed: " + str(exc))
        else:
            if built is None:
                return
            batch_id, _revision, _batch, _ids, _votes = built
            cleanup = self.store.feed_get("publication-cleanup:" + batch_id)
            if cleanup and cleanup.get("status") == "failed":
                self._error("New contribution staged; prior staging cleanup failed: " + cleanup.get("detail", "unknown"))
        self._submit(self.store.batch(batch_id))

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
                    pass
                elif self._generation is not None and generation != self._generation:
                    if generation is None:
                        self._cancel_unsent("sharing disabled; unsent work cancelled")
                    else:
                        self._cancel_unsent(
                            "settings generation changed; unsent work cancelled"
                        )
                        self._cancel.clear()
                if block != "fault":
                    self._generation = generation
                # ReMe refreshes on query or an explicit index operation. The
                # retired FTS scheduler must not import/index a whole library
                # under the state lock during capture or service startup.
                if generation is not None and not self._is_frozen():
                    for row in self.store.outbox_unresolved(limit=2, due_only=True):
                        if self._is_frozen():
                            break
                        if self.store.reconcile_due(row["batch_id"]) and self.begin_work():
                            try:
                                self._reconcile(row)
                            finally:
                                self.end_work()
                    # Transient send failures resume automatically with
                    # persisted backoff: the stored batch payload is retried,
                    # never rebuilt from scratch and never silently dropped.
                    for row in self.store.outbox_unavailable(limit=2, due_only=True):
                        if self._is_frozen():
                            break
                        if self.store.retry_due(row["batch_id"]) and self.begin_work():
                            try:
                                self._submit(row)
                            finally:
                                self.end_work()
                    for row in self.store.outbox_pending(limit=2):
                        if self._is_frozen():
                            break
                        if self.begin_work():
                            try:
                                self._submit(row)
                            finally:
                                self.end_work()
                    material = self.store.has_changed_drafts(
                        generation=generation, ready_only=True
                    ) or self.store.unbatched_votes(generation=generation)
                    if material:
                        idle_for = time.monotonic() - self.last_activity
                        deactivated = (
                            self.admission is not None
                            and not self.admission.leases()
                        )
                        if (
                            (idle_for >= settings.idle_seconds or deactivated)
                            and not self._is_frozen()
                            and self.begin_work()
                        ):
                            try:
                                self._flush()
                            finally:
                                self.end_work()
            except Exception as exc:
                self._unexpected("knowledge.outbox", "tick", exc)
                self._error(f"outbox: {type(exc).__name__}: {exc}"[:500])
            self.stop.wait(1.0)

    def shutdown(self):
        self.stop.set()
        self._cancel.set()
        self._summary_cancel.set()
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass
        self.thread.join(timeout=10)
        self.outbox_thread.join(timeout=5)
        if self.summary_thread.ident is not None:
            self.summary_thread.join(timeout=10)

    # ---------------------------------------------------------------- status

    def begin_work(self):
        """Admit one actual service/worker operation. False when frozen or stopping."""
        with self._activity_lock:
            if self._frozen or self.stop.is_set():
                return False
            self._activity += 1
            return True

    def end_work(self):
        with self._activity_lock:
            if self._activity > 0:
                self._activity -= 1

    def _is_frozen(self):
        with self._activity_lock:
            return self._frozen or self.stop.is_set()

    def stop_if_idle(self):
        """Atomically freeze new admission and report whether anything is
        actually running or queued. Unknown/pending receipts and idle task
        grants are not activity. Never cancels in-flight work: a busy result
        unfreezes so the service continues."""
        with self._activity_lock:
            if self.stop.is_set():
                return dict(
                    idle=True, status="stopping", activity=self._activity,
                    queued_captures=self.queue.unfinished_tasks,
                    due_capture=False,
                )
            self._frozen = True
            activity = self._activity
            queued = self.queue.unfinished_tasks
        due = self.store.due_capture() is not None
        with self.store.lock:
            due_summary = self.store.db.execute(
                "SELECT 1 FROM material_batches b JOIN transcript_tasks t ON t.entry_id=b.entry_id "
                "WHERE b.status IN ('pending','retry-requested') AND t.summary_due<=? LIMIT 1",
                (time.time(),)).fetchone() is not None
        busy = activity > 0 or queued > 0 or due or due_summary
        if busy:
            with self._activity_lock:
                if not self.stop.is_set():
                    self._frozen = False
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
        missing = [name for name in required if shutil.which(name) is None]
        with self._activity_lock:
            activity = self._activity
            frozen = self._frozen
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
            summary_budget=settings.as_dict().get('summary_budget', dict(calls_per_hour=32, input_tokens_per_hour=512000)),
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
            activity=activity,
            admission_frozen=frozen,
            worker_alive=self.thread.is_alive() and not self._worker_failed,
            worker_failed=bool(self._worker_failed),
            outbox_alive=self.outbox_thread.is_alive(),
            summary_alive=self.summary_thread.is_alive(),
        )
