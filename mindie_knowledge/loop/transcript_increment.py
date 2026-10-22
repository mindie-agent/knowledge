"""[Role] Incremental transcript cursor and capture-processing mixin for `Engine`.

[Invariants]
- A transcript cursor is reused only after its exact file identity (device,
  inode, anchor bytes) and one canonical path alias are proven.
- Capture processing revalidates settings, consent and the session lease before
  reading a new page or committing any material; revoked work is not replayed.
- Partial pages commit a durable continuation rather than re-reading old bytes.

[Timeouts & Locks]
- Runs inside the Engine's `OperationOwner` cancellation scope and the Store
  RLock; no new network or long-lived lock is acquired here.

[Side-Effect Policy]
- Reads native transcripts, writes capture cursors/material through the Store,
  and may invoke the existing redaction/summary paths. Never publishes remotely.
"""
from __future__ import annotations

import hashlib
import os
import queue
import time

from .activation import activation_epoch, AdmissionUnavailable as AdmissionUnreadable
from .process import MaintenanceCancelled
from .store import canonical, session_key
from ..filesystem import resolved_path

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

class TranscriptCursorError(ValueError):
    def __init__(self, code):
        detail = {
            "persisted_identity_invalid": "persisted transcript identity is unusable; not rereading",
            "transcript_cursor_alias_conflict": "multiple transcript cursor aliases exist; not rereading",
            "transcript_cursor_identity_mismatch": "saved transcript identity cannot be confirmed; not rereading",
            "transcript_unavailable": "transcript identity is unavailable; not treating it as first use",
        }
        super().__init__(detail[code])
        self.code = code

def _cursor_identity_matches(source, expected, identity):
    if (identity is None or expected.dev != identity.dev or expected.ino != identity.ino
            or not expected.anchor_len):
        return False
    if expected.anchor_len == identity.anchor_len:
        return expected.anchor_digest == identity.anchor_digest
    try:
        with open(resolved_path(source), "rb") as stream:
            info = os.fstat(stream.fileno())
            return (info.st_dev == expected.dev and info.st_ino == expected.ino
                    and hashlib.sha256(stream.read(expected.anchor_len)).hexdigest()
                    == expected.anchor_digest)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return False

def _path_aliases(source):
    """Resolve through Engine so its adapter/test seam stays authoritative."""
    from . import engine
    return engine.path_aliases(source)


def transcript_cursor(store, source, parser, identity):
    """Reuse one proven prior spelling; an alias never resets a saved cursor."""
    aliases = _path_aliases(source)
    existing = [(key, cursor) for key in aliases if (cursor := store.cursor(key)) is not None]
    if len(existing) > 1:
        raise TranscriptCursorError("transcript_cursor_alias_conflict")
    if not existing:
        if identity is None:
            raise TranscriptCursorError("transcript_unavailable")
        return aliases[0], None
    key, cursor = existing[0]
    expected = parser.FileIdentity.unserialize(
        cursor.get("identity") or "", identity.path if identity is not None else str(resolved_path(source)))
    if expected is None:
        raise TranscriptCursorError("persisted_identity_invalid")
    if not _cursor_identity_matches(source, expected, identity):
        raise TranscriptCursorError("transcript_cursor_identity_mismatch")
    return key, cursor


class TranscriptMixin:

    def capture(self, *, session_id, turn_id, transcript_path=None, summary="",
                cwd=None, harness=""):
        """Admit one Stop event into the capture table.

        Community off short-circuits before any row. The Stop hook commits
        through the same identity before it talks to this process.
        """
        settings = self._settings()
        if not settings.allows_capture():
            if settings.capture_block_kind() == 'fault':
                self._fault('knowledge.capture', 'authorization', 'authority_unavailable')
                return dict(status='failed', reason=settings.contribution_block_reason(),
                            failed_stage='authorization', error_code='authority_unavailable')
            return dict(status="skipped",
                        reason=settings.contribution_block_reason())
        if self.admission is None:
            self._fault('knowledge.capture', 'admission', 'admission_unconfigured')
            return dict(status="failed",
                        reason="no adapter admission is configured; identity unknown")
        lease = self.admission.active_lease(session_id)
        if lease is None:
            self._fault('knowledge.capture', 'admission', 'task_unbound')
            return dict(status="failed", reason="native task association is missing")
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
        self._fault('knowledge.capture', 'authorization', 'admission_unavailable')
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
        self._fault('knowledge.capture', 'authorization', 'authority_unavailable')
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
            self.store.mark_capture(row["id"], "failed", "no transcript adapter is configured",
                                    error_code='parser_unconfigured', failed_stage='configuration')
            return None
        boundary = row["boundary"] or settings.enabled_at
        identity = parser.identify(row["transcript"])
        try:
            key, cursor = transcript_cursor(self.store, row["transcript"], parser, identity)
        except TranscriptCursorError as exc:
            self.store.mark_capture(row["id"], "failed", str(exc),
                                    error_code=exc.code, failed_stage="identity")
            return None
        observed_cursor = None
        if cursor is not None:
            observed_cursor = {
                "identity": cursor.get("identity") or "",
                "finish": int(cursor["finish"]),
            }
        idtext = identity.serialize() if identity else ""

        def reserve(start, finish, rdigest, status="attempted", detail=""):
            return self.store.reserve_region(
                capture_id=row["id"], file_identity=key, identity=idtext,
                start=start, finish=finish, region_digest=rdigest,
                status=status, detail=detail, observed_cursor=observed_cursor,
            )

        if cursor is None and boundary is None:
            self.store.mark_capture(row["id"], "failed", "no reliable authorization boundary",
                                    error_code='authorization_boundary_missing', failed_stage='authorization')
            return None
        start = cursor["finish"] if cursor else 0
        expected = None
        if cursor:
            expected = parser.FileIdentity.unserialize(
                cursor["identity"], identity.path
            )
            if expected is None:
                # A missing/malformed persisted identity fails closed: the
                # segment is never reset to start=0 or read unsafely.
                self.store.mark_capture(
                    row["id"], "failed",
                    "persisted transcript identity is unusable; not rereading",
                    error_code='persisted_identity_invalid', failed_stage='identity',
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
            )), error_code='incomplete_projection')
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
                    error_code='unreliable_timestamps',
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
                error_code='unknown_transcript_format',
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
                    error_code='transcript_replaced', failed_stage='identity',
                )
                return None
            region["region_id"] = region_id
            self.store.mark_capture(row["id"], "failed", "transcript replaced or truncated",
                                    error_code='transcript_replaced', failed_stage='identity')
            return None
        if status == "wrong-task":
            self.store.mark_capture(row["id"], "failed",
                                    "transcript identity mismatch; not read",
                                    error_code='transcript_identity_mismatch', failed_stage='identity')
            return None
        if status in {
            "invalid-record", "invalid-boundary", "oversize",
        }:
            self.store.mark_capture(row["id"], "failed",
                                    "public transcript " + status + "; cursor unchanged",
                                    error_code='transcript_' + status.replace('-', '_'))
            return None
        self.store.mark_capture(row["id"], "failed", "transcript read failed: " + status,
                                error_code='transcript_read_failed')
        return None
    def _process(self, ident):
        row = self.store.capture_row(ident)
        if row is None or row["status"] not in {"queued", "pending", "deferred"}:
            return
        with self.operations.scope("capture", ident, row["generation"]) as operation:
            if operation is not None:
                self._process_capture(row, operation.cancel)
    def _process_capture(self, row, cancel):
        ident = row["id"]
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
            capture(self, row, text, region, cancel=cancel)
            self._clear_faults('knowledge.capture')
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
            self._fault('knowledge.capture', 'local-capture',
                        'committed_cleanup_failed' if getattr(exc, 'metadata_committed', False)
                        else 'capture_processing_failed', exc)
            if getattr(exc, "metadata_committed", False):
                # The body/cursor committed; cleanup is a separate visible fault.
                self.background_errors["material-cleanup"] = type(exc).__name__
                return
            if isinstance(exc, OSError) and not isinstance(exc, TimeoutError):
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
