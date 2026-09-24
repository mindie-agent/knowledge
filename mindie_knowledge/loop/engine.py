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
from .activation import activation_epoch
from .budget import BudgetExceeded, MaintenanceBudget
from .dfx import failure
from .documents import DraftFull
from .process import MaintenanceCancelled, bounded_run
from .store import Store, canonical, digest, new_identity, session_key

Store_confirmed = Store.CONFIRMED_BATCH

ORGANIZE_FIELDS = {"entry_id", "title", "summary", "conditions", "content"}
MAX_STRUCTURED_RESULT = 32 * 1024
MAX_INPUT = 64 * 1024
SUMMARY_FIELD = 4096
_EOF_SETTLE_LIMIT = 3
_PARTIAL_LIMIT = 4


class AdmissionUnreadable(Exception):
    """Admission storage could not be read. This is not a revocation."""


class CursorConflict(Exception):
    """The shared cursor moved. Do not call the model or count a failure."""


def mask_text(text):
    """Deterministic pre-model privacy filter; returns (masked, rules)."""
    findings = scan_text(text)
    for finding in findings:
        if finding.start is None or finding.end is None:
            raise ValueError("redaction finding is missing original source offsets")
    for finding in sorted(findings, key=lambda f: f.start, reverse=True):
        text = text[: finding.start] + f"[redacted:{finding.rule}]" + text[finding.end :]
    return text, sorted({finding.rule for finding in findings})


class Engine:
    def __init__(self, store, *, agent_command=None, settings_path=None,
                 admission=None, state_dir=None, transcript_adapter=None):
        """``transcript_adapter`` is the already-loaded trusted parser module
        (absolute local module from engine config ``transcript_adapter``)
        exporting ``FileIdentity``/``identify``/``read_material``. Without it
        capture degrades to honest summary-only; core never guesses a format."""
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
        self.transcript = transcript_adapter
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
        self._worker_failed = False
        self._activity_lock = threading.Lock()
        self._activity = 0
        self._frozen = False
        try:
            from mindie_knowledge.community import reconcile_batch, submit_batch

            self.community = dict(submit_batch=submit_batch,
                                  reconcile_batch=reconcile_batch)
        except Exception as exc:
            failure(
                "community.import",
                stage="import",
                category="dependency",
                exception=exc,
            )
            self.community = None

    # -------------------------------------------------------------- settings

    def _settings(self):
        return settings_mod.load(self.settings_path)

    def _error(self, detail):
        self.errors = (self.errors + [detail])[-20:]

    def _unexpected(self, operation, stage, exc):
        """Report an unhandled internal error. Expected cancellation, budget,
        config/caller, and community business errors are not incidents."""
        if isinstance(exc, (MaintenanceCancelled, AdmissionUnreadable, CursorConflict,
                            BudgetExceeded, OSError, ValueError, TypeError)):
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

        Community off short-circuits before any row. A paused maintenance
        circuit still records the turn and holds it until explicit resume;
        it does not drop the event. The Stop hook commits through the same
        identity before it talks to this process.
        """
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
        paused = self.budget.status()["paused"]
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
        captured = self.store.add_capture(
            root_session=root_hash, session=session_id, turn=turn_id,
            transcript=transcript_path, summary=summary or "",
            generation=settings.generation, boundary=boundary, scope=scope,
            namespace=namespace,
            activation_epoch=activation_epoch(lease["token"]),
            hold="maintenance-paused" if paused else None,
        )
        if captured.get("revoked") or paused:
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

    # -------------------------------------------------------------- organize

    def _gate_live(self):
        if self.stop.is_set():
            raise MaintenanceCancelled("service is stopping")
        if not self._settings().allows_capture():
            raise MaintenanceCancelled("sharing disabled before model spawn")

    def _revalidate(self, row):
        """The capture's persisted authorization must still hold exactly:
        same settings generation, live lease, unchanged authorized scope.
        Anything else is a revocation — the material never reaches a child."""
        settings = self._settings()
        if not settings.allows_capture():
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

    def agent(self, payload, *, attempt_id, root_hash, gate=None, reserve_region=None):
        raw = canonical(payload)
        if len(raw.encode("utf-8")) > MAX_INPUT:
            raise ValueError("maintenance input exceeds limit")
        self._cancel.clear()
        self.budget.reserve(attempt_id, root_hash, payload.get("role", "organize"))
        outcome = False
        started = False
        try:
            self._gate_live()
            if gate is not None:
                gate()  # identical-authorization recheck immediately before spawn
            if reserve_region is not None:
                reserve_region()  # budget admitted; consume exactly this input before spawn
            started = True
            output = bounded_run(
                self.agent_command, raw, timeout=125, max_output=131072,
                cancel=self._cancel,
            )
            try:
                result = json.loads(output)
                if not isinstance(result, dict):
                    raise ValueError("agent must return one JSON object")
                if len(canonical(result).encode("utf-8")) > MAX_STRUCTURED_RESULT:
                    raise ValueError("structured result exceeds the 32 KiB limit")
                self._validate_organize(result)
            except (ValueError, UnicodeError, TypeError) as exc:
                failure(
                    "organizer.result",
                    stage="validate",
                    category="invalid_result",
                    exception=exc,
                )
                raise
            outcome = True
            return result
        except MaintenanceCancelled:
            outcome = None
            raise
        except (AdmissionUnreadable, CursorConflict):
            if not started:
                self.budget.abandon_unstarted(attempt_id)
                outcome = "abandoned"
            raise
        finally:
            # A validated result stays 'running' until the caller checkpoints
            # it. Finishing success here would drop the output on an apply crash.
            # An unstarted admission or cursor conflict is abandoned, not failed.
            if outcome is not True and outcome != "abandoned":
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
            title = entry.get("title")
            if ident:
                # An update may pass title=null to preserve the existing one.
                if title is not None and (not isinstance(title, str) or not title.strip()):
                    raise ValueError("title must be null or nonempty text")
            elif not isinstance(title, str) or not title.strip():
                raise ValueError("a new entry requires a nonempty title")
            for name in ("summary", "content"):
                if not isinstance(entry.get(name), str) or not entry[name].strip():
                    raise ValueError(f"organized entry requires nonempty {name}")
            if not isinstance(entry.get("conditions", {}), dict):
                raise ValueError("conditions must be an object")

    @staticmethod
    def _is_notification(row):
        return row.get("identity_kind") == "notification"

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
                self.store.mark_capture(ident, terminal[0], terminal[1])
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
            terminal=("no-new-material", "eof settled; no new material"),
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

    def _defer_reread(self, ident):
        """Finite wait after a cursor conflict. Not an immediate busy loop."""
        reason = self.store.continuation_reason(ident) or ""
        if reason == "cursor-conflict":
            self.store.dormant_capture(ident, reason="cursor-conflict")
            return
        self._defer_counted(
            ident, "cursor-conflict", _PARTIAL_LIMIT, dormant_reason="cursor-conflict",
        )

    def _defer_apply(self, ident):
        """Keep a saved result and wait. Do not poll it again on the next tick."""
        reason = self.store.continuation_reason(ident) or ""
        if reason == "apply-pending":
            count = 0
        else:
            try:
                count = int(reason.split(":", 1)[1]) if reason.startswith("apply-retry:") else 0
            except ValueError:
                count = 0
        count += 1
        if count > _PARTIAL_LIMIT:
            self.store.schedule_continuation(
                ident, due=0, reason="apply-pending", eligible=0,
            )
            return
        self.store.schedule_continuation(
            ident, due=time.time() + min(8, 2 ** (count - 1)),
            reason=f"apply-retry:{count}", eligible=1,
        )

    def _summary_fallback(self, row, note, fail_detail):
        """Turn captures may use a bounded summary. Notifications never do."""
        if self._is_notification(row):
            self.store.mark_capture(row["id"], "failed", fail_detail)
            return "stop"
        if row["summary"].strip():
            return ("", True, [note])
        return False

    def _transcript_increment(self, row, settings, lease, region):
        """Read/filter the authorized increment; reserves regions as it goes.
        Returns (text, summary_only, notes) or None when there is no material."""
        parser = self.transcript
        if parser is None:
            # Missing parser: a turn may use its summary. A notification may not.
            fallback = self._summary_fallback(
                row, "no transcript adapter is configured; summary-only",
                "no transcript adapter is configured; notification summary is forbidden",
            )
            if fallback == "stop":
                return None
            if fallback is False:
                self.store.mark_capture(
                    row["id"], "failed",
                    "no transcript adapter is configured and no summary",
                )
                return None
            return fallback
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
            # No reliable authorization boundary: never backfill history.
            fallback = self._summary_fallback(
                row, "no reliable authorization boundary; summary-only",
                "no reliable authorization boundary; notification summary is forbidden",
            )
            if fallback == "stop":
                return None
            if fallback is False:
                self.store.mark_capture(
                    row["id"], "failed",
                    "no reliable authorization boundary and no summary",
                )
                return None
            return fallback
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
        if status == "ok" and inc["text"].strip():
            if not inc.get("timestamps_reliable", True):
                # Filtering by the authorization boundary was unreliable:
                # never admit possibly preauthorization text; degrade instead.
                fallback = self._summary_fallback(
                    row, "unreliable record timestamps; summary-only",
                    "unreliable record timestamps; notification summary is forbidden",
                )
                if fallback == "stop":
                    return None
                if fallback:
                    return fallback
                region_id = reserve(inc["start"], inc["end"], inc["digest"], status="failed")
                if region_id is None:
                    self._defer_reread(row["id"])
                    return None
                self.store.finish_region(
                    region_id, "failed", "unreliable timestamps and no summary",
                )
                self.store.mark_capture(
                    row["id"], "failed", "unreliable timestamps and no summary",
                )
                return None
            return (inc["text"], False, [])
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
                region_id = reserve(inc["start"], inc["end"], inc["digest"],
                                    status="succeeded", detail="no public material")
                if region_id is None:
                    self._defer_reread(row["id"])
                    return None
                self.store.finish_region(region_id, "succeeded")
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
            fallback = self._summary_fallback(
                row, "unknown transcript format; summary-only",
                "unknown transcript format; notification summary is forbidden",
            )
            if fallback == "stop":
                return None
            if fallback:
                return fallback
            region_id = reserve(inc["start"], inc["end"], inc["digest"])
            if region_id is None:
                self._defer_reread(row["id"])
                return None
            region["region_id"] = region_id
            self.store.finish_region(
                region_id, "failed", "unknown transcript format and no summary",
            )
            self.store.mark_capture(
                row["id"], "failed", "unknown transcript format and no summary",
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
            if self._is_notification(row):
                self.store.mark_capture(
                    row["id"], "failed",
                    "transcript replaced; notification summary is forbidden",
                )
                return None
            if row["summary"].strip():
                return ("", True, ["transcript replaced; summary-only"])
            self.store.mark_capture(
                row["id"], "failed", "transcript replaced and no summary",
            )
            return None
        if status == "wrong-task":
            self.store.mark_capture(row["id"], "failed",
                                    "transcript identity mismatch; not read")
            return None
        # missing/unreadable transcript
        fallback = self._summary_fallback(
            row, "transcript unreadable; summary-only",
            "transcript unreadable; notification summary is forbidden",
        )
        if fallback == "stop":
            return None
        if fallback:
            return fallback
        self.store.mark_capture(
            row["id"], "failed", "transcript unreadable and no summary",
        )
        return None

    @staticmethod
    def _pr_number(pr_url):
        if not isinstance(pr_url, str):
            return None
        tail = pr_url.rstrip("/").rsplit("/", 1)[-1]
        return int(tail) if tail.isdigit() else None

    def _restore_sent_draft(self, entry_id, generation):
        """Fetch the exact body this entry last sent so a later same-task
        observation appends to/updates the prior body instead of replacing it
        with only the new paragraph.

        Addressed by the per-entry last-confirmed receipt (independent of the
        newest lineage outbox row): exact confirmed head, path, hash and sent
        revision. The fetched body must match the retained hash AND parse
        back to the same entry identity and sent revision. Any mismatch or
        failure simply keeps the update refused — no base is ever fabricated,
        a withdrawn entry is never resurrected and a maintainer correction is
        never overwritten (the retained hash stays the honest expected base).
        """
        def fail(reason):
            self._error(f"restore: {reason}"[:240])
            return False

        if self.community is None or not generation:
            return fail("community unavailable")
        receipt = self.store.sent_receipt(entry_id)
        if not receipt or receipt.get("generation") not in (generation, None):
            return fail("no matching receipt")
        head_sha = receipt.get("head_sha")
        path = receipt.get("path")
        expected_hash = receipt.get("sha256")
        expected_revision = receipt.get("sent_revision")
        if not head_sha or not path or not expected_revision:
            return fail("incomplete receipt")
        settings = self._settings()
        if not settings.allows_capture() or not settings.repository:
            return fail("capture disabled")
        last = None
        try:
            from mindie_knowledge.community import gitops
            from mindie_knowledge.community.common import Deadline
            from mindie_knowledge.community.publish import _remote_url

            from .documents import parse_entry

            cfg = settings.as_dict()
            write_repo = cfg.get("fork") or settings.repository
            deadline = Deadline(cfg.get("transaction_seconds", 120), 30,
                                cancel=self._cancel)
            env = gitops.git_env(cfg)
            work_dir = gitops.ensure_clone(
                _remote_url(cfg, write_repo),
                self.state_dir / "git" / write_repo.replace("/", "_"),
                deadline, env=env,
            )
            if not gitops.fetch_commit(work_dir, head_sha, deadline, env=env):
                last = "commit fetch refused"
                number = self._pr_number(receipt.get("pr_url"))
                if number is None:
                    last = "commit fetch refused and no PR"
                else:
                    try:
                        fetched = gitops.fetch_pr_head(
                            work_dir, number, "", deadline, env=env
                        )
                        if fetched != head_sha:
                            last = "pr head mismatch"
                    except Exception as exc:
                        last = f"{type(exc).__name__}: {exc}"
            # Clone of main may already hold the receipt head even when a
            # SHA fetch is refused; show the exact commit either way.
            raw = gitops.show_file(work_dir, head_sha, path, deadline, env=env)
            if raw is None:
                return fail(last or "missing blob at receipt head")
            normalized = raw.encode("utf-8").replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            if len(normalized) > 128 * 1024:
                return fail("blob exceeds limit")
            if expected_hash and hashlib.sha256(normalized).hexdigest() != expected_hash:
                return fail("hash mismatch")
            doc = parse_entry(normalized)
            if doc["entry_id"] != entry_id or doc["revision"] != expected_revision:
                return fail("entry or revision mismatch")
            self.store.restore_draft(entry_id, doc, generation=generation)
            return True
        except Exception as exc:
            return fail(f"{type(exc).__name__}: {exc}")

    def _scanned_entries(self, result):
        """Keep only outbound-clean entries and assign stable ids before apply."""
        kept, notes = [], []
        for entry in result["entries"]:
            candidate = canonical({key: entry.get(key) for key in sorted(ORGANIZE_FIELDS)})
            findings = scan_text(candidate)
            if findings:
                rules = ", ".join(sorted({finding.rule for finding in findings}))
                notes.append(
                    "entry rejected before storage/publication; outbound scan: " + rules
                )
                continue
            item = {key: entry.get(key) for key in ORGANIZE_FIELDS if key in entry}
            if not item.get("entry_id"):
                item["entry_id"] = new_identity()
                item["new"] = True
            kept.append(item)
        return kept, notes

    def _apply_one(self, entry, *, opaque, marker, generation):
        ident = entry["entry_id"]
        header = {
            key: entry[key] for key in ("title", "summary", "conditions") if key in entry
        }
        if entry.get("new"):
            found = self.store._row(ident)
            if found is not None and found["draft_revision"]:
                return self.store.ref(ident, found["draft_revision"])
            doc = self.store.create_draft(
                kind="experience", title=entry["title"], summary=entry["summary"],
                content=entry["content"], conditions=entry.get("conditions") or {},
                owner=opaque, entry_id=ident, generation=generation,
            )
            return self.store.ref(doc["entry_id"], doc["revision"])
        try:
            doc, _appended = self.store.append_observation(
                ident, entry["content"], marker=marker, producer=opaque,
                generation=generation, header=header,
            )
        except ValueError as exc:
            if "no local draft" not in str(exc) or not self._restore_sent_draft(ident, generation):
                raise
            doc, _appended = self.store.append_observation(
                ident, entry["content"], marker=marker, producer=opaque,
                generation=generation, header=header,
            )
        return self.store.ref(doc["entry_id"], doc["revision"])

    def _checkpoint_result(self, attempt_id, result, region, inc, summary_only, notes, row, *, apply=False):
        """Save the scanned result and the region receipt in one transaction.

        ``apply`` then runs the saved body. A false value leaves apply-pending
        when there is something to apply, which is the admission-unreadable path.
        """
        kept, scan_notes = self._scanned_entries(result)
        notes = list(notes) + scan_notes
        more = bool(inc.get("more") and inc.get("end", 0) > inc.get("start", 0))
        region_id = region.get("region_id")
        region_status = "summary-only" if summary_only else "succeeded" if region_id else None
        detail = canonical(dict(refs=[], notes=notes))[:1000]
        if not kept:
            self.budget.settle_empty(
                attempt_id, canonical(dict(refs=[], notes=notes)),
                capture_id=row["id"], capture_detail=detail,
                region_id=region_id, region_status=region_status or "succeeded",
                more=more,
            )
            return
        self.budget.checkpoint(
            attempt_id, canonical(dict(entries=kept)),
            canonical(dict(items=[], notes=scan_notes, refs=[], more=more)),
            capture_id=row["id"], capture_status="apply-pending",
            capture_detail=detail, region_id=region_id, region_status=region_status,
        )
        if apply:
            self._apply_saved(attempt_id, self.store.capture_row(row["id"]) or row)

    def _apply_saved(self, attempt_id, row):
        """Apply a checkpointed result once. Never calls the organizer."""
        record = self.budget.application(attempt_id)
        if record is None or not record.get("result"):
            return
        try:
            self._revalidate(row)
        except AdmissionUnreadable:
            # The body stays. A later due continuation retries the apply.
            if row and row.get("id"):
                self._defer_apply(row["id"])
            return
        except MaintenanceCancelled as exc:
            self.budget.release_application(
                attempt_id, canonical(dict(state="revoked", cause=str(exc)[:200])),
            )
            if row and row.get("status") not in {"cancelled", "organized"}:
                self.store.mark_capture(row["id"], "cancelled", str(exc)[:500])
            return
        payload = json.loads(record["result"])
        receipt = json.loads(record["apply_receipt"] or "{}")
        if not isinstance(receipt, dict):
            receipt = {}
        items = [item for item in receipt.get("items", []) if isinstance(item, dict)]
        done = {item.get("entry_id") for item in items if item.get("state") == "applied"}
        refs = [ref for ref in receipt.get("refs", []) if isinstance(ref, str)]
        notes = [note for note in receipt.get("notes", []) if isinstance(note, str)]
        marker = attempt_id.split(":", 2)[2]
        opaque = self.store.opaque_for(row["root_session"])
        blocked = False
        for entry in payload.get("entries", []):
            entry_id = entry.get("entry_id")
            if entry_id in done:
                continue
            try:
                ref = self._apply_one(
                    entry, opaque=opaque, marker=marker, generation=row["generation"],
                )
                refs.append(ref)
                items.append(dict(entry_id=entry_id, state="applied"))
                done.add(entry_id)
            except DraftFull:
                blocked = True
                items.append(dict(entry_id=entry_id, state="blocked", cause="draft-full"))
                notes.append("draft full")
            except ValueError as exc:
                blocked = True
                items.append(dict(entry_id=entry_id, state="blocked", cause="apply-failed"))
                notes.append(str(exc)[:200])
        receipt = dict(items=items, notes=notes, refs=refs, more=bool(receipt.get("more")))
        detail = canonical(dict(refs=refs, notes=notes))[:1000]
        if blocked:
            self.budget.checkpoint(
                attempt_id, record["result"], canonical(receipt),
                capture_id=row["id"], capture_status="apply-pending",
                capture_detail=detail,
            )
            self._defer_apply(row["id"])
            return
        self.budget.complete_application(
            attempt_id, canonical(receipt), capture_id=row["id"],
            capture_detail=detail, more=bool(receipt.get("more")),
        )

    def _apply(self, result, *, opaque, marker, generation):
        """Deterministic metadata update + append; no repair model call."""
        refs, notes = [], []
        for entry in result["entries"]:
            candidate = canonical({k: entry.get(k) for k in sorted(ORGANIZE_FIELDS)})
            findings = scan_text(candidate)
            if findings:
                rules = ", ".join(sorted({f.rule for f in findings}))
                notes.append(f"entry rejected before storage/publication; outbound scan: {rules}")
                continue
            try:
                ident = entry.get("entry_id")
                if ident:
                    try:
                        doc, _appended = self.store.append_observation(
                            ident, entry["content"], marker=marker, producer=opaque,
                            generation=generation, header={k: entry[k] for k in
                                ("title", "summary", "conditions") if k in entry},
                        )
                    except ValueError as exc:
                        # A compacted (confirmed-sent) draft has no local body:
                        # fetch the exact prior remote body once, then append.
                        if "no local draft" not in str(exc) or not self._restore_sent_draft(
                            ident, generation
                        ):
                            raise
                        doc, _appended = self.store.append_observation(
                            ident, entry["content"], marker=marker, producer=opaque,
                            generation=generation, header={k: entry[k] for k in
                                ("title", "summary", "conditions") if k in entry},
                        )
                else:
                    doc = self.store.create_draft(
                        kind="experience", title=entry["title"],
                        summary=entry["summary"], content=entry["content"],
                        conditions=entry.get("conditions") or {}, owner=opaque,
                        generation=generation,
                    )
                refs.append(self.store.ref(doc["entry_id"], doc["revision"]))
            except DraftFull:
                notes.append(f"draft full: {entry.get('entry_id')}")
            except ValueError as exc:
                notes.append(str(exc)[:200])
        return refs, notes

    def _process(self, ident):
        row = self.store.capture_row(ident)
        if row is None:
            return
        if row["status"] not in {"queued", "pending", "deferred"}:
            return
        if self.budget.status()["paused"]:
            self.store.dormant_capture(ident, reason="maintenance-paused")
            return
        settings = self._settings()
        if not settings.allows_capture():
            self.store.mark_capture(ident, "cancelled", "sharing disabled while queued")
            return
        lease = self.admission.active_lease(row["session"]) if self.admission else None
        if self.admission is not None and lease is None:
            inspected = self.admission.inspect(row["session"])
            if inspected.get("status") == "unavailable":
                self._defer_admission(ident)
                return
            self.store.mark_capture(ident, "cancelled", "task deactivated while queued")
            return
        if not self.agent_command:
            self.store.mark_capture(
                ident, "discarded", "no maintenance runner is configured"
            )
            return
        region = {}
        try:
            try:
                self._revalidate(row)
            except AdmissionUnreadable:
                self._defer_admission(ident)
                return
            except MaintenanceCancelled as exc:
                self.store.mark_capture(ident, "cancelled", str(exc)[:500])
                return
            if row["transcript"]:
                outcome = self._transcript_increment(row, settings, lease, region)
            elif self._is_notification(row):
                self.store.mark_capture(
                    ident, "failed",
                    "notification requires a transcript; summary is forbidden",
                )
                return
            elif row["summary"].strip():
                outcome = ("", True, [])
            else:
                self.store.mark_capture(ident, "no-new-material")
                return
            if outcome is None:
                return
            text, summary_only, notes = outcome
            if summary_only and self._is_notification(row):
                self.store.mark_capture(
                    ident, "failed", "notification forbids summary fallback",
                )
                return
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
                    ranges=inc.get("coverage", []),
                    start=inc.get("start"), end=inc.get("end"),
                    more=inc.get("more", False),
                ),
                existing_drafts=self.store.draft_headers(
                    owner=opaque, generation=row["generation"], query=masked),
                retrieved_refs=[
                    hit["ref"]
                    for hit in self.store.query(masked[:2000], limit=5)["results"]
                ],
            )
            # Keep the new evidence intact; older optional context yields first
            # when UTF-8 headers would exceed the worker envelope.
            while payload["existing_drafts"] and len(canonical(payload).encode()) > MAX_INPUT:
                payload["existing_drafts"].pop()
            def reserve_input():
                if inc and not region.get("region_id"):
                    reserved = region["reserve"](inc["start"], inc["end"], inc["digest"])
                    if reserved is None:
                        raise CursorConflict("cursor advanced before reservation")
                    region["region_id"] = reserved
                self.store.mark_capture(ident, "processing", "input reserved; no retry of this region")
            attempt_id = f"organize:{ident}:{marker}"
            try:
                result = self.agent(
                    payload, attempt_id=attempt_id,
                    root_hash=row["root_session"],
                    gate=lambda: self._revalidate(row),
                    reserve_region=reserve_input,
                )
            except AdmissionUnreadable:
                self._defer_admission(ident)
                return
            except CursorConflict:
                self._defer_reread(ident)
                return
            except MaintenanceCancelled as exc:
                if region.get("region_id"):
                    self.store.finish_region(
                        region["region_id"], "cancelled", str(exc)[:500]
                    )
                self.store.mark_capture(ident, "cancelled", str(exc)[:500])
                return
            try:
                self._revalidate(row)  # again before applying any result
            except AdmissionUnreadable:
                self._checkpoint_result(
                    attempt_id, result, region, inc, summary_only, notes, row,
                )
                return
            except MaintenanceCancelled as exc:
                if region.get("region_id"):
                    self.store.finish_region(
                        region["region_id"], "cancelled", str(exc)[:500]
                    )
                self.store.mark_capture(ident, "cancelled", str(exc)[:500])
                return
            self._checkpoint_result(
                attempt_id, result, region, inc, summary_only, notes, row,
                apply=True,
            )
            self.last_activity = time.monotonic()
        except BudgetExceeded as exc:
            if exc.retry_at is not None and not region.get("region_id"):
                self.store.defer_capture(ident, due=exc.retry_at, reason=str(exc))
            else:
                self.store.mark_capture(ident, "discarded", str(exc))
        except Exception as exc:
            self._unexpected("knowledge.capture", "process", exc)
            detail = f"{type(exc).__name__}: {exc}"[:1000]
            self._error(detail)
            current = self.store.capture_row(ident)
            if current and current["status"] in {"apply-pending", "organized"}:
                return
            if region.get("region_id"):
                self.store.finish_region(region["region_id"], "failed", detail)
            self.store.mark_capture(ident, "failed", detail)

    # ---------------------------------------------------------------- worker

    def _apply_due(self):
        """Apply one saved result whose continuation is due. No new model.

        ``begin_work`` is the same admission gate as capture processing.
        Frozen or stopping does not start. One count covers the whole apply
        and is released in ``finally``.
        """
        attempt_id = self.store.due_application()
        if not attempt_id or not self.begin_work():
            return
        try:
            capture_id = attempt_id.split(":", 2)[1] if attempt_id.count(":") >= 2 else None
            row = self.store.capture_row(capture_id) if capture_id else None
            if row is None:
                return
            try:
                self._apply_saved(attempt_id, row)
            except Exception as exc:
                self._unexpected("knowledge.capture", "apply", exc)
                if row.get("id"):
                    self._defer_apply(row["id"])
        finally:
            self.end_work()

    def start(self):
        with self.store.lock, self.store.db:
            self.store.db.execute(
                "UPDATE captures SET status='failed', "
                "detail='service restarted after input reservation; not replaying' "
                "WHERE status='processing'"
            )
            self.store.db.execute("UPDATE regions SET status='failed', detail='interrupted attempt; no replay' WHERE status='attempted'")
        self.budget.recover_interrupted()
        self.revoke_stale()
        # Arm only. Applying here can block readiness on a network restore.
        self.store.arm_apply_continuations()
        self.thread.start()
        self.outbox_thread.start()

    def revoke_stale(self):
        """Restart/poll-edge safety net: when sharing is enabled, pending
        batches of any other generation become disabled; when it is disabled,
        every unsent batch and publishable vote is cancelled. Old material is
        never backfilled into a new generation."""
        settings = self._settings()
        if settings.allows_capture():
            self.store.disable_foreign_pending(settings.generation)
        else:
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
                    elif not ident:
                        self._apply_due()
                except (MaintenanceCancelled, AdmissionUnreadable, CursorConflict):
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
            except (MaintenanceCancelled, AdmissionUnreadable, CursorConflict):
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
        batch = json.loads(batch_row["batch"])
        try:
            withdrawn = any(self.store.get(ref).get("withdrawn", False)
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
        )
        if receipt.get("status") in Store_confirmed:
            # The GitHub branch is now the durable body source: drop the sent
            # private payload (draft bodies/history, raw summaries, staging).
            self.store.compact_confirmed(batch_row["batch_id"])

    def _reconcile(self, batch_row):
        if self.community is None:
            return  # stay unresolved; the dependency failure is reported on submit
        try:
            receipt = self.community["reconcile_batch"](
                batch_row["batch_id"], self._settings().as_dict(), self.state_dir
            )
        except Exception as exc:
            self._unexpected("knowledge.publish", "reconcile", exc)
            return
        self.store.mark_batch(
            batch_row["batch_id"], receipt.get("status", "unknown"),
            detail=receipt.get("detail", ""), pr_url=receipt.get("pr_url"),
            head_sha=receipt.get("head_sha"),
        )
        if receipt.get("status") in Store_confirmed:
            self.store.compact_confirmed(batch_row["batch_id"])

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
                if generation is not None and not self._is_frozen():
                    for row in self.store.outbox_unresolved()[:2]:
                        if self._is_frozen():
                            break
                        if self.store.reconcile_due(row["batch_id"]) and self.begin_work():
                            try:
                                self._reconcile(row)
                            finally:
                                self.end_work()
                    for row in self.store.outbox_pending()[:2]:
                        if self._is_frozen():
                            break
                        if self.begin_work():
                            try:
                                self._submit(row)
                            finally:
                                self.end_work()
                    material = self.store.drafts_changed(
                        generation=generation
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
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass
        self.thread.join(timeout=10)
        self.outbox_thread.join(timeout=5)

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
        busy = activity > 0 or queued > 0 or due
        if busy:
            with self._activity_lock:
                if not self.stop.is_set():
                    self._frozen = False
            return dict(
                idle=False, status="busy", activity=activity,
                queued_captures=queued, due_capture=due,
            )
        return dict(
            idle=True, status="stopping", activity=0,
            queued_captures=0, due_capture=False,
        )

    def status(self):
        settings = self._settings()
        with self._activity_lock:
            activity = self._activity
            frozen = self._frozen
        return dict(
            **self.store.status(),
            maintenance_pending=self.queue.unfinished_tasks,
            maintenance_budget=self.budget.status(),
            sharing=settings.public_status(),
            community_package=self.community is not None,
            errors=self.errors,
            activity=activity,
            admission_frozen=frozen,
            worker_alive=self.thread.is_alive() and not self._worker_failed,
            worker_failed=bool(self._worker_failed),
            outbox_alive=self.outbox_thread.is_alive(),
        )
