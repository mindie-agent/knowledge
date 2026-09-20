"""Bounded background organization with a configured agent runner.

One organizer role, no judge: the runner receives the filtered, already
redaction-masked increment plus compact draft headers and returns at most
three entries; the first content of an entry is its detailed case body and
later content is appended as a marker-deduplicated, self-contained
observation. The input region is durably reserved BEFORE the model spawns,
so every outcome — failure, crash, cancellation — consumes it; attempted and
successful cursors are separate and failed regions stay visible as coverage
gaps. Nothing here replays failed work.

Sharing revocation is live: the outbox/idle thread rereads the shared
settings file about once a second; disabling (or replacing) the configuration
cancels in-flight model work, pending batch timers and unsent contributions,
and never backfills the disabled period after re-enable.
"""

from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from pathlib import Path

from mindie_knowledge.redact import scan_text

from . import settings as settings_mod
from . import transcript as transcript_mod
from .budget import BudgetExceeded, MaintenanceBudget
from .documents import DraftFull
from .process import MaintenanceCancelled, bounded_run
from .store import canonical, digest, session_key

ORGANIZE_FIELDS = {"entry_id", "title", "summary", "conditions", "content", "sources"}
MAX_STRUCTURED_RESULT = 32 * 1024
MAX_INPUT = 64 * 1024
SUMMARY_FIELD = 4096


def mask_text(text):
    """Deterministic pre-model privacy filter; returns (masked, rules)."""
    findings = scan_text(text)
    for finding in findings:
        if finding.value:
            text = text.replace(finding.value, f"[redacted:{finding.rule}]")
    return text, sorted({finding.rule for finding in findings})


class Engine:
    def __init__(self, store, *, agent_command=None, settings_path=None,
                 admission=None, state_dir=None):
        if agent_command is not None and (
            not isinstance(agent_command, list)
            or not agent_command
            or not all(isinstance(x, str) for x in agent_command)
        ):
            raise ValueError("agent_command must be a nonempty argv list or None")
        self.store = store
        self.agent_command = agent_command
        self.settings_path = settings_path
        self.admission = admission
        self.state_dir = Path(state_dir) if state_dir else store.root / "outbox"
        self.budget = MaintenanceBudget(store)
        self.queue = queue.Queue(maxsize=8)
        self.stop = threading.Event()
        self._cancel = threading.Event()
        self.thread = threading.Thread(
            target=self.run, name="mindie-maintenance", daemon=True
        )
        self.outbox_thread = threading.Thread(
            target=self._outbox_loop, name="mindie-outbox", daemon=True
        )
        self.errors = []
        self.last_activity = time.monotonic()
        self._generation = None
        try:
            from mindie_knowledge.community import reconcile_batch, submit_batch

            self.community = dict(submit_batch=submit_batch,
                                  reconcile_batch=reconcile_batch)
        except Exception:
            self.community = None

    # -------------------------------------------------------------- settings

    def _settings(self):
        return settings_mod.load(self.settings_path)

    def _error(self, detail):
        self.errors = (self.errors + [detail])[-20:]

    # --------------------------------------------------------------- capture

    def capture(self, *, session_id, turn_id, transcript_path=None, summary="",
                cwd=None):
        """Admission gate for one Stop event. Community off short-circuits
        before any row, cursor, draft, worker or model exists."""
        settings = self._settings()
        if not settings.allows_capture():
            return dict(status="skipped",
                        reason="community contribution is disabled or unconfigured")
        if self.admission is None:
            return dict(status="skipped",
                        reason="no adapter admission is configured; identity unknown")
        lease = self.admission.active_lease(session_id)
        if lease is None:
            return dict(status="skipped", reason="session is not manually active")
        if not lease.get("capture_schema"):
            return dict(status="skipped",
                        reason="adapter lease store predates the capture schema")
        scope = self.admission.scope_root(session_id)
        if not scope or not settings.in_scope(scope):
            return dict(status="skipped",
                        reason="the lease's project root is outside the authorized scope")
        if self.budget.status()["paused"]:
            return dict(status="discarded", reason="maintenance circuit paused")
        root_session = lease.get("root_session") or session_id
        root_hash = session_key(root_session)
        captured = self.store.add_capture(
            root_session=root_hash, session=session_id, turn=turn_id,
            transcript=transcript_path, summary=summary or "",
        )
        if captured["duplicate"]:
            return captured
        try:
            self.queue.put_nowait(captured["id"])
        except queue.Full:
            self.store.mark_capture(
                captured["id"], "discarded", "maintenance queue full"
            )
            return dict(status="discarded", reason="maintenance queue full")
        self.last_activity = time.monotonic()
        return captured

    # -------------------------------------------------------------- organize

    def _gate_live(self):
        if self.stop.is_set():
            raise MaintenanceCancelled("service is stopping")
        if not self._settings().allows_capture():
            raise MaintenanceCancelled("sharing disabled before model spawn")

    def agent(self, payload, *, attempt_id, root_hash):
        raw = canonical(payload)
        if len(raw.encode("utf-8")) > MAX_INPUT:
            raise ValueError("maintenance input exceeds limit")
        self._cancel.clear()
        self.budget.reserve(attempt_id, root_hash, payload.get("role", "organize"))
        outcome = False
        try:
            self._gate_live()
            output = bounded_run(
                self.agent_command, raw, timeout=65, max_output=131072,
                cancel=self._cancel,
            )
            result = json.loads(output)
            if not isinstance(result, dict):
                raise ValueError("agent must return one JSON object")
            if len(canonical(result).encode("utf-8")) > MAX_STRUCTURED_RESULT:
                raise ValueError("structured result exceeds the 32 KiB limit")
            self._validate_organize(result)
            outcome = True
            return result
        except MaintenanceCancelled:
            outcome = None
            raise
        finally:
            self.budget.finish(attempt_id, outcome)

    @staticmethod
    def _validate_organize(result):
        if set(result) != {"entries"} or not isinstance(result["entries"], list):
            raise ValueError("invalid organizer result")
        if len(result["entries"]) > 3:
            raise ValueError("at most three entries per call")
        for entry in result["entries"]:
            if not isinstance(entry, dict) or not set(entry) <= ORGANIZE_FIELDS:
                raise ValueError("invalid organized entry fields")
            ident = entry.get("entry_id")
            if ident is not None and not isinstance(ident, str):
                raise ValueError("entry_id must be null or an identity string")
            for name in ("title", "summary", "content"):
                if not isinstance(entry.get(name), str) or not entry[name].strip():
                    raise ValueError(f"organized entry requires nonempty {name}")
            if not isinstance(entry.get("conditions", {}), dict):
                raise ValueError("conditions must be an object")
            if not isinstance(entry.get("sources", []), list):
                raise ValueError("sources must be a list")

    def _transcript_increment(self, row, settings, lease, region):
        """Read/filter the authorized increment; reserves regions as it goes.
        Returns (text, summary_only, notes) or None when there is no material."""
        boundary = settings.enabled_at or (lease or {}).get("activated_at")
        key = str(Path(row["transcript"]).resolve(strict=False))
        cursor = self.store.cursor(key)
        identity = transcript_mod.identify(row["transcript"])
        idtext = f"{identity.dev}:{identity.ino}" if identity else ""

        def reserve(start, finish, rdigest, status="attempted", detail=""):
            return self.store.reserve_region(
                capture_id=row["id"], file_identity=key, identity=idtext,
                start=start, finish=finish, region_digest=rdigest,
                status=status, detail=detail,
            )

        if cursor is None and boundary is None:
            # No reliable authorization boundary: never backfill history.
            return ("", True, ["no reliable authorization boundary; summary-only"])
        start = cursor["finish"] if cursor else 0
        expected = None
        if cursor and cursor.get("identity"):
            dev, _, ino = cursor["identity"].partition(":")
            try:
                expected = transcript_mod.FileIdentity(key, int(dev or 0),
                                                       int(ino or 0), 0, 0)
            except ValueError:
                expected = None
        inc = transcript_mod.read_increment(
            row["transcript"], start, session_id=row["session"],
            not_before=boundary, expected=expected,
        )
        status = inc["status"]
        region.update(inc=inc, key=key, reserve=reserve)
        if status == "ok" and inc["text"].strip():
            region["region_id"] = reserve(inc["start"], inc["end"], inc["digest"])
            notes = []
            if not inc.get("timestamps_reliable", True):
                notes.append("some records lack usable timestamps")
            return (inc["text"], False, notes)
        if status in {"ok", "unchanged"}:
            if status == "ok" and inc["end"] > inc["start"]:
                # Consumed bytes held no public material; consume them visibly.
                region_id = reserve(inc["start"], inc["end"], inc["digest"],
                                    status="succeeded", detail="no public material")
                self.store.finish_region(region_id, "succeeded")
            if row["summary"].strip():
                return ("", True, ["no new public transcript material; summary-only"])
            self.store.mark_capture(row["id"], "no-new-material")
            return None
        if status == "unknown-format":
            region["region_id"] = reserve(inc["start"], inc["end"], inc["digest"])
            if row["summary"].strip():
                return ("", True, ["unknown transcript format; summary-only"])
            self.store.finish_region(region["region_id"], "failed",
                                     "unknown transcript format and no summary")
            self.store.mark_capture(row["id"], "failed",
                                    "unknown transcript format and no summary")
            return None
        if status == "replaced":
            size = identity.size if identity else 0
            region["region_id"] = reserve(
                0, size, "0" * 64, status="failed",
                detail="transcript replaced or truncated; segment parsing stopped",
            )
            if row["summary"].strip():
                return ("", True, ["transcript replaced; summary-only"])
            self.store.mark_capture(row["id"], "failed",
                                    "transcript replaced and no summary")
            return None
        if status == "wrong-task":
            self.store.mark_capture(row["id"], "failed",
                                    "transcript identity mismatch; not read")
            return None
        # missing/unreadable transcript
        if row["summary"].strip():
            return ("", True, ["transcript unreadable; summary-only"])
        self.store.mark_capture(row["id"], "failed",
                                "transcript unreadable and no summary")
        return None

    def _apply(self, result, *, opaque, marker):
        """Deterministic metadata update + append; no repair model call."""
        refs, notes = [], []
        for entry in result["entries"]:
            candidate = canonical({k: entry.get(k) for k in sorted(ORGANIZE_FIELDS)})
            findings = scan_text(candidate)
            if findings:
                rules = ", ".join(sorted({f.rule for f in findings}))
                notes.append(f"entry kept local; outbound scan: {rules}")
                continue
            try:
                ident = entry.get("entry_id")
                if ident:
                    doc, _appended = self.store.append_observation(
                        ident, entry["content"], marker=marker, producer=opaque
                    )
                else:
                    doc = self.store.create_draft(
                        kind="experience", title=entry["title"],
                        summary=entry["summary"], content=entry["content"],
                        conditions=entry.get("conditions") or {},
                        sources=entry.get("sources") or [], producers=[opaque],
                    )
                refs.append(self.store.ref(doc["entry_id"]))
            except DraftFull:
                notes.append(f"draft full: {entry.get('entry_id')}")
            except ValueError as exc:
                notes.append(str(exc)[:200])
        return refs, notes

    def _process(self, ident):
        row = self.store.capture_row(ident)
        if row is None:
            return
        settings = self._settings()
        if not settings.allows_capture():
            self.store.mark_capture(ident, "cancelled", "sharing disabled while queued")
            return
        lease = self.admission.active_lease(row["session"]) if self.admission else None
        if self.admission is not None and lease is None:
            self.store.mark_capture(ident, "cancelled", "task deactivated while queued")
            return
        if not self.agent_command:
            self.store.mark_capture(
                ident, "discarded", "no maintenance runner is configured"
            )
            return
        region = {}
        try:
            if row["transcript"]:
                outcome = self._transcript_increment(row, settings, lease, region)
            elif row["summary"].strip():
                outcome = ("", True, [])
            else:
                self.store.mark_capture(ident, "no-new-material")
                return
            if outcome is None:
                return
            text, summary_only, notes = outcome
            if summary_only:
                text = "[summary] " + " ".join(row["summary"].split())[:SUMMARY_FIELD]
            masked, rules = mask_text(text)
            if rules:
                notes.append("pre-model redaction: " + ", ".join(rules))
            if not masked.strip():
                if region.get("region_id"):
                    self.store.finish_region(
                        region["region_id"], "failed", "increment fully redacted"
                    )
                self.store.mark_capture(ident, "no-shareable-material")
                return
            opaque = self.store.opaque_for(row["root_session"])
            inc = region.get("inc") or {}
            marker = (inc.get("digest") or digest(["summary", ident]))[:64]
            payload = dict(
                role="organize", domain=self.store.domain, increment=masked,
                coverage=dict(
                    summary_only=summary_only, notes=notes,
                    gaps=len(self.store.coverage_gaps(region.get("key", "")))
                    if region.get("key") else 0,
                ),
                existing_drafts=self.store.draft_headers(producer=opaque),
                retrieved_refs=[
                    hit["ref"]
                    for hit in self.store.query(masked[:2000], limit=5)["results"]
                ],
            )
            try:
                result = self.agent(
                    payload, attempt_id=f"organize:{ident}",
                    root_hash=row["root_session"],
                )
            except MaintenanceCancelled:
                if region.get("region_id"):
                    self.store.finish_region(
                        region["region_id"], "cancelled", "sharing disabled or shutdown"
                    )
                self.store.mark_capture(ident, "cancelled", "maintenance cancelled")
                raise
            refs, applied_notes = self._apply(result, opaque=opaque, marker=marker)
            notes.extend(applied_notes)
            if region.get("region_id"):
                self.store.finish_region(
                    region["region_id"], "summary-only" if summary_only else "succeeded"
                )
            detail = canonical(dict(refs=refs, notes=notes))[:1000]
            self.store.mark_capture(ident, "organized", detail)
            self.last_activity = time.monotonic()
        except MaintenanceCancelled:
            raise
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"[:1000]
            self._error(detail)
            if region.get("region_id"):
                self.store.finish_region(region["region_id"], "failed", detail)
            self.store.mark_capture(ident, "failed", detail)

    # ---------------------------------------------------------------- worker

    def start(self):
        with self.store.lock, self.store.db:
            self.store.db.execute(
                "UPDATE captures SET status='discarded', "
                "detail='service restarted before processing completed' "
                "WHERE status='queued'"
            )
        self.thread.start()
        self.outbox_thread.start()

    def run(self):
        while not self.stop.is_set():
            try:
                ident = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if ident:
                    self._process(ident)
            except MaintenanceCancelled:
                pass
            except Exception as exc:  # never let the worker die silently
                self._error(f"{type(exc).__name__}: {exc}"[:1000])
            finally:
                self.queue.task_done()
        while True:  # shutdown: drain explicitly; nothing replays
            try:
                ident = self.queue.get_nowait()
            except queue.Empty:
                break
            if ident:
                self.store.mark_capture(
                    ident, "discarded", "service stopping before processing"
                )
            self.queue.task_done()

    # ---------------------------------------------------------------- outbox

    def _cancel_unsent(self, reason):
        """Sharing revocation cancels pending timers and unsent contributions."""
        self._cancel.set()
        for batch in self.store.outbox_pending():
            self.store.mark_batch(batch["batch_id"], "disabled", detail=reason)
        with self.store._write_txn():
            self.store.db.execute(
                "UPDATE votes SET publishable=0 WHERE batch_id IS NULL"
            )

    def _submit(self, batch_row):
        settings = self._settings()
        if not settings.allows_capture():
            return
        if self.community is None:
            self.store.mark_batch(
                batch_row["batch_id"], "unavailable", attempted=True,
                detail="mindie_knowledge.community is not installed; "
                       "the contribution cannot be sent (dependency failure)",
            )
            return
        batch = json.loads(batch_row["batch"])
        try:
            receipt = self.community["submit_batch"](
                batch, settings.as_dict(), self.state_dir, cancel=self._cancel
            )
        except Exception as exc:
            self.store.mark_batch(
                batch_row["batch_id"], "unknown", attempted=True,
                detail=f"{type(exc).__name__}: {exc}"[:500],
            )
            return
        self.store.mark_batch(
            batch_row["batch_id"], receipt.get("status", "unknown"),
            attempted=True, detail=receipt.get("detail", ""),
            pr_url=receipt.get("pr_url"), head_sha=receipt.get("head_sha"),
        )

    def _reconcile(self, batch_row):
        if self.community is None:
            return  # stay unresolved; the dependency failure is reported on submit
        try:
            receipt = self.community["reconcile_batch"](
                batch_row["batch_id"], self._settings().as_dict(), self.state_dir
            )
        except Exception:
            return
        self.store.mark_batch(
            batch_row["batch_id"], receipt.get("status", "unknown"),
            detail=receipt.get("detail", ""), pr_url=receipt.get("pr_url"),
            head_sha=receipt.get("head_sha"),
        )

    def _flush(self):
        """Coalesce all pending material into one batch and send it."""
        from .export import build_batch

        settings = self._settings()
        if not settings.allows_capture():
            return
        built = build_batch(self.store, settings=settings)
        if built is None:
            return
        batch_id, _revision, _batch, _ids, _votes = built
        self._submit(self.store.batch(batch_id))

    def _outbox_loop(self):
        """Model-free idle worker: lives for the service lifetime so the
        silent window always elapses before delivery; it never disappears
        after one Stop and never extends a model deadline."""
        while not self.stop.is_set():
            try:
                settings = self._settings()
                generation = settings.generation if settings.allows_capture() else None
                if self._generation is not None and generation != self._generation:
                    if generation is None:
                        self._cancel_unsent("sharing disabled; unsent work cancelled")
                    else:
                        self._cancel_unsent(
                            "settings generation changed; unsent work cancelled"
                        )
                        self._cancel.clear()
                self._generation = generation
                if generation is not None:
                    for row in self.store.outbox_unresolved()[:2]:
                        self._reconcile(row)
                    for row in self.store.outbox_pending()[:2]:
                        self._submit(row)
                    material = self.store.drafts_changed() or self.store.unbatched_votes()
                    if material:
                        idle_for = time.monotonic() - self.last_activity
                        deactivated = (
                            self.admission is not None
                            and not self.admission.leases()
                        )
                        if idle_for >= settings.idle_seconds or deactivated:
                            self._flush()
            except Exception as exc:
                self._error(f"outbox: {type(exc).__name__}: {exc}"[:500])
            self.stop.wait(1.0)

    def shutdown(self):
        self.stop.set()
        self._cancel.set()
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass
        self.thread.join(timeout=10)
        self.outbox_thread.join(timeout=5)

    # ---------------------------------------------------------------- status

    def status(self):
        settings = self._settings()
        return dict(
            **self.store.status(),
            maintenance_pending=self.queue.unfinished_tasks,
            maintenance_budget=self.budget.status(),
            sharing=settings.public_status(),
            community_package=self.community is not None,
            errors=self.errors,
        )
