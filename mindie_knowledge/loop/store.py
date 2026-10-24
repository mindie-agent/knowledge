"""[Role] Authoritative `state-v4.sqlite3` (`mindie-store/4`) transaction coordinator for task headers, capture cursors, incremental index batches, and outbox receipts.

[Invariants]
- SQLite stores compact headers, cursors, grants, votes, and receipts; durable
  material bodies live in `MaterialStore` (`tasks/<id>/index.md` + `blocks/<hash>.md`).
- Every material mutation follows two-phase staging: stage candidate files ->
  commit SQLite metadata inside `_write_txn()` -> promote `current.json` pointers
  in `_finish_material_writes()`.
- Schema constants, validation and pure helpers live in `store_schema`; material,
  vote and outbox mutations live in their mixins. Public names are re-exported
  here so existing imports keep working.

[Timeouts & Locks]
- Opened via `open_database` in WAL mode (`timeout=5.0s`); in-process writes
  serialize on `self.lock` (`threading.RLock`) with `BEGIN IMMEDIATE`.

[Side-Effect Policy]
- Local SQLite and `MaterialStore` mutations only; never spawns subprocesses or
  performs network I/O.
"""
from __future__ import annotations

import contextlib
import json
import re
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
    QueryMixin,
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
from .store_captures import MaterialMixin
from .store_outbox import OutboxMixin


class Store(MaterialMixin, OutboxMixin, QueryMixin):

    def __init__(self, root, domain):
        if not isinstance(domain, str) or not documents.DOMAIN_RE.fullmatch(domain):
            raise ValueError("invalid domain")
        from ..state_layout import prepare_layout
        try:
            with prepare_layout(root, domain) as directory:
                self._open_state(directory, domain)
        except BaseException as error:
            for resource in (getattr(self, 'db', None), getattr(self, 'materials', None)):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception as cleanup:
                        error.add_note('State initialization cleanup also failed: ' + type(cleanup).__name__)
            raise
    def _open_state(self, directory, domain):
        self.domain = domain
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.lock = threading.RLock()
        from mindie_knowledge.materials.store import MaterialStore
        self.materials = MaterialStore(self.root / "materials", domain=domain)
        self._material_dirty = set()
        self._closed = False
        from ..owned_state import open_database
        self.db = open_database(
            self.root / "state-v4.sqlite3", schema=SCHEMA,
            required=_STATE_COLUMNS, initialize=_initialize_state, validate=_validate_state,
            validate_current=_validate_schema_identity,
            residue=(self.root / "materials" / "current.json",
                     self.root / "materials" / ".current-catalog.sqlite3",
                     self.root / "materials" / "tasks", self.root / "outbox"),
        )
        self.db.execute('BEGIN IMMEDIATE')
        try:
            snapshot = {}
            for row in self.db.execute('SELECT entry_id,draft_revision,published_revision,feed_active FROM entries'):
                revisions = {}
                if row['draft_revision']:
                    revisions['draft'] = row['draft_revision']
                if row['feed_active'] and row['published_revision']:
                    revisions['feed'] = row['published_revision']
                snapshot[row['entry_id']] = revisions
            current = self.materials._pointers()
            if (set(current['draft']) | set(current['feed'])) - set(snapshot):
                raise ValueError('committed material has no runtime metadata; existing files were preserved')
            self.materials.recover_snapshot(snapshot, retained=self.retained_material_revisions())
            self.db.executemany("INSERT OR IGNORE INTO known_revisions VALUES(?,?)",
                                [(entry_id, revision) for entry_id, revisions in snapshot.items()
                                 for revision in set(revisions.values())])
        except Exception:
            self.db.rollback()
            self.db.close()
            self.materials.close()
            raise
        self.db.commit()

    @property
    def graded(self):
        from ..graded.repository import GradedRepository
        if not hasattr(self, "_graded_repository"):
            self._graded_repository = GradedRepository(self)
        return self._graded_repository

    @contextlib.contextmanager
    def _write_txn(self):
        """Serialize read-modify-write across processes sharing this root."""
        with self.lock, self.materials.validated_headers():
            # A nested _write_txn shares the outer transaction's proven
            # authority; only the first frame of a transaction pays the path
            # check. This keeps lock-held hot paths from repeating lstat.
            if self.db.in_transaction:
                yield
                return
            self.db.assert_authority()
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.rollback()
                raise
            self.db.commit()
            self._finish_material_writes()
    def close(self):
        error = None
        self._closed = True
        for operation in (self._finish_material_writes, self.materials.close, self.db.close):
            try:
                operation()
            except BaseException as exc:
                if error is None:
                    error = exc
                else:
                    error.add_note(f"store close also failed: {type(exc).__name__}")
        if error is not None:
            raise error

    _VISIBLE_SQL = "feed_active=1 OR (draft_revision IS NOT NULL AND published_revision IS NULL)"

    @staticmethod
    def _header(doc):
        return canonical({key: value for key, value in doc.items() if key != 'content'})
    def _finish_material_writes(self):
        if not self._material_dirty:
            return
        self.db.execute('BEGIN IMMEDIATE')
        try:
            snapshot = {}
            for entry_id in tuple(self._material_dirty):
                row = self._row(entry_id)
                revisions = {}
                if row is not None:
                    if row['draft_revision']:
                        revisions['draft'] = row['draft_revision']
                    if row['feed_active'] and row['published_revision']:
                        revisions['feed'] = row['published_revision']
                snapshot[entry_id] = revisions
            self.materials.retain_snapshot(snapshot, retained=self.retained_material_revisions())
        except Exception as exc:
            self.db.rollback()
            if getattr(exc, 'committed', False):
                self._material_dirty.difference_update(snapshot)
            exc.metadata_committed = True
            raise
        self.db.commit()
        self._material_dirty.difference_update(snapshot)
    def _mark_material_changed(self, entry_id):
        self._material_dirty.add(entry_id)
    def _mark_all_material_changed(self):
        self._material_dirty.update(r[0] for r in self.db.execute('SELECT entry_id FROM entries'))
    def search_index_status(self):
        with self.lock:
            visible = self.db.execute('SELECT count(*) FROM entries WHERE ' + self._VISIBLE_SQL).fetchone()[0]
            status = self.materials.index_status()
            return dict(status, visible=visible, requeued=len(self._material_dirty))
    def advance_search_index(self, **_budgets):
        with self.lock:
            self._finish_material_writes()
            self.materials.refresh_index()
        return self.search_index_status()
    def grant(self, kind, identity, revision, generation):
        """Durably authorize exactly one material revision for one sharing
        generation. Anything without an explicit grant is local-only and is
        never selected for a batch, however it was created."""
        if kind not in {"draft", "vote"}:
            raise ValueError("grant kind must be draft or vote")
        if not isinstance(generation, str) or not generation:
            raise ValueError("grant requires a nonempty sharing generation")
        with self._write_txn():
            self.db.execute(
                "INSERT OR REPLACE INTO grants VALUES(?,?,?,?,?)",
                (kind, identity, revision, generation, time.time()),
            )
    def granted(self, kind, identity, revision, generation):
        with self.lock:
            return (
                self.db.execute(
                    "SELECT 1 FROM grants WHERE kind=? AND identity=? "
                    "AND revision=? AND generation=?",
                    (kind, identity, revision, generation),
                ).fetchone()
                is not None
            )

    @staticmethod
    def vote_identity(root_opaque, entry_id):
        return f"{root_opaque}:{entry_id}"


    # ------------------------------------------------------------------ refs

    WITHDRAWN_NOTE = (
        "withdrawn from the published knowledge base by upstream deletion; "
        "the published body is no longer retained"
    )
    def ref(self, entry_id, revision=None):
        base = f"mindie://{self.domain}/{entry_id}"
        return f"{base}@{revision}" if revision else base
    def _unique_token(self, value, rows):
        """16-hex prefix when it names exactly one known value, else the full
        hash — a rendered reference must always resolve back precisely."""
        prefix = value[:16]
        return value if any(other != value for other in rows) else prefix
    def _short_ref(self, entry_id, revision):
        with self.lock:
            entries = [r[0] for r in self.db.execute(
                "SELECT entry_id FROM entries WHERE entry_id LIKE ?",
                (entry_id[:16] + "%",),
            )]
            revisions = [r[0] for r in self.db.execute(
                "SELECT revision FROM revisions WHERE entry_id=? AND revision LIKE ?",
                (entry_id, revision[:16] + "%"),
            )]
        return self.ref(
            self._unique_token(entry_id, entries),
            self._unique_token(revision, revisions),
        )
    def _resolve_entry(self, token):
        if len(token) == 64:
            return token
        rows = [r[0] for r in self.db.execute(
            "SELECT entry_id FROM entries WHERE entry_id LIKE ?", (token + "%",)
        )]
        if not rows:
            raise ValueError("unknown reference in this domain")
        if len(rows) > 1:
            raise ValueError("ambiguous reference prefix; use the full 64-hex identity")
        return rows[0]
    def _resolve_revision(self, entry_id, token):
        if len(token) == 64:
            return token
        rows = [r[0] for r in self.db.execute(
            "SELECT revision FROM revisions WHERE entry_id=? AND revision LIKE ?",
            (entry_id, token + "%"),
        )]
        if not rows:
            raise ValueError("unknown pinned revision in this domain")
        if len(rows) > 1:
            raise ValueError("ambiguous pinned revision prefix; use the full 64-hex revision")
        return rows[0]
    def _parse_ref(self, ref):
        """Resolve a reference to a full ``(entry_id, revision)`` pair.

        Accepts the full ``mindie://domain/<64-hex>[@<64-hex>]`` form and the
        short 16-hex prefix form (with or without the scheme). Prefixes must
        name exactly one known value; ambiguity fails, never picks the first.
        Caller must hold the lock: resolution reads the catalogue."""
        prefix = f"mindie://{self.domain}/"
        text = ref if isinstance(ref, str) else ""
        if text.startswith(prefix):
            text = text[len(prefix):]
        elif not re.fullmatch(r"[0-9a-f]{16,64}(@[0-9a-f]{16,64})?", text or ""):
            raise ValueError("reference is outside the selected domain")
        entry, _, revision = text.partition("@")
        if not re.fullmatch(r"[0-9a-f]{16,64}", entry):
            raise ValueError("reference is outside the selected domain")
        if revision and not re.fullmatch(r"[0-9a-f]{16,64}", revision):
            raise ValueError("invalid pinned revision")
        entry_id = self._resolve_entry(entry)
        return entry_id, self._resolve_revision(entry_id, revision) if revision else None
    def _row(self, entry_id):
        return self.db.execute(
            "SELECT * FROM entries WHERE entry_id=?", (entry_id,)
        ).fetchone()
    def _revision_doc(self, entry_id, revision):
        row = self.db.execute('SELECT 1 FROM revisions WHERE entry_id=? AND revision=?',
                              (entry_id, revision)).fetchone()
        return self.materials.get_document(entry_id, revision=revision) if row else None
    def _withdrawn(self, row):
        """Published once, no longer carried by the feed tree after a
        successful sync. Local drafts never resurrect it."""
        return bool(
            row is not None and row["published_revision"] and not row["feed_active"]
        )
    def get(self, ref):
        """Internal document inspection for storage/publication verification.

        This is not the public reading interface. ``explain`` reads current
        navigation or one exact block and never calls this whole-body helper.
        """
        with self.lock:
            entry_id, revision = self._parse_ref(ref)
            row = self._row(entry_id)
            if row is not None and self._withdrawn(row) and not revision:
                return dict(json.loads(row['doc']), content='', withdrawn=True, note=self.WITHDRAWN_NOTE)
            if revision:
                doc = self._revision_doc(entry_id, revision)
                if doc is None:
                    raise ValueError("unknown pinned revision in this domain")
            else:
                if row is None:
                    raise ValueError("unknown reference in this domain")
                doc = self._revision_doc(entry_id, json.loads(row["doc"])["revision"])
            if self._withdrawn(row):
                return dict(doc, withdrawn=True, note=self.WITHDRAWN_NOTE)
            return dict(doc, withdrawn=False)
    def is_withdrawn(self, ref):
        """Publication checks current withdrawal state, not a past draft body."""
        if isinstance(ref, str) and "/units/" in ref:
            from ..graded.feedback import parse_feedback_ref
            observed = parse_feedback_ref(ref, self.domain)
            with self.lock:
                catalog = self.graded._catalog()
                ident = observed["unit_id"]
                if ident in {item["id"] for item in catalog.get("units", [])}:
                    return False
                if ident in {item["id"] for item in catalog["retired_local"]} or self.db.execute(
                        "SELECT 1 FROM known_revisions WHERE entry_id=? LIMIT 1", (ident,)).fetchone():
                    return True
                raise ValueError("unknown graded reference in this domain")
        with self.lock:
            entry_id, _ = self._parse_ref(ref.split("@", 1)[0])
            row = self.db.execute(
                "SELECT published_revision, feed_active FROM entries WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown reference in this domain")
            return self._withdrawn(row)
    def explain(self, ref=None, **kwargs):
        """Current task navigation, one admitted block, or a graded unit set.

        Block identity binds file bytes, independently of changing task
        navigation. No full-document assembly, historical package or old
        offset-based reading path is reachable through this interface.
        """
        supported = {"refs", "conditions", "known_refs", "token_budget", "run_id",
                     "mode", "ledger_entries", "bottleneck_summary", "native_subcontext", "goal"}
        unknown = set(kwargs) - supported
        if unknown:
            raise TypeError(f"explain() got an unexpected keyword argument {sorted(unknown)[0]!r}")
        refs = kwargs.get("refs")
        if ref is None and refs is None:
            raise ValueError("explain requires ref or refs")
        if refs is not None or (isinstance(ref, str) and "/units/" in ref):
            # An omitted or explicitly null argument keeps `graded.explain`'s own
            # default rather than forwarding None into its validated signature.
            values = {"ref": ref, "refs": refs, **{key: kwargs.get(key) for key in supported}}
            return self.graded.explain(
                **{key: value for key, value in values.items() if value is not None})
        parsed = parse_read_ref(ref, domain=self.domain)
        entry_id = parsed["task_id"]
        navigation_ref = task_ref(self.domain, entry_id)
        with self.lock:
            row = self._row(entry_id)
            if row is not None and self._withdrawn(row):
                raise ReadReferenceError("withdrawn", "The task was withdrawn; its material is unavailable.")
            if row is None or not (row["feed_active"] or row["draft_revision"]):
                raise ReadReferenceError("removed_or_superseded", "The task is not present in the current material collection.")
            source = "feed" if row["feed_active"] else "draft"
            revision = row["published_revision"] if source == "feed" else row["draft_revision"]
            material = self.materials.read_current(
                entry_id, source=source, revision=revision,
                block_id=parsed.get("block_id"), sha256=parsed.get("sha256"),
            )
            header = material["header"]
            entry, blocks = header["entry"], header["blocks"]

            def reference(descriptor):
                return block_ref(self.domain, entry_id, descriptor["block_id"], descriptor["sha256"])

            navigation = dict(ref=navigation_ref, title=entry["title"], summary=header["navigation"],
                              revision=revision, advisory=True)
            result = dict(kind=parsed["kind"], ref=ref, task_ref=navigation_ref,
                          entry_id=entry_id, domain=self.domain, source=source,
                          current_revision=revision, current_navigation=navigation,
                          feedback_ref=feedback_ref(self.domain, entry_id, revision),
                          block_count=len(blocks),
                          note="Reference material. Current navigation is fallible metadata, not verification of a block's claims.")
            if parsed["kind"] == "task":
                return dict(result, title=entry["title"], navigation=header["navigation"],
                            first_block_ref=reference(blocks[0]) if blocks else None)
            position = material["position"]
            descriptor = material["block"]
            return dict(result, block_id=descriptor["block_id"], sha256=descriptor["sha256"],
                        title=descriptor["title"], summary=descriptor["summary"],
                        source_range=descriptor["source_range"], content=material["content"],
                        previous_block_ref=reference(blocks[position - 1]) if position else None,
                        next_block_ref=reference(blocks[position + 1]) if position + 1 < len(blocks) else None)

    # ---------------------------------------------------------------- drafts
    def add_capture(self, *, root_session, session, turn, transcript, summary,
                    generation=None, boundary=None, scope=None, namespace="",
                    activation_epoch=None, kind="turn", event_key=None):
        if kind not in _IDENTITY_KINDS:
            raise ValueError("invalid identity kind")
        if kind == "notification":
            if turn not in (None, ""):
                raise ValueError("notification identity is not a turn_id")
            if not isinstance(event_key, str) or not event_key.strip() or len(event_key) > 256:
                raise ValueError("event_id must be nonempty text of at most 256 characters")
            if event_key != event_key.strip() or "\x00" in event_key:
                raise ValueError("event_id must be nonempty text of at most 256 characters")
            turn = ""
        elif not isinstance(turn, str) or not turn.strip() or len(turn) > 256:
            raise ValueError("turn_id must be nonempty text of at most 256 characters")
        elif event_key:
            raise ValueError("turn identity does not take an event_id")
        if not isinstance(summary, str) or len(summary) > 32768:
            raise ValueError("summary exceeds the bounded envelope")
        if namespace and not _HARNESS_RE.fullmatch(namespace):
            raise ValueError("invalid capture namespace")
        with self._write_txn():
            result = commit_capture(
                self.db, namespace=namespace, root_session=root_session,
                session=session, turn=turn, transcript=transcript,
                summary=summary.strip(), generation=generation, boundary=boundary,
                scope=scope, activation_epoch=activation_epoch,
                kind=kind, event_key=event_key,
            )
        if result.get("revoked") is None:
            result.pop("revoked", None)
        return result
    def capture_row(self, ident):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM captures WHERE id=?", (ident,)
            ).fetchone()
        return dict(row) if row else None
    def capture_material_receipt(self, ident):
        """One event's body receipt; errors and continuation reasons are separate.

        Capture identities remain durable for duplicate suppression, so this
        bounded metadata has the same lifetime. It never contains body text.
        An invalid saved receipt is a fault, not evidence of an empty event.
        """
        with self.lock:
            row = self.db.execute("SELECT value FROM state WHERE key=?",
                                  ("capture-material:" + ident,)).fetchone()
        if row is None:
            return None
        receipt = _stored_json(row[0], dict, "capture material receipt")
        return validate_capture_material_receipt(receipt, self.domain)
    def record_capture_material(self, ident, receipt):
        """Commit the receipt in the same transaction as the body and cursor."""
        receipt = validate_capture_material_receipt(receipt, self.domain)
        with self._write_txn():
            if self.capture_row(ident) is None:
                raise ValueError("capture material receipt has no capture event")
            self.capture_material_receipt(ident)
            self.db.execute("INSERT OR REPLACE INTO state VALUES(?,?)",
                            ("capture-material:" + ident, canonical(receipt)))
            self.mark_capture(ident, "organized", canonical(receipt))
    def finish_capture_eof(self, ident):
        with self._write_txn():
            receipt = self.capture_material_receipt(ident)
            if receipt is not None:
                self.mark_capture(ident, "organized", canonical(receipt))
            else:
                self.mark_capture(ident, "no-new-material", "eof settled; no new material")
    def mark_capture(self, ident, status, detail="", *, error_code='capture_failed', failed_stage='projection'):
        with self._write_txn():
            previous = self.capture_row(ident)
            if status in {"cancelled", "discarded"}:
                self.db.execute(
                    "UPDATE captures SET status=?, detail=?, transcript=NULL, "
                    "summary='' WHERE id=?",
                    (status, str(detail)[:1000], ident),
                )
            else:
                self.db.execute(
                    "UPDATE captures SET status=?, detail=? WHERE id=?",
                    (status, str(detail)[:1000], ident),
                )
            if status not in {"queued", "pending", "deferred"}:
                self.db.execute("DELETE FROM continuations WHERE capture_id=?", (ident,))
        if status == 'failed' and previous is not None and previous['status'] != 'failed':
            from .dfx import failure
            failure('knowledge.capture', stage=failed_stage, category=error_code, reportable=False)
    def continuation_reason(self, ident):
        with self.lock:
            row = self.db.execute(
                "SELECT reason FROM continuations WHERE capture_id=?", (ident,)
            ).fetchone()
        return row[0] if row else None
    def defer_capture(self, ident, *, due, reason, eligible=1):
        with self._write_txn():
            self.db.execute("UPDATE captures SET status='pending', detail=? WHERE id=?",
                            (reason[:1000], ident))
            _upsert_continuation(self.db, ident, due, reason[:1000], eligible)
    def dormant_capture(self, ident, *, reason):
        """Ineligible until a later event or explicit resume. Due is not a year."""
        with self._write_txn():
            self.db.execute(
                "UPDATE captures SET status='pending', detail=? WHERE id=?",
                (reason[:1000], ident),
            )
            _upsert_continuation(self.db, ident, 0, reason[:1000], False)
    def due_capture(self):
        with self.lock:
            row = self.db.execute(
                "SELECT c.id FROM captures c LEFT JOIN continuations q ON q.capture_id=c.id "
                "WHERE c.status IN ('queued','pending','deferred') "
                "AND COALESCE(q.eligible, 1) != 0 "
                "AND COALESCE(q.due,0)<=? ORDER BY COALESCE(q.due,0), c.created LIMIT 1",
                (time.time(),),
            ).fetchone()
            return row[0] if row else None
    def cursor(self, file_identity):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM cursors WHERE file_identity=?", (file_identity,)
            ).fetchone()
        return dict(row) if row else None
    def reserve_region(self, *, capture_id, file_identity, identity, start, finish,
                       region_digest, observed_cursor, status="attempted", detail=""):
        """Reserve one region and advance the shared cursor, or write nothing.

        ``observed_cursor`` is the row the caller saw before this read: None
        if that read saw no cursor, otherwise its ``identity`` and ``finish``.
        ``identity`` is the new file identity to store after the compare
        succeeds. A short file may grow its anchor; that new identity is not
        the compare value. A conflicting reader gets None. Nothing is written
        and the cursor is not moved backward.
        """
        ident = digest(["region", capture_id, file_identity, start, finish, region_digest])
        now = time.time()
        start = int(start)
        finish = int(finish)
        with self._write_txn():
            cursor = self.db.execute(
                "SELECT identity, finish FROM cursors WHERE file_identity=?",
                (file_identity,),
            ).fetchone()
            if observed_cursor is None:
                if cursor is not None or start != 0:
                    return None
            else:
                if cursor is None or not isinstance(observed_cursor, dict):
                    return None
                expected_finish = int(observed_cursor["finish"])
                expected_identity = observed_cursor.get("identity") or ""
                if int(cursor["finish"]) != expected_finish:
                    return None
                if (cursor["identity"] or "") != expected_identity:
                    return None
                if start != expected_finish:
                    return None
            already = self.db.execute(
                "SELECT id FROM regions WHERE id=?", (ident,)
            ).fetchone()
            if already is not None:
                return None
            self.db.execute(
                "INSERT INTO regions(id, capture_id, file_identity, start, "
                "finish, digest, status, detail, created, recovery, identity) "
                "VALUES(?,?,?,?,?,?,?,?,?,0,?)",
                (ident, capture_id, file_identity, start, finish, region_digest,
                 status, str(detail)[:1000], now, identity or None),
            )
            self.db.execute(
                "INSERT INTO cursors VALUES(?,?,?,?,0,?) "
                "ON CONFLICT(file_identity) DO UPDATE SET "
                "identity=excluded.identity, finish=excluded.finish, "
                "digest=excluded.digest, updated=excluded.updated",
                (file_identity, identity or "", finish, region_digest, now),
            )
        return ident
    def schedule_continuation(self, ident, *, due, reason, eligible=1):
        """Update one continuation without changing capture status."""
        with self._write_txn():
            _upsert_continuation(self.db, ident, due, reason[:1000], eligible)
    def finish_region(self, ident, status, detail=""):
        ok = status == "succeeded"
        with self._write_txn():
            row = self.db.execute(
                "SELECT file_identity, finish FROM regions WHERE id=?", (ident,)
            ).fetchone()
            self.db.execute(
                "UPDATE regions SET status=?, detail=? WHERE id=?",
                (status, str(detail)[:1000], ident),
            )
            if ok and row is not None:
                self.db.execute(
                    "UPDATE cursors SET ok_finish=MAX(ok_finish, ?), updated=? "
                    "WHERE file_identity=?",
                    (row["finish"], time.time(), row["file_identity"]),
                )
    def coverage_gaps(self, file_identity=None):
        with self.lock:
            if file_identity is None:
                rows = self.db.execute(
                    "SELECT * FROM regions WHERE status IN ('failed','cancelled') "
                    "ORDER BY created"
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT * FROM regions WHERE file_identity=? AND status IN "
                    "('failed','cancelled') ORDER BY created",
                    (file_identity,),
                ).fetchall()
        return [dict(r) for r in rows]


    # ---------------------------------------------------------------- legacy

    # Older runtime databases remain inert on disk; there is no runtime
    # compatibility layer. Explicitly selected transcript imports use the
    # current public-material pipeline rather than reading legacy databases.

    # ---------------------------------------------------------------- status
    def _capture_status(self, row):
        result = dict(row)
        try:
            result["material_receipt"] = self.capture_material_receipt(row["id"])
        except ValueError as exc:
            # Keep a page's actual error alongside the separate receipt fault.
            result["material_receipt_error"] = str(exc)
        return result
    def status(self):
        with self.lock:
            counts = {
                r[0]: r[1]
                for r in self.db.execute(
                    "SELECT CASE WHEN feed_active=1 THEN 'published' "
                    "WHEN draft_revision IS NOT NULL THEN 'draft' ELSE 'archived' END "
                    "bucket, count(*) FROM entries GROUP BY bucket"
                )
            }
            return dict(
                domain=self.domain,
                schema=SCHEMA,
                entries=counts,
                transcript_summaries={r[0]: r[1] for r in self.db.execute(
                    f"SELECT {SUMMARY_STATUS_SQL} AS visible_status, count(*) "
                    "FROM transcript_tasks t GROUP BY visible_status")},
                withdrawn=self.db.execute(
                    "SELECT count(*) FROM entries WHERE published_revision "
                    "IS NOT NULL AND feed_active=0"
                ).fetchone()[0],
                captures=[
                    self._capture_status(r)
                    for r in self.db.execute(
                        "SELECT id, status, detail FROM captures "
                        "ORDER BY created DESC LIMIT 20"
                    )
                ],
                coverage_gaps=len(self.coverage_gaps()),
                votes=self.db.execute("SELECT count(*) FROM votes").fetchone()[0],
                quarantined_entries=[
                    dict(r)
                    for r in self.db.execute(
                        "SELECT entry_id, kind, detail FROM entry_quarantine "
                        "ORDER BY created DESC LIMIT 20"
                    )
                ],
                export_attempts=[json.loads(r[0]) for r in self.db.execute(
                    "SELECT value FROM state WHERE key LIKE 'export:%' "
                    "ORDER BY rowid DESC LIMIT 20"
                )],
                outbox=[
                    dict(r)
                    for r in self.db.execute(
                        "SELECT batch_id, status, detail, pr_url, attempted "
                        "FROM outbox ORDER BY created DESC LIMIT 20"
                    )
                ],
                feeds={
                    r[0]: json.loads(r[1])
                    for r in self.db.execute("SELECT key, value FROM feed_state")
                },
            )


__all__ = [
    "DraftFull",
    "Store",
    "canonical",
    "digest",
    "new_identity",
    "session_key",
]
