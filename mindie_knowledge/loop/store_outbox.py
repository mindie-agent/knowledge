"""Outbox, publication receipt and staging mixin for the authoritative Store.

[Role] Outbox, publication receipt and staging methods of the authoritative Store.
[Invariants] Durable receipts are written before a remote write can be attempted;
unknown outcomes remain unresolved until read-only reconciliation, never replayed.
[Timeouts & Locks] Runs under the Store RLock and an explicit SQLite transaction;
publication cleanup failures stay visible on the receipt.
[Side-Effect Policy] Mutates outbox rows, sent receipts and local staging files;
no Git push or GitHub request is issued from this module.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from . import documents
from .documents import DraftFull
from ..materials.references import (
    MaterialReadError, ReadReferenceError, block_ref, feedback_ref,
    parse_feedback_ref, parse_read_ref, task_ref,
)
from .store_schema import (
    MAX_VOTE_REASON,
    MISSING_MATERIAL_JOB_DETAIL,
    RATINGS,
    REPLACEABLE_BATCH,
    SCHEMA,
    SUMMARY_STATUS_SQL,
    TERMINAL_BATCH,
    _HARNESS_RE,
    _IDENTITY_KINDS,
    _REARM_REASONS,
    _STATE_COLUMNS,
    _UNPROCESSED_CAPTURE,
    _backoff_seconds,
    _capture_identity_row,
    _capture_revision_refs,
    _exact_revision_ref,
    _initialize_state,
    _stored_json,
    _upsert_continuation,
    _valid_retry_at,
    _validate_schema_identity,
    _validate_state,
    canonical,
    capture_identity,
    commit_capture,
    digest,
    new_identity,
    session_key,
    validate_capture_material_receipt,
)


class OutboxMixin:

    EXPORT_BACKOFF_BASE = 60.0
    EXPORT_BACKOFF_CAP = 3600.0

    @classmethod
    def _export_backoff(cls, attempts):
        return _backoff_seconds(
            cls.EXPORT_BACKOFF_BASE, cls.EXPORT_BACKOFF_CAP, attempts
        )
    def reserve_export(self, fingerprint):
        """Consume this material revision before final scanning/staging.

        True means this process may build now; False means do not build:

        - ``staged`` — the batch was already recorded (the pending outbox row
          owns it);
        - ``failed`` with class ``content`` — deterministic content rejection,
          quarantined to exactly this fingerprint and never retried;
        - ``next_check`` in the future — a transient failure (or an
          interrupted attempt) is backing off.

        An interrupted attempt (crash between reserve and finish) leaves an
        ``attempted`` record with a persisted ``next_check``: no remote write
        or outbox batch can exist for it, so once the backoff is due the
        local deterministic build resumes automatically — without organizer
        replay, cursor resets or redaction bypass. Every reservation persists
        the next backoff time up front, so a crash cannot spin a failing
        build every idle tick.
        """
        now = time.time()
        with self._write_txn():
            row = self.db.execute(
                "SELECT value FROM state WHERE key=?", ("export:" + fingerprint,)
            ).fetchone()
            if row is None:
                self.db.execute(
                    "INSERT INTO state VALUES(?,?)",
                    (
                        "export:" + fingerprint,
                        canonical({
                            "status": "attempted", "attempts": 1, "detail": "",
                            "class": None,
                            # The backoff is persisted up front, so a crash in
                            # the build window also resumes after a delay
                            # instead of spinning every idle tick.
                            "next_check": now + self._export_backoff(1),
                            "updated": now,
                        }),
                    ),
                )
                return True
            record = _stored_json(row[0], dict, "export attempt")
            status = record.get("status")
            if status == "staged":
                return False
            if status == "failed" and record.get("class") == "content":
                return False
            next_check = record.get("next_check") or 0
            if next_check and now < next_check:
                return False
            attempts = int(record.get("attempts") or 0) + 1
            self.db.execute(
                "UPDATE state SET value=? WHERE key=?",
                (
                    canonical({
                        "status": "attempted", "attempts": attempts,
                        "detail": str(record.get("detail", ""))[:500],
                        "class": record.get("class"),
                        "next_check": now + self._export_backoff(attempts),
                        "updated": now,
                    }),
                    "export:" + fingerprint,
                ),
            )
            return True
    def finish_export(self, fingerprint, status, detail="", *, classification=None):
        """Record the build outcome with its failure class and next check.

        ``classification='content'`` quarantines the fingerprint permanently
        (a redaction/validation rejection is never resolved by retrying);
        anything else is transient: the record keeps the reason and the
        persisted ``next_check`` so background scheduling resumes it with
        backoff after the local problem is fixed."""
        now = time.time()
        with self._write_txn():
            row = self.db.execute(
                "SELECT value FROM state WHERE key=?", ("export:" + fingerprint,)
            ).fetchone()
            record = {}
            if row is not None:
                record = _stored_json(row[0], dict, "export attempt")
            attempts = int(record.get("attempts") or 0)
            next_check = None
            if status == "failed":
                classification = classification or "transient"
                if classification != "content":
                    next_check = now + self._export_backoff(max(1, attempts))
            self.db.execute(
                "UPDATE state SET value=? WHERE key=?",
                (
                    canonical({
                        "status": status, "attempts": attempts,
                        "detail": str(detail)[:500], "class": classification,
                        "next_check": next_check, "updated": now,
                    }),
                    "export:" + fingerprint,
                ),
            )
    def create_batch(self, *, batch_id, revision, batch, entry_ids, vote_keys,
                     generation=None, source_revisions=None, entry_revisions=None, publication_target=None):
        """Record one built batch as pending and bind its material.

        Material bound to a batch is never silently re-batched: only a newer
        draft revision or a replacement vote becomes new work. The batch row
        stores only its frozen-file descriptor, never a second body copy.
        """
        # Only identities enter SQLite; publication reads the frozen files.
        if any("content" in item for item in batch.get("files", [])):
            raise ValueError("outbox descriptors cannot contain file bodies")
        if set(entry_revisions or {}) != set(entry_ids):
            raise ValueError("frozen task revisions differ from task package identities")
        from mindie_knowledge.materials.publication import staging_path
        frozen = staging_path(self.root, batch_id, revision) / "manifest.json"
        if not frozen.is_file():
            raise ValueError("contribution files must be frozen before recording the outbox")
        now = time.time()
        replaced = False
        with self._write_txn():
            existing = self.db.execute(
                "SELECT revision, status FROM outbox WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if existing is not None:
                if existing["status"] not in REPLACEABLE_BATCH:
                    raise ValueError(
                        f"batch lineage has an unresolved {existing['status']} "
                        "receipt; reconcile it before new work"
                    )
                if existing["revision"] == revision:
                    raise ValueError("this exact batch revision was already sent")
                self._queue_publication_cleanup(batch_id, existing["revision"])
                replaced = True
                self.db.execute("DELETE FROM outbox WHERE batch_id=?", (batch_id,))
            self.db.execute(
                "INSERT INTO outbox VALUES(?,?,?,?,?,NULL,NULL,?,NULL,?,0,NULL,?)",
                (batch_id, revision, canonical(batch), "pending", "", now, now,
                 generation),
            )
            if source_revisions is not None:
                if set(source_revisions) != set(entry_ids):
                    raise ValueError("prepared feed identities differ from task package identities")
                self.feed_set("prepared-package-bases:" + batch_id,
                              dict(batch_revision=revision, source_revisions=source_revisions,
                                   entry_revisions=entry_revisions, publication_target=publication_target))
            for entry_id in entry_ids:
                row = self._row(entry_id)
                if row is not None:
                    self.db.execute(
                        "UPDATE entries SET batched_revision=? WHERE entry_id=?",
                        (entry_revisions[entry_id], entry_id),
                    )
            for opaque, entry_id, revision in vote_keys:
                self.db.execute(
                    "UPDATE votes SET batch_id=? WHERE root_opaque=? "
                    "AND entry_id=? AND revision=?",
                    (batch_id, opaque, entry_id, revision),
                )
        if replaced:
            self._cleanup_publication_staging(batch_id)
    def mark_batch(self, batch_id, status, *, detail="", pr_url=None, head_sha=None,
                   attempted=False, actual_files=None, retry_at=None):
        """Record one batch receipt.

        ``actual_files`` is the publisher's report of the entry files as
        actually committed (post-merge content identities), so the per-entry
        receipts reflect what the remote holds rather than the candidate
        payload. A valid ``retry_at`` (Retry-After receipt hint) raises the
        persisted next-attempt floor in this same transaction — never
        shortening it. Entering ``unknown``/``unavailable`` from a different
        status resets the operation attempt counter so the first
        reconciliation is not skipped by an inherited count, but never erases
        an already reserved next-attempt floor: the reserved time is kept and
        the receipt hint can only raise it."""
        if status not in {"pending", *TERMINAL_BATCH}:
            raise ValueError("invalid batch status")
        with self._write_txn():
            previous = self.db.execute(
                "SELECT status FROM outbox WHERE batch_id=?", (batch_id,)
            ).fetchone()
            self.db.execute(
                "UPDATE outbox SET status=?, detail=?, pr_url=COALESCE(?, pr_url), "
                "head_sha=COALESCE(?, head_sha), attempted=COALESCE(?, attempted), "
                "updated=? WHERE batch_id=?",
                (status, str(detail)[:1000], pr_url, head_sha,
                 time.time() if attempted else None, time.time(), batch_id),
            )
            if (
                previous is not None
                and previous["status"] != status
                and status in ("unknown", "unavailable")
            ):
                # Reset only the attempt COUNT. A later reserved floor
                # (next_attempt) survives the transition untouched.
                self.db.execute(
                    "UPDATE outbox SET reconciliations=0 WHERE batch_id=?",
                    (batch_id,),
                )
            if _valid_retry_at(retry_at):
                self.db.execute(
                    "UPDATE outbox SET next_attempt=MAX(COALESCE(next_attempt, 0), ?) "
                    "WHERE batch_id=?",
                    (float(retry_at), batch_id),
                )
            if status in self.CONFIRMED_BATCH:
                row = self.db.execute(
                    "SELECT * FROM outbox WHERE batch_id=?", (batch_id,)
                ).fetchone()
                if row is not None:
                    batch = _stored_json(row["batch"], dict, "outbox batch")
                    self._record_sent_receipts(row, batch, actual_files=actual_files)
            if status == "rejected":
                # A proven closed-unmerged PR retires exactly this batch's
                # entry material from automatic publication; the lineage row
                # itself stays replaceable so unrelated material flows.
                row = self.db.execute(
                    "SELECT batch FROM outbox WHERE batch_id=?", (batch_id,)
                ).fetchone()
                refs = _stored_json(row[0], dict, "outbox batch").get("entry_refs", []) if row else []
                for ref in refs:
                    parsed = _exact_revision_ref(ref)
                    if parsed is None:
                        continue
                    self.db.execute(
                        "INSERT OR IGNORE INTO entry_quarantine VALUES(?,?,?,?)",
                        (parsed[0], "pr-rejected",
                         str(detail)[:500] or "contribution PR closed unmerged",
                         time.time()),
                    )
        if previous is not None and previous['status'] != status and status in {
                'failed', 'unknown', 'unavailable', 'needs_review', 'rejected'}:
            from .dfx import failure
            failure('knowledge.publish', stage='receipt', category='publication_' + status,
                    reportable=False)
    def batch(self, batch_id):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM outbox WHERE batch_id=?", (batch_id,)
            ).fetchone()
        return dict(row) if row else None
    def disable_foreign_pending(self, generation):
        """Pending batches from another settings generation are cancelled,
        never sent: durable across restarts and missed polling edges."""
        with self._write_txn():
            cursor = self.db.execute(
                "UPDATE outbox SET status='disabled', "
                "detail='settings generation changed before send', updated=? "
                "WHERE status='pending' AND generation IS NOT ?",
                (time.time(), generation),
            )
            return cursor.rowcount
    def _outbox_rows(self, condition, *, limit=None, due_only=False):
        sql = 'SELECT * FROM outbox WHERE (' + condition + ')'
        params = []
        if due_only:
            sql += ' AND (next_attempt IS NULL OR next_attempt<=?)'
            params.append(time.time())
        sql += ' ORDER BY created'
        if limit is not None:
            sql += ' LIMIT ?'
            params.append(limit)
        return [dict(row) for row in self.db.execute(sql, params)]
    def outbox_pending(self, *, limit=None):
        """Never-attempted batches (resumable after a forced shutdown)."""
        with self.lock:
            return self._outbox_rows("status='pending' AND attempted IS NULL", limit=limit)
    def cancel_pending(self, detail, *, keep_generation=None):
        """Cancel unsent operations without touching current or uncertain work."""
        with self._write_txn():
            return self.db.execute(
                "UPDATE outbox SET status='disabled', detail=?, updated=? "
                "WHERE status='pending' AND attempted IS NULL "
                "AND (? IS NULL OR generation IS NOT ?)",
                (detail, time.time(), keep_generation, keep_generation),
            ).rowcount
    def revoke_unsent(self, detail, *, keep_generation=None):
        """Batch and feedback revocation share one authority transaction."""
        with self._write_txn():
            self.cancel_pending(detail, keep_generation=keep_generation)
            self.db.execute(
                "UPDATE votes SET publishable=0 WHERE batch_id IS NULL "
                "AND (? IS NULL OR NOT EXISTS (SELECT 1 FROM grants "
                "WHERE grants.kind='vote' AND grants.identity=votes.root_opaque||':'||votes.entry_id "
                "AND grants.revision=votes.revision AND grants.generation=?))",
                (keep_generation, keep_generation),
            )
    def _operation_due(self, batch_id):
        """Persisted exponential backoff gate for one bounded outbox operation.

        Shared by read-only reconciliation (``unknown``) and resubmission
        (``unavailable``): one bounded operation per scheduler opportunity,
        interval growing to one hour, durable across restarts — and no
        permanent exhaustion latch. An unknown outcome stays unknown until
        remote evidence proves it; a transient send failure resumes after the
        environment recovers. True when the operation may run now."""
        now = time.time()
        with self._write_txn():
            row = self.db.execute(
                "SELECT reconciliations, next_attempt FROM outbox WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if row is None:
                return False
            if row["next_attempt"] is not None and now < row["next_attempt"]:
                return False
            count = row["reconciliations"] + 1
            self.db.execute(
                "UPDATE outbox SET reconciliations=?, next_attempt=? "
                "WHERE batch_id=?",
                (count, now + _backoff_seconds(60.0, 3600.0, count), batch_id),
            )
            return True
    def reconcile_due(self, batch_id):
        """True when one bounded read-only reconciliation may run now.

        There is no attempt cap: exhaustion is never proof of failure and
        never permission to resend; the persisted backoff merely spaces the
        checks out to at most one per hour."""
        return self._operation_due(batch_id)
    def retry_due(self, batch_id):
        """True when one bounded resubmission of an unavailable batch may run."""
        return self._operation_due(batch_id)
    def defer_operation_until(self, batch_id, retry_at):
        """Raise the persisted next-attempt floor to a valid ``retry_at``.

        ``retry_at`` is a Retry-After receipt hint (finite Unix-epoch
        seconds). Only a valid non-bool finite number is applied; a past or
        smaller value never shortens the existing persisted backoff (the
        floor is the maximum of both). Returns True when the hint was valid.
        """
        if not _valid_retry_at(retry_at):
            return False
        with self._write_txn():
            self.db.execute(
                "UPDATE outbox SET next_attempt=MAX(COALESCE(next_attempt, 0), ?) "
                "WHERE batch_id=?",
                (float(retry_at), batch_id),
            )
        return True
    def outbox_unresolved(self, *, limit=None, due_only=False):
        """Attempted but outcome-unknown batches needing bounded reconciliation."""
        with self.lock:
            return self._outbox_rows("status='unknown' OR (status='pending' AND attempted IS NOT NULL)",
                                     limit=limit, due_only=due_only)
    def outbox_unavailable(self, *, limit=None, due_only=False):
        """Batches whose last send hit a transient environment failure.

        The stored payload is intact (nothing confirmed, nothing compacted);
        resubmission retries the exact same operation with persisted backoff.
        """
        with self.lock:
            return self._outbox_rows("status='unavailable' AND attempted IS NOT NULL",
                                     limit=limit, due_only=due_only)

    # ---------------------------------------------- confirmed-PR compaction

    CONFIRMED_BATCH = ("submitted", "updated", "unchanged")
    def _queue_publication_cleanup(self, batch_id, revision):
        """Persist exact known-outcome revisions before replacing their receipt."""
        key = "publication-cleanup:" + batch_id
        previous = self.feed_get(key) or {}
        pending = previous.get("pending_revisions", [])
        if not isinstance(pending, list) or any(not isinstance(item, str) for item in pending):
            raise ValueError("invalid publication cleanup revision queue")
        if previous.get("status") == "failed" and previous.get("revision"):
            pending = [*pending, previous["revision"]]
        self.feed_set(key, dict(status="pending", revision=revision,
                                pending_revisions=sorted(set([*pending, revision]))))
    def _cleanup_publication_staging(self, batch_id):
        """Clean resolved candidates; retain failures independently of publication."""
        from mindie_knowledge.materials.publication import CleanupReceiptError, cleanup_staged_batch

        with self._write_txn():
            key = "publication-cleanup:" + batch_id
            receipt = self.feed_get(key)
            failures, removed = {}, 0
            for revision in receipt["pending_revisions"]:
                try:
                    removed += int(cleanup_staged_batch(self.root, batch_id, revision))
                except TimeoutError:
                    raise
                except (OSError, ValueError) as exc:
                    failures[revision] = f"{type(exc).__name__}: {exc}"[:500]
            result = dict(staging=removed)
            updated = dict(status="failed" if failures else "complete", revision=receipt["revision"],
                           pending_revisions=sorted(failures))
            if failures:
                updated["detail"] = "; ".join(failures.values())[:1000]
                result.update(cleanup_status="failed", cleanup_error=updated["detail"])
            try:
                self.feed_set(key, updated)
            except Exception as exc:
                detail = (updated.get("detail", "cleanup finished")
                          + f"; cleanup receipt could not be saved: {type(exc).__name__}: {exc}")
                raise CleanupReceiptError(batch_id, detail) from exc
            if failures:
                from .dfx import failure
                failure('knowledge.publish', stage='cleanup', category='publication_cleanup_failed', reportable=False)
            return result
    def compact_confirmed(self, batch_id):
        """Keep an unmerged candidate readable; clean only resolved staging.

        The confirmed PR outcome stays recorded even if local cleanup fails.
        A matching local draft is retired only after that revision is present
        in the synchronized public main tree, never merely when a PR opens.
        """
        with self._write_txn():
            row = self.db.execute("SELECT * FROM outbox WHERE batch_id=?", (batch_id,)).fetchone()
            if row is None or row["status"] not in self.CONFIRMED_BATCH:
                return None
            batch = _stored_json(row["batch"], dict, "outbox batch")
            removed = dict(entries=0, revisions=0, captures=0, staging=0)
            refs = set()
            for ref in batch.get("entry_refs", []):
                parsed = _exact_revision_ref(ref)
                if parsed is None:
                    continue
                entry_id, revision = parsed
                refs.add(parsed)
                entry = self._row(entry_id)
                if (entry is not None and entry["feed_active"]
                        and entry["published_revision"] == revision
                        and entry["draft_revision"] == revision):
                    self.db.execute("UPDATE entries SET draft_revision=NULL WHERE entry_id=?", (entry_id,))
                    self._material_dirty.add(entry_id)
                    removed["entries"] += 1
                    removed["revisions"] += self._prune_body_history(entry_id)
            if row["generation"] and refs:
                removed["captures"] = self._clear_covered_captures(row["generation"], refs)
            revision = row["revision"]
            self._queue_publication_cleanup(batch_id, revision)
        removed.update(self._cleanup_publication_staging(batch_id))
        return removed
    def _clear_covered_captures(self, generation, batch_refs):
        """Clear summaries only when capture detail names a nonempty set of
        exact entry@revision refs that is a subset of this sent batch.
        Ambiguous or unversioned coverage is left intact — never invented
        from timestamps or from sharing an entry id with an older revision."""
        cleared = 0
        rows = self.db.execute(
            "SELECT id, detail FROM captures WHERE generation=? AND "
            "summary != '' AND status='organized'",
            (generation,),
        ).fetchall()
        for ident, detail in rows:
            covered = _capture_revision_refs(detail)
            if covered and covered <= batch_refs:
                self.db.execute(
                    "UPDATE captures SET summary='' WHERE id=?", (ident,)
                )
                cleared += 1
        return cleared
    def _record_sent_receipts(self, row, batch, *, actual_files=None):
        """Store the exact current per-file send identities, with no bodies."""
        row = dict(row)
        if not isinstance(row.get("head_sha"), str) or not row["head_sha"]:
            raise ValueError("confirmed contribution has no remote head identity")
        if any("/units/" in ref for ref in batch.get("entry_refs", [])):
            from ..graded.export import record_sent_receipts
            return record_sent_receipts(self, row, batch, actual_files)
        candidate = {item["path"]: item for item in batch.get("files", [])}
        actual = {item["path"]: item for item in actual_files or []}
        prepared = self.feed_get("prepared-package-bases:" + row["batch_id"])
        task_ids = {path.split("/")[1] for path in candidate
                    if path.startswith("tasks/") and path.endswith("/index.md")}
        if task_ids and (not isinstance(prepared, dict) or prepared.get("batch_revision") != row["revision"]
                         or set(prepared.get("source_revisions", {})) != task_ids
                         or set(prepared.get("entry_revisions", {})) != task_ids):
            raise ValueError("confirmed task publication lacks its exact prepared package/base identity receipt")
        sources = prepared["source_revisions"] if task_ids else {}
        frozen_entries = prepared["entry_revisions"] if task_ids else {}
        now = time.time()
        for ref in batch.get("entry_refs", []):
            parsed = _exact_revision_ref(ref)
            if parsed is None:
                continue
            entry_id, revision = parsed
            if entry_id in frozen_entries and revision != frozen_entries[entry_id]:
                continue  # a vote on a past observation is not this task package
            prefix = f"tasks/{entry_id}/"
            index_path = prefix + "index.md"
            if index_path not in candidate:
                continue  # a feedback reference is not a task publication
            files = []
            for path, item in sorted(candidate.items()):
                if not path.startswith(prefix):
                    continue
                proven = actual.get(path, item)
                if proven.get("sha256") is not None:
                    files.append(dict(path=path, sha256=proven["sha256"],
                                      revision=proven.get("revision") or revision))
            index = next((item for item in files if item["path"] == index_path), None)
            if index is None:
                raise ValueError("confirmed task contribution has no navigation index receipt")
            # Task packages do not merge observation text during publication:
            # every accepted file is either byte-identical or an exact-base update.
            receipt = dict(revision=revision, files=files, head_sha=row["head_sha"],
                           batch_id=row["batch_id"], batch_revision=row["revision"],
                           publication_target=(prepared or {}).get("publication_target"))
            if entry_id in sources:
                receipt["source_published_revision"] = sources[entry_id]
            self.db.execute("INSERT OR REPLACE INTO feed_state VALUES(?,?)",
                            ("sent-package:" + entry_id, canonical(receipt)))
            self.db.execute(
                "INSERT OR REPLACE INTO sent_receipts(entry_id,generation,sent_revision,path,sha256,"
                "head_sha,repository,pr_url,batch_id,updated,markers,batch_revision) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (entry_id, row.get("generation"), revision, index_path, index["sha256"],
                 row["head_sha"], ((prepared or {}).get("publication_target") or {}).get("repository"),
                 row.get("pr_url"), row["batch_id"], now, None, row["revision"]),
            )
    def sent_receipt(self, entry_id):
        """Last confirmed per-entry receipt (path/hash/revision/exact head)."""
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM sent_receipts WHERE entry_id=?", (entry_id,)
            ).fetchone()
        return dict(row) if row else None
    def sent_file_hash(self, entry_id):
        """The exact body hash this domain last sent for one entry, from the
        per-entry confirmed receipt (survives later lineage-row replacement)."""
        receipt = self.sent_receipt(entry_id)
        return receipt["sha256"] if receipt else None
    def sent_markers(self, entry_id):
        """Package updates use exact file identities, not observation markers."""
        return None
    def sent_package_files(self, entry_id):
        receipt = self.feed_get("sent-package:" + entry_id)
        if receipt is None:
            return []
        files = receipt.get("files")
        if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
            raise ValueError("invalid stored task file receipts")
        return files
