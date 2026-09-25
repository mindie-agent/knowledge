"""Versioned local knowledge store, scoped to one domain.

This is the ``mindie-store/3`` runtime store (``store-v3.sqlite3``) serving
canonical ``mindie-entry/2`` documents. It replaces content-as-identity with
stable opaque entry IDs plus an explicit revision history: every known body —
local draft revisions and published revisions installed from the content
repository — is retained, so an old pinned reference always reads the exact
historical bytes while ordinary search shows the current published version.
Local drafts update by append-only, marker-deduplicated observations; draft
ownership lives in a private entry-owner relation, never in a downloaded
document. Withdrawal is upstream deletion: an entry the feed tree no longer
carries leaves ordinary search, is never resurrected by its local draft, and
its retained pinned reads carry an explicit ``withdrawn`` flag and note.

Per explicit user steering there is no legacy compatibility layer: the public
knowledge base restarts empty in this format, old public migration is
cancelled, and pre-existing private user files stay inert (never opened,
never deleted). The runtime never creates or reads the obsolete
``ledger.sqlite3``.

SQLite is the serving catalogue and event ledger. Rendered draft Markdown is
an inspectable export, not an independently editable input.
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

from mindie_knowledge.markdown import _atomic_write_text

from . import documents
from .documents import DraftFull

SCHEMA = "mindie-store/3"
MAX_VOTE_REASON = 1000
RATINGS = ("up", "down")
# A rejected lineage row may be replaced by a NEW batch of other material:
# the rejection quarantines the rejected entries, never the whole domain.
REPLACEABLE_BATCH = frozenset({"submitted", "updated", "unchanged", "needs_review",
                               "failed", "rejected"})

# Batch receipts that must never automatically write again.
TERMINAL_BATCH = ("submitted", "updated", "unchanged", "needs_review", "failed",
                  "rejected", "unknown", "disabled", "unavailable")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _backoff_seconds(base, cap, attempt):
    """Exponential backoff; the exponent is clamped before computing so a
    long-failing persisted counter can never overflow to an error."""
    return min(cap, base * (2.0 ** min(max(0, attempt - 1), 20)))


class IndexNotReady(ValueError):
    """The derived search index is being built or rebuilt.

    A brief honest readiness state (existing read-rejection shape): never a
    fake empty result and never a failure of the queried content. Callers
    that merely wanted OPTIONAL retrieval context (the organizer payload)
    must catch exactly this type and continue without it; anything else still
    propagates."""


def session_key(value):
    """Local hash of a native session id; never exported."""
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("session_id must be nonempty text of at most 256 characters")
    return digest(value.strip())


def _exact_revision_ref(ref):
    """Return ``(entry_id, revision)`` for one nonempty versioned ref.

    Unversioned, slash-less, or otherwise unparseable refs are refused — they
    never count as coverage of a confirmed sent revision.
    """
    if not isinstance(ref, str) or "/" not in ref:
        return None
    token = ref.rsplit("/", 1)[-1]
    entry_id, sep, revision = token.partition("@")
    if not sep or not entry_id or not revision:
        return None
    return entry_id, revision


def _capture_revision_refs(detail):
    """Exact ``(entry, revision)`` pairs named by an organized capture.

    Returns None when coverage is empty, unversioned, mixed with unparseable
    refs, or otherwise ambiguous — the caller must leave that capture intact.
    """
    if not isinstance(detail, str) or not detail:
        return None
    try:
        payload = json.loads(detail)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    refs = payload.get("refs")
    if not isinstance(refs, list) or not refs:
        return None
    covered = set()
    for ref in refs:
        parsed = _exact_revision_ref(ref)
        if parsed is None:
            return None
        covered.add(parsed)
    return covered if covered else None


def new_identity():
    return secrets.token_hex(32)


_HARNESS_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_UNPROCESSED_CAPTURE = frozenset({"queued", "pending", "deferred"})
_IDENTITY_KINDS = frozenset({"turn", "notification"})
_REARM_REASONS = (
    "reason='incomplete-tail' OR reason='admission-unreadable' "
    "OR reason='cursor-conflict' OR reason LIKE 'cursor-conflict:%' "
    "OR reason LIKE 'eof-settle:%' OR reason LIKE 'admission-unreadable:%'"
)


def capture_identity(namespace, session, kind, key):
    """Stable id for one session plus a turn id or a notification event id."""
    if kind not in _IDENTITY_KINDS:
        raise ValueError("invalid identity kind")
    return digest(["capture", kind, namespace or "", session, key.strip()])


def legacy_session_capture_identity(namespace, session, turn):
    """Previous handoff id: harness, session, and native turn. Turn kind only."""
    return digest(["capture", namespace or "", session, turn.strip()])


def legacy_capture_identity(root_hash, turn):
    """Pre-handoff id. Ownership hash plus turn; not safe across sibling sessions."""
    return digest(["capture", root_hash, turn.strip()])


def _capture_identity_row(db, ident):
    return db.execute(
        "SELECT id, status, session, generation, activation_epoch "
        "FROM captures WHERE id=?",
        (ident,),
    ).fetchone()


def _upsert_continuation(db, ident, due, reason, eligible):
    db.execute(
        "INSERT INTO continuations(capture_id, due, reason, eligible) VALUES(?,?,?,?) "
        "ON CONFLICT(capture_id) DO UPDATE SET due=excluded.due, "
        "reason=excluded.reason, eligible=excluded.eligible",
        (ident, due, reason, 1 if eligible else 0),
    )


def _hold_capture(db, ident):
    """Park a paused capture without a fictitious future due time."""
    db.execute(
        "UPDATE captures SET status='pending', detail=? WHERE id=?",
        ("maintenance-paused", ident),
    )
    _upsert_continuation(db, ident, 0, "maintenance-paused", False)


def _valid_retry_at(value):
    """A Retry-After hint is a finite Unix-epoch number — never a bool, NaN
    or infinity (anything else is ignored, never honored)."""
    return (
        value is not None
        and not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def commit_capture(
    db, *, namespace, root_session, session, turn, transcript, summary,
    generation, boundary, scope, activation_epoch, hold=None,
    kind="turn", event_key=None,
):
    """Insert or adopt one capture. The caller holds the write transaction.

    A repeat of the same kind and key returns the existing row. Turn identity
    still adopts this session's older id formulas. An unprocessed row whose
    activation epoch or sharing generation no longer matches is cancelled in
    place; the id stays so the event is not captured again. ``processing``
    and terminal rows are not rewritten. A repeat makes a dormant tail or
    admission wait eligible again. It does not clear a maintenance pause.
    """
    if kind not in _IDENTITY_KINDS:
        raise ValueError("invalid identity kind")
    if kind == "notification":
        key = (event_key or "").strip()
        turn_value = ""
        stored_event = key
    else:
        kind = "turn"
        key = turn.strip()
        turn_value = key
        stored_event = None
    ident = capture_identity(namespace, session, kind, key)
    row = _capture_identity_row(db, ident)
    if row is None and kind == "turn":
        for candidate in (
            legacy_session_capture_identity(namespace, session, key),
            legacy_capture_identity(root_session, key),
        ):
            if candidate == ident:
                continue
            found = _capture_identity_row(db, candidate)
            if found is not None and found["session"] == session:
                row = found
                ident = found["id"]
                break
    if row is not None:
        status = row["status"]
        epoch_changed = bool(activation_epoch) and row["activation_epoch"] != activation_epoch
        generation_changed = (
            bool(generation) and bool(row["generation"]) and row["generation"] != generation
        )
        if status in _UNPROCESSED_CAPTURE and (epoch_changed or generation_changed):
            detail = (
                "activation epoch changed before processing"
                if epoch_changed
                else "settings generation changed before processing"
            )
            db.execute(
                "UPDATE captures SET status='cancelled', detail=?, transcript=NULL, "
                "summary='' WHERE id=?",
                (detail, ident),
            )
            db.execute("DELETE FROM continuations WHERE capture_id=?", (ident,))
            return dict(
                id=ident, status="cancelled", duplicate=True,
                revoked="activation-revoked" if epoch_changed else "generation-revoked",
            )
        if hold == "maintenance-paused" and status == "queued":
            _hold_capture(db, ident)
            return dict(id=ident, status="pending", duplicate=True, revoked=None)
        if status in _UNPROCESSED_CAPTURE:
            db.execute(
                "UPDATE continuations SET due=?, eligible=1 "
                f"WHERE capture_id=? AND ({_REARM_REASONS})",
                (time.time(), ident),
            )
        return dict(id=ident, status=status, duplicate=True, revoked=None)
    db.execute(
        """INSERT INTO captures(
            id, root_session, session, turn, transcript, summary, status,
            detail, created, generation, boundary, scope, activation_epoch,
            identity_kind, event_key
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ident, root_session, session, turn_value, transcript, summary, "queued", "",
            time.time(), generation, boundary, scope, activation_epoch,
            kind, stored_event,
        ),
    )
    if hold == "maintenance-paused":
        _hold_capture(db, ident)
        return dict(id=ident, status="pending", duplicate=False, revoked=None)
    return dict(id=ident, status="queued", duplicate=False, revoked=None)


class Store:
    def __init__(self, root, domain):
        if not isinstance(domain, str) or not documents.DOMAIN_RE.fullmatch(domain):
            raise ValueError("invalid domain")
        self.domain = domain
        self.root = Path(root).resolve() / domain
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(
            self.root / "store-v3.sqlite3", check_same_thread=False
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS entries(entry_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL, title TEXT NOT NULL,
                origin TEXT NOT NULL, draft_revision TEXT, published_revision TEXT,
                feed_active INTEGER NOT NULL DEFAULT 0, batched_revision TEXT,
                doc TEXT NOT NULL, updated REAL NOT NULL,
                conditions TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS revisions(entry_id TEXT NOT NULL,
                revision TEXT NOT NULL, doc TEXT NOT NULL, source TEXT NOT NULL,
                created REAL NOT NULL, PRIMARY KEY(entry_id, revision));
            CREATE TABLE IF NOT EXISTS captures(id TEXT PRIMARY KEY,
                root_session TEXT NOT NULL, session TEXT NOT NULL, turn TEXT NOT NULL,
                transcript TEXT, summary TEXT NOT NULL, status TEXT NOT NULL,
                detail TEXT NOT NULL, created REAL NOT NULL,
                generation TEXT, boundary REAL, scope TEXT,
                activation_epoch TEXT, identity_kind TEXT, event_key TEXT);
            CREATE TABLE IF NOT EXISTS regions(id TEXT PRIMARY KEY,
                capture_id TEXT NOT NULL, file_identity TEXT NOT NULL,
                start INTEGER NOT NULL, finish INTEGER NOT NULL, digest TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS cursors(file_identity TEXT PRIMARY KEY,
                identity TEXT NOT NULL, finish INTEGER NOT NULL, digest TEXT NOT NULL,
                ok_finish INTEGER NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS votes(root_opaque TEXT NOT NULL,
                entry_id TEXT NOT NULL, revision TEXT NOT NULL, rating TEXT NOT NULL,
                reason TEXT NOT NULL, publishable INTEGER NOT NULL, batch_id TEXT,
                updated REAL NOT NULL,
                PRIMARY KEY(root_opaque, entry_id, revision));
            CREATE TABLE IF NOT EXISTS opaque_roots(root_hash TEXT PRIMARY KEY,
                opaque TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox(batch_id TEXT PRIMARY KEY,
                revision TEXT NOT NULL, batch TEXT NOT NULL, status TEXT NOT NULL,
                detail TEXT NOT NULL, pr_url TEXT, head_sha TEXT,
                created REAL NOT NULL, attempted REAL, updated REAL NOT NULL,
                reconciliations INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL, generation TEXT);
            CREATE TABLE IF NOT EXISTS feed_state(key TEXT PRIMARY KEY,
                value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS feed_staging(feed TEXT NOT NULL,
                git_commit TEXT NOT NULL, path TEXT NOT NULL, entry_id TEXT NOT NULL,
                doc TEXT NOT NULL, PRIMARY KEY(feed, path));
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,
                value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS continuations(capture_id TEXT PRIMARY KEY,
                due REAL NOT NULL, reason TEXT NOT NULL,
                eligible INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS grants(kind TEXT NOT NULL,
                identity TEXT NOT NULL, revision TEXT NOT NULL,
                generation TEXT NOT NULL, created REAL NOT NULL,
                PRIMARY KEY(kind, identity, revision));
            CREATE TABLE IF NOT EXISTS owners(entry_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sent_receipts(
                entry_id TEXT PRIMARY KEY,
                generation TEXT,
                sent_revision TEXT NOT NULL,
                path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                head_sha TEXT NOT NULL,
                repository TEXT,
                pr_url TEXT,
                batch_id TEXT,
                updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS entry_quarantine(
                entry_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                detail TEXT NOT NULL,
                created REAL NOT NULL);
        """)
        capture_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(captures)")
        }
        if capture_columns and "activation_epoch" not in capture_columns:
            self.db.execute("ALTER TABLE captures ADD COLUMN activation_epoch TEXT")
        if capture_columns and "identity_kind" not in capture_columns:
            self.db.execute("ALTER TABLE captures ADD COLUMN identity_kind TEXT")
        if capture_columns and "event_key" not in capture_columns:
            self.db.execute("ALTER TABLE captures ADD COLUMN event_key TEXT")
        continuation_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(continuations)")
        }
        if continuation_columns and "eligible" not in continuation_columns:
            self.db.execute(
                "ALTER TABLE continuations ADD COLUMN eligible INTEGER NOT NULL DEFAULT 1"
            )
        receipt_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(sent_receipts)")
        }
        if receipt_columns and "markers" not in receipt_columns:
            self.db.execute("ALTER TABLE sent_receipts ADD COLUMN markers TEXT")
        if receipt_columns and "batch_revision" not in receipt_columns:
            self.db.execute("ALTER TABLE sent_receipts ADD COLUMN batch_revision TEXT")
        staging_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(feed_staging)")
        }
        if staging_columns and "tokens" not in staging_columns:
            self.db.execute("ALTER TABLE feed_staging ADD COLUMN tokens TEXT")
            self.db.execute("ALTER TABLE feed_staging ADD COLUMN text_digest TEXT")
        entry_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(entries)")
        }
        if entry_columns and "conditions" not in entry_columns:
            # Small authoritative metadata for pre-LIMIT filtering; the
            # search backfill populates it for pre-existing rows.
            self.db.execute(
                "ALTER TABLE entries ADD COLUMN conditions TEXT NOT NULL DEFAULT '{}'"
            )
        self._init_search_index()
        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES('schema', ?)", (SCHEMA,)
        )
        self.db.execute(
            "INSERT OR IGNORE INTO meta VALUES('capture_floor', ?)",
            (str(time.time()),),
        )
        self.capture_floor = float(self.db.execute(
            "SELECT value FROM meta WHERE key='capture_floor'"
        ).fetchone()[0])
        self.db.commit()

    @contextlib.contextmanager
    def _write_txn(self):
        """Serialize read-modify-write across processes sharing this root."""
        with self.lock:
            if self.db.in_transaction:
                yield
                return
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.rollback()
                raise
            self.db.commit()

    def close(self):
        self.db.close()

    # ------------------------------------------------- derived search index
    #
    # The store keeps a REBUILDABLE contentless FTS5 index of derived token
    # streams; `entries` remains the only authority. The index stores only
    # the inverted term index (content='', contentless_delete=1) — no body
    # and no token-stream text copy — plus the entry_id ↔ rowid map with a
    # text fingerprint, so a state-only change never retokenizes. Tokenizer
    # semantics are exactly `retrieval.tokens()` (qualified identifiers,
    # aliases, CJK bigrams); FTS tokenchars keep `./+:-_` inside tokens.
    # A query is one SQL join: index MATCH → entry map → current visibility +
    # conditions filters → rank → LIMIT; only hit headers are read.
    #
    # Tokenization always happens off the write transaction/store lock:
    # create/restore compute from the known document before the write txn;
    # append computes from an off-lock base and re-verifies the base revision
    # at apply (a raced draft recomputes, bounded); feed staging precomputes
    # per blob and the atomic switch applies them; compaction/withdrawal
    # remove the row in the same transaction. An old store (or a
    # version/corruption reset) backfills in bounded resumable slices driven
    # by the existing outbox worker, with apply-time verification that the
    # staged tokens still match the entry's current visible revision — a
    # raced entry is requeued, never overwritten with a stale snapshot and
    # never skipped at completion.

    SEARCH_INDEX_VERSION = "fts5-t2"
    SEARCH_BACKFILL_SLICE = 128

    _VISIBLE_SQL = (
        "feed_active=1 OR (draft_revision IS NOT NULL AND published_revision IS NULL)"
    )

    def _init_search_index(self):
        version_row = self.db.execute(
            "SELECT value FROM meta WHERE key='search_index_version'"
        ).fetchone()
        current = version_row[0] if version_row else None
        if current is not None and current != self.SEARCH_INDEX_VERSION:
            # Index-semantics change: drop only derived tables and rebuild.
            self.db.execute("DROP TABLE IF EXISTS search_index")
            self.db.execute("DROP TABLE IF EXISTS search_map")
        try:
            self.db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS search_index "
                "USING fts5(tokens, content='', contentless_delete=1, "
                "tokenize=\"unicode61 tokenchars './+:-_'\")"
            )
        except sqlite3.OperationalError:
            raise ValueError(
                "knowledge search requires SQLite FTS5 support; this SQLite "
                "build does not provide it"
            ) from None
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS search_map("
            "entry_id TEXT PRIMARY KEY, docid INTEGER NOT NULL UNIQUE, "
            "text_digest TEXT NOT NULL)"
        )
        if current != self.SEARCH_INDEX_VERSION:
            self.db.execute(
                "INSERT OR REPLACE INTO meta VALUES('search_index_version', ?)",
                (self.SEARCH_INDEX_VERSION,),
            )
            self.db.execute("DELETE FROM meta WHERE key='search_backfill'")
            self.db.execute("DELETE FROM meta WHERE key='search_requeue'")

    def _search_backfill_state(self):
        row = self.db.execute(
            "SELECT value FROM meta WHERE key='search_backfill'"
        ).fetchone()
        if row is None:
            return {"next_rowid": 0, "complete": False}
        try:
            value = json.loads(row[0])
        except ValueError:
            return {"next_rowid": 0, "complete": False}
        return value if isinstance(value, dict) else {"next_rowid": 0, "complete": False}

    def _search_requeue_read(self):
        row = self.db.execute(
            "SELECT value FROM meta WHERE key='search_requeue'"
        ).fetchone()
        if row is None:
            return []
        try:
            value = json.loads(row[0])
        except ValueError:
            return []
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str)][:256]

    @staticmethod
    def _doc_source_text(doc):
        return doc["title"] + "\n" + doc["summary"] + "\n" + doc["content"]

    def _index_upsert_doc(self, entry_id, doc):
        """Tokenize and index one entry's visible document. Only used where
        the caller is already on a short local path (fallback when no staged
        token stream exists); the feed production path precomputes per blob.
        """
        from mindie_knowledge.retrieval import index_text

        text = self._doc_source_text(doc)
        self._index_upsert_tokens(
            entry_id, index_text(text),
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )

    def _index_upsert_tokens(self, entry_id, tokens_text, text_digest):
        """Write one entry's precomputed token row (caller holds the txn;
        consistency with the current revision is the caller's verified
        precondition)."""
        row = self.db.execute(
            "SELECT docid, text_digest FROM search_map WHERE entry_id=?",
            (entry_id,),
        ).fetchone()
        if row is not None and row["text_digest"] == text_digest:
            return  # state-only change: never retokenize
        if row is not None:
            self.db.execute(
                "UPDATE search_index SET tokens=? WHERE rowid=?",
                (tokens_text, row["docid"]),
            )
            self.db.execute(
                "UPDATE search_map SET text_digest=? WHERE entry_id=?",
                (text_digest, entry_id),
            )
        else:
            cursor = self.db.execute(
                "INSERT INTO search_index(rowid, tokens) VALUES(NULL, ?)",
                (tokens_text,),
            )
            self.db.execute(
                "INSERT INTO search_map VALUES(?,?,?)",
                (entry_id, cursor.lastrowid, text_digest),
            )

    def _index_remove(self, entry_id):
        row = self.db.execute(
            "SELECT docid FROM search_map WHERE entry_id=?", (entry_id,)
        ).fetchone()
        if row is None:
            return
        self.db.execute("DELETE FROM search_index WHERE rowid=?", (row["docid"],))
        self.db.execute("DELETE FROM search_map WHERE entry_id=?", (entry_id,))

    def _index_sweep_invisible(self):
        """Drop index rows for entries gone or no longer visible (same txn)."""
        stale = self.db.execute(
            "SELECT m.entry_id, m.docid FROM search_map m WHERE m.entry_id "
            "NOT IN (SELECT entry_id FROM entries WHERE " + self._VISIBLE_SQL + ")"
        ).fetchall()
        for entry_id, docid in stale:
            self.db.execute("DELETE FROM search_index WHERE rowid=?", (docid,))
            self.db.execute("DELETE FROM search_map WHERE entry_id=?", (entry_id,))

    def _search_reset(self):
        """Corruption: drop only derived data and restart the backfill."""
        with self._write_txn():
            self.db.execute("DROP TABLE IF EXISTS search_index")
            self.db.execute("DROP TABLE IF EXISTS search_map")
            self._init_search_index()
            self.db.execute("DELETE FROM meta WHERE key='search_backfill'")
            self.db.execute("DELETE FROM meta WHERE key='search_requeue'")

    def search_index_status(self):
        with self.lock:
            state = self._search_backfill_state()
            return dict(
                version=self.SEARCH_INDEX_VERSION,
                complete=bool(state.get("complete")),
                indexed=self.db.execute(
                    "SELECT count(*) FROM search_map"
                ).fetchone()[0],
                visible=self.db.execute(
                    "SELECT count(*) FROM entries WHERE " + self._VISIBLE_SQL
                ).fetchone()[0],
                next_rowid=state.get("next_rowid", 0),
                requeued=len(self._search_requeue_read()),
            )

    def _search_mark_complete_if_covered(self):
        """Persist completeness when the visible set is fully covered.

        The write-path hooks keep coverage current directly, so a store that
        never needed a backfill becomes ready without slicing; a store with
        genuine gaps keeps its persisted cursor untouched."""
        missing = self.db.execute(
            "SELECT count(*) FROM entries WHERE (" + self._VISIBLE_SQL + ") "
            "AND entry_id NOT IN (SELECT entry_id FROM search_map)"
        ).fetchone()[0]
        stale = self.db.execute(
            "SELECT count(*) FROM search_map WHERE entry_id NOT IN "
            "(SELECT entry_id FROM entries WHERE " + self._VISIBLE_SQL + ")"
        ).fetchone()[0]
        if missing or stale or self._search_requeue_read():
            return False
        with self._write_txn():
            top = self.db.execute("SELECT MAX(rowid) FROM entries").fetchone()[0] or 0
            self.db.execute(
                "INSERT OR REPLACE INTO meta VALUES('search_backfill', ?)",
                (canonical({"next_rowid": top, "complete": True}),),
            )
        return True

    def advance_search_index(self, *, max_entries=None, budget_seconds=0.5):
        """Advance the derived search index by one bounded, resumable slice.

        Tokenization runs off the store lock; one short write transaction per
        slice; the persisted cursor resumes after interruption. At apply time
        each staged result must still match the entry's current visible text
        (digest proof); a raced entry is requeued for the next slice instead
        of being overwritten with a stale snapshot or skipped at completion.
        Returns True when the index covers every currently visible entry.
        Purely local read-only maintenance: no capture, no model, no
        publication.
        """
        max_entries = max_entries or self.SEARCH_BACKFILL_SLICE
        deadline = time.monotonic() + budget_seconds
        with self.lock:
            state = self._search_backfill_state()
            requeue = self._search_requeue_read()
            if state.get("complete") and not requeue:
                return True
            cursor = int(state.get("next_rowid") or 0)
            rows = []
            for entry_id in requeue:
                found = self.db.execute(
                    "SELECT rowid, entry_id, doc, feed_active, "
                    "draft_revision, published_revision FROM entries "
                    "WHERE entry_id=?",
                    (entry_id,),
                ).fetchone()
                if found is not None:
                    rows.append(found)
            remaining = max_entries - len(rows)
            slice_rows = []
            if remaining > 0 and not state.get("complete"):
                slice_rows = self.db.execute(
                    "SELECT rowid, entry_id, doc, feed_active, draft_revision, "
                    "published_revision FROM entries "
                    "WHERE rowid > ? ORDER BY rowid LIMIT ?",
                    (cursor, remaining),
                ).fetchall()
                rows.extend(slice_rows)
            existing = (
                {
                    r[0]: r[1]
                    for r in self.db.execute(
                        "SELECT entry_id, text_digest FROM search_map "
                        f"WHERE entry_id IN ({','.join('?' for _ in rows)})",
                        tuple(r["entry_id"] for r in rows),
                    )
                }
                if rows
                else {}
            )
        from mindie_knowledge.retrieval import index_text

        prepared = []
        last_rowid = cursor
        exhausted = False
        for row in rows:
            if time.monotonic() > deadline:
                exhausted = True
                break
            visible = bool(
                row["feed_active"]
                or (row["draft_revision"] and not row["published_revision"])
            )
            if row["rowid"] > cursor:
                last_rowid = row["rowid"]
            if not visible:
                prepared.append((row["entry_id"], None))
                continue
            doc = json.loads(row["doc"])
            text = self._doc_source_text(doc)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if existing.get(row["entry_id"]) == digest:
                continue  # already current: no retokenization, no write
            prepared.append(
                (row["entry_id"],
                 (index_text(text), digest, canonical(doc["conditions"])))
            )
        still_racing = []
        with self._write_txn():
            for entry_id, payload in prepared:
                if payload is None:
                    self._index_remove(entry_id)
                    continue
                tokens_text, digest, conditions_json = payload
                # Verify against the CURRENT authoritative row before
                # applying an off-lock derived snapshot.
                current = self.db.execute(
                    "SELECT doc, feed_active, draft_revision, "
                    "published_revision FROM entries WHERE entry_id=?",
                    (entry_id,),
                ).fetchone()
                if current is None:
                    self._index_remove(entry_id)
                    continue
                current_visible = bool(
                    current["feed_active"]
                    or (current["draft_revision"]
                        and not current["published_revision"])
                )
                if not current_visible:
                    self._index_remove(entry_id)
                    continue
                current_doc = json.loads(current["doc"])
                current_digest = hashlib.sha256(
                    self._doc_source_text(current_doc).encode("utf-8")
                ).hexdigest()
                if current_digest != digest:
                    # The entry moved while we tokenized: discard the stale
                    # derived result; the next slice re-reads the current
                    # version. Never overwrite with it, never skip it.
                    still_racing.append(entry_id)
                    continue
                self.db.execute(
                    "UPDATE entries SET conditions=? WHERE entry_id=?",
                    (conditions_json, entry_id),
                )
                self._index_upsert_tokens(entry_id, tokens_text, digest)
            self.db.execute("DELETE FROM meta WHERE key='search_requeue'")
            if still_racing:
                self.db.execute(
                    "INSERT INTO meta VALUES('search_requeue', ?)",
                    (canonical(still_racing),),
                )
            # The cursor slice completed when it read past the last row; a
            # complete index also requires no entry still racing.
            slice_finished = (not exhausted) and len(slice_rows) < (
                max_entries - len(requeue)
            )
            complete = slice_finished and not still_racing
            if complete:
                self._index_sweep_invisible()
            self.db.execute(
                "INSERT OR REPLACE INTO meta VALUES('search_backfill', ?)",
                (canonical({"next_rowid": last_rowid, "complete": complete}),),
            )
        return complete


    # ------------------------------------------------- material authorization

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
        "this is a retained historical copy, not current published material"
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
        row = self.db.execute(
            "SELECT doc FROM revisions WHERE entry_id=? AND revision=?",
            (entry_id, revision),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def _withdrawn(self, row):
        """Published once, no longer carried by the feed tree after a
        successful sync. Local drafts never resurrect it."""
        return bool(
            row is not None and row["published_revision"] and not row["feed_active"]
        )

    def get(self, ref):
        """Exact document for a reference; a pinned revision reads history.
        A withdrawn entry stays readable with an explicit flag and note."""
        with self.lock:
            entry_id, revision = self._parse_ref(ref)
            row = self._row(entry_id)
            if revision:
                doc = self._revision_doc(entry_id, revision)
                if doc is None:
                    raise ValueError("unknown pinned revision in this domain")
            else:
                if row is None:
                    raise ValueError("unknown reference in this domain")
                doc = json.loads(row["doc"])
            if self._withdrawn(row):
                return dict(doc, withdrawn=True, note=self.WITHDRAWN_NOTE)
            return dict(doc, withdrawn=False)

    # One-call response protection for the native wire (the MCP wrappers
    # duplicate the body as content.text plus structuredContent, so 32 Ki
    # non-BMP characters already cost 256 KiB before JSON overhead): a long
    # body is paginated with the explicit continuation shape
    # (content_offset/content_length/next_offset) instead of failing at
    # transport serialization or being silently truncated. This is a
    # per-call budget, not a document cap. Short bodies return whole.
    EXPLAIN_PAGE_CHARS = 8 * 1024
    EXPLAIN_MAX_LIMIT = 32 * 1024

    def explain(self, ref, *, offset=0, limit=None):
        doc = self.get(ref)
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        if limit is not None and (
            type(limit) is not int or not 1 <= limit <= self.EXPLAIN_MAX_LIMIT
        ):
            raise ValueError(
                f"limit must be between 1 and {self.EXPLAIN_MAX_LIMIT} characters"
            )
        body = doc["content"]
        total = len(body)
        if limit is None and total - offset > self.EXPLAIN_PAGE_CHARS:
            limit = self.EXPLAIN_PAGE_CHARS
        sliced = body[offset : offset + limit if limit is not None else None]
        next_offset = offset + len(sliced)
        return dict(
            doc, content=sliced, content_offset=offset, content_length=total,
            next_offset=next_offset if next_offset < total else None,
            ref=self.ref(doc["entry_id"], doc["revision"]),
        )

    # ---------------------------------------------------------------- drafts

    def _write_draft_file(self, doc):
        from .documents import render_entry

        _atomic_write_text(
            self.root / "drafts" / f"{doc['entry_id']}.md", render_entry(doc)
        )

    def _insert_revision(self, doc, source, created):
        self.db.execute(
            "INSERT OR REPLACE INTO revisions VALUES(?,?,?,?,?)",
            (doc["entry_id"], doc["revision"], canonical(doc), source, created),
        )

    def _owner_of(self, entry_id):
        row = self.db.execute(
            "SELECT owner FROM owners WHERE entry_id=?", (entry_id,)
        ).fetchone()
        return row[0] if row else None

    def create_draft(self, *, kind, title, summary, content, conditions=None,
                     owner=None, entry_id=None, origin="draft",
                     generation=None):
        """Create one local draft with a fresh opaque stable identity.

        ``owner`` is the producing task's opaque identity, recorded in the
        private entry-owner relation; it is never rendered into the document
        or folded into the revision. Without ``generation`` the draft is
        local-only: it is never selected for a contribution batch just because
        a flush happens."""
        if origin not in {"draft"}:
            raise ValueError("local entries start as drafts")
        entry_id = entry_id or new_identity()
        if not documents.HEX_RE.fullmatch(entry_id):
            raise ValueError("entry_id must be a 64-character hex identity")
        if owner is not None and not documents.HEX_RE.fullmatch(owner):
            raise ValueError("owner must be an opaque 64-character hex identity")
        doc = documents.make_entry(
            entry_id=entry_id, domain=self.domain, kind=kind, title=title,
            summary=summary, content=content, conditions=conditions,
        )
        # Derived tokenization happens off the write transaction; the applied
        # row is exactly this content, so no verify is needed for a creation.
        from mindie_knowledge.retrieval import index_text

        source_text = self._doc_source_text(doc)
        tokens_text = index_text(source_text)
        text_digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        now = time.time()
        with self._write_txn():
            if self._row(entry_id) is not None:
                raise ValueError("entry identity already exists")
            self._insert_revision(doc, origin, now)
            self.db.execute(
                "INSERT INTO entries(entry_id, kind, title, origin, "
                "draft_revision, published_revision, feed_active, "
                "batched_revision, doc, updated, conditions) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (entry_id, kind, doc["title"], origin, doc["revision"],
                 None, 0, None, canonical(doc), now,
                 canonical(doc["conditions"])),
            )
            if owner is not None:
                self.db.execute(
                    "INSERT INTO owners VALUES(?,?,?)", (entry_id, owner, now)
                )
            if generation is not None:
                self.db.execute(
                    "INSERT OR REPLACE INTO grants VALUES(?,?,?,?,?)",
                    ("draft", entry_id, doc["revision"], generation, now),
                )
            self._write_draft_file(doc)
            self._index_upsert_tokens(entry_id, tokens_text, text_digest)
        return doc

    def append_observation(self, entry_id, addition, *, marker, producer=None,
                           generation=None, header=None):
        """Append one bounded observation/correction to a local draft.

        Idempotent on ``marker`` (the reserved increment identity): a repeated
        Stop never creates a near-duplicate section. Only the owning task in
        the private entry-owner relation may extend a draft; ownership claimed
        by imported content is never trusted. Published bodies are never
        rewritten locally. With ``generation``, the base draft must already be
        granted to that same sharing generation — an update id can never
        smuggle an old-generation private body into a new publication.
        Without it the new revision is local-only. Returns
        ``(doc, appended)``.

        The derived tokenization is computed off the write lock from an
        off-lock base snapshot; the apply transaction re-verifies the base
        revision and recomputes (bounded) if the draft raced ahead, so no
        long body is ever tokenized under the write transaction and no stale
        derived row can overwrite a newer draft.
        """
        from mindie_knowledge.retrieval import index_text

        for _attempt in range(3):
            with self.lock:
                row = self._row(entry_id)
                if row is None:
                    raise ValueError("unknown draft in this domain")
                if not row["draft_revision"]:
                    raise ValueError("entry has no local draft to update")
                base = self._revision_doc(entry_id, row["draft_revision"])
                owner = self._owner_of(entry_id)
            if producer is not None and producer != owner:
                raise ValueError("only the owning task may append its draft")
            # Off the write lock: the deterministic document and its derived
            # token stream.
            doc, appended = documents.append_observation(base, addition, marker=marker)
            if not appended:
                return doc, False
            if header:
                # The body remains an append-only evidence trail. Retrieval
                # must describe the latest conclusion, including its
                # correction. The title is stable by default: null preserves
                # it, only an explicitly supplied correction renames.
                if header.get("title") is not None:
                    doc["title"] = str(header["title"]).strip()
                if "summary" in header:
                    doc["summary"] = header["summary"]
                if "conditions" in header and header["conditions"] is not None:
                    doc["conditions"] = dict(header["conditions"])
                doc["revision"] = documents.revision_of(doc)
                documents.validate(doc)
            source_text = self._doc_source_text(doc)
            tokens_text = index_text(source_text)
            text_digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            now = time.time()
            with self._write_txn():
                current = self._row(entry_id)
                if current is None or current["draft_revision"] != base["revision"]:
                    continue  # the draft raced ahead; recompute from its base
                if generation is not None and not self.granted(
                    "draft", entry_id, base["revision"], generation
                ):
                    raise ValueError(
                        "draft material belongs to another sharing generation; "
                        "it stays local instead of being republished"
                    )
                self._insert_revision(doc, current["origin"], now)
                if generation is not None:
                    self.db.execute(
                        "INSERT OR REPLACE INTO grants VALUES(?,?,?,?,?)",
                        ("draft", entry_id, doc["revision"], generation, now),
                    )
                visible = not current["feed_active"]
                self.db.execute(
                    "UPDATE entries SET draft_revision=?, title=?, doc=?, "
                    "updated=?, conditions=? WHERE entry_id=?",
                    (doc["revision"], doc["title"],
                     canonical(doc) if visible else current["doc"], now,
                     canonical(doc["conditions"]) if visible
                     else current["conditions"], entry_id),
                )
                self._write_draft_file(doc)
                if visible:
                    # Only a genuinely visible document enters the index; for
                    # a feed-active entry the published body keeps the row.
                    self._index_upsert_tokens(entry_id, tokens_text, text_digest)
                return doc, True
        raise ValueError(
            "draft kept changing across bounded retries; append not applied"
        )

    _NOT_WITHDRAWN = (
        "NOT (entries.published_revision IS NOT NULL AND entries.feed_active=0)"
    )
    # Quarantined entries (final-scan content rejection or a proven
    # closed-unmerged PR) stay local and readable but never re-enter an
    # automatic batch; unrelated material in the same domain is unaffected.
    _NOT_QUARANTINED = (
        "NOT EXISTS (SELECT 1 FROM entry_quarantine "
        "WHERE entry_quarantine.entry_id=entries.entry_id)"
    )

    def drafts_changed(self, *, generation=None):
        """Draft revision bodies not yet included in any outbox batch.

        Read from the revisions table, never the visible ``entries.doc``: for a
        published entry the visible doc is the published body, while the
        outbound candidate is the newer local draft correction. With
        ``generation`` (the outbound path), only material explicitly granted
        to that sharing generation is selected — anything else stays local
        and inert, never backfilled. Without it, this is the local
        bookkeeping view across generations. A draft whose entry the feed no
        longer carries is never a new publication; a quarantined entry stays
        out of automatic batches."""
        with self.lock:
            if generation is not None:
                rows = self.db.execute(
                    "SELECT entries.entry_id, entries.draft_revision FROM entries "
                    "JOIN grants ON grants.kind='draft' "
                    "AND grants.identity=entries.entry_id "
                    "AND grants.revision=entries.draft_revision "
                    "AND grants.generation=? "
                    "WHERE entries.draft_revision IS NOT NULL "
                    "AND entries.draft_revision != COALESCE(entries.batched_revision, '') "
                    f"AND {self._NOT_WITHDRAWN} AND {self._NOT_QUARANTINED}",
                    (generation,),
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT entry_id, draft_revision FROM entries "
                    "WHERE draft_revision IS NOT NULL "
                    "AND draft_revision != COALESCE(batched_revision, '') "
                    "AND NOT EXISTS (SELECT 1 FROM entry_quarantine "
                    "WHERE entry_quarantine.entry_id=entries.entry_id) "
                    f"AND {self._NOT_WITHDRAWN.replace('entries.', '')}"
                ).fetchall()
            return [self._revision_doc(r["entry_id"], r["draft_revision"])
                    for r in rows]

    def draft_headers(self, *, owner=None, limit=6, excerpt=1200,
                      generation=None, query=""):
        """Compact headers plus short excerpts as organizer context. With
        ``generation``, only material granted to that sharing generation is
        offered as update context; with ``owner``, only drafts that task owns
        in the private entry-owner relation. A compacted sent entry (no local
        draft body left) is offered as a bare receipt header — retained
        title/summary plus the exact sent revision, empty excerpt — never a
        body, never a draft or publication candidate; the caller restores the
        exact remote head on demand."""
        with self.lock:
            sql = (
                "SELECT entries.entry_id, entries.draft_revision, entries.doc, "
                "sent_receipts.sent_revision "
                "FROM entries LEFT JOIN sent_receipts "
                "ON sent_receipts.entry_id=entries.entry_id "
                f"WHERE {self._NOT_WITHDRAWN} "
            )
            params = []
            if owner is not None:
                sql += (
                    "AND EXISTS (SELECT 1 FROM owners "
                    "WHERE owners.entry_id=entries.entry_id AND owners.owner=?) "
                )
                params.append(owner)
            live = "entries.draft_revision IS NOT NULL"
            compacted = (
                "entries.draft_revision IS NULL "
                "AND sent_receipts.sent_revision != '' "
                "AND sent_receipts.path != '' "
                "AND sent_receipts.sha256 != '' "
                "AND sent_receipts.head_sha != ''"
            )
            if generation is not None:
                live += (
                    " AND EXISTS (SELECT 1 FROM grants "
                    "WHERE grants.kind='draft' "
                    "AND grants.identity=entries.entry_id "
                    "AND grants.revision=entries.draft_revision "
                    "AND grants.generation=?)"
                )
                compacted += " AND sent_receipts.generation=?"
                params.extend([generation, generation])
            sql += f"AND (({live}) OR ({compacted})) "
            sql += "ORDER BY entries.updated DESC LIMIT 256"
            rows = self.db.execute(sql, params).fetchall()
        headers = []
        for row in rows:
            if row["draft_revision"] is not None:
                doc = self._revision_doc(row["entry_id"], row["draft_revision"]) or {}
                headers.append(dict(
                    entry_id=doc["entry_id"], title=doc["title"],
                    revision=doc["revision"],
                    summary=doc["summary"],
                    excerpt=(doc["content"] if len(doc["content"]) <= excerpt else
                             doc["content"][:excerpt//3] + "\n[earlier body omitted]\n" +
                             doc["content"][-2*excerpt//3:]),
                ))
            else:
                header = json.loads(row["doc"])
                headers.append(dict(
                    entry_id=row["entry_id"], title=header["title"],
                    revision=row["sent_revision"],
                    summary=header["summary"],
                    excerpt="",
                ))
        if query:
            import re
            terms = set(re.findall(r"[\w.-]{3,}", query.lower()))
            headers.sort(key=lambda h: sum(term in (h["title"]+" "+h["summary"]+" "+h["excerpt"]).lower()
                                           for term in terms), reverse=True)
        return headers[:limit]

    # ---------------------------------------------------------------- search

    def query(self, query, limit=5, conditions=None):
        """BM25 over visible entries via the derived contentless FTS5 index:
        published versions win; withdrawn-from-feed entries stay explainable
        but leave the results. Draft overlays on published entries are
        labeled, never a second hit. One SQL join does index MATCH → entry
        map → current visibility/conditions filtering → rank → LIMIT; only
        hit headers are read, and no query ever walks, re-sorts or
        retokenizes the library. While the derived index is being built (old
        store, version reset, corruption), the call honestly rejects with
        :class:`IndexNotReady` — a brief transient read-rejection, never a
        fake/partial result set; the outbox worker completes the build.
        """
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("query must be nonempty text of at most 2000 characters")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        if conditions is not None and not isinstance(conditions, dict):
            raise ValueError("conditions must be an object")
        from mindie_knowledge.retrieval import tokens

        terms = list(dict.fromkeys(tokens(query)))
        if not terms:
            return dict(
                domain=self.domain, retrieval="bm25", results=[],
                note="Reference material. Scores indicate retrieval usefulness, not factual confidence.",
            )
        match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        condition_sql = ""
        condition_args = []
        if conditions:
            for key, value in conditions.items():
                # A knowledge entry carrying a conflicting value is excluded;
                # entries without the key (or non-knowledge kinds) pass. The
                # JSON path is a bound parameter, never interpolated SQL.
                path = '$."' + str(key).replace('"', '""') + '"'
                condition_sql += (
                    " AND NOT (e.kind='knowledge' "
                    "AND json_extract(e.conditions, ?) IS NOT NULL "
                    "AND json_extract(e.conditions, ?) != ?)"
                )
                condition_args.extend([path, path, str(value)])
        sql = (
            "SELECT e.entry_id, e.kind, e.origin, e.feed_active, "
            "e.draft_revision, e.published_revision, e.conditions, e.doc, "
            "bm25(search_index) AS rank FROM search_index "
            "JOIN search_map m ON m.docid = search_index.rowid "
            "JOIN entries e ON e.entry_id = m.entry_id "
            "WHERE search_index MATCH ? AND (" + self._VISIBLE_SQL + ")"
            + condition_sql + " ORDER BY rank LIMIT ?"
        )
        with self.lock:
            if not self._search_backfill_state().get("complete"):
                # Hooks may have kept full coverage without any backfill
                # slice; check coverage once per readiness episode. There is
                # deliberately no inline slice work here: a query never
                # builds or tokenizes content.
                self._search_mark_complete_if_covered()
            if not self._search_backfill_state().get("complete"):
                status = self.search_index_status()
                raise IndexNotReady(
                    "knowledge search index is being rebuilt "
                    f"({status['indexed']}/{status['visible']} entries); "
                    "this is a brief readiness state, not an empty result"
                )
            try:
                rows = self.db.execute(
                    sql, (match, *condition_args, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                # Derived-index corruption: drop only derived data and let
                # the background worker rebuild; authoritative tables
                # untouched.
                self._search_reset()
                raise IndexNotReady(
                    "knowledge search index was reset after a read error and "
                    "is being rebuilt; retry shortly"
                ) from None
            output = []
            for row in rows:
                doc = json.loads(row["doc"])
                supplemental = bool(
                    row["feed_active"]
                    and row["draft_revision"]
                    and row["draft_revision"] != row["published_revision"]
                )
                output.append(dict(
                    ref=self._short_ref(doc["entry_id"], doc["revision"]),
                    kind=doc["kind"], title=doc["title"],
                    summary=doc["summary"], conditions=doc["conditions"],
                    origin=row["origin"], supplemental=supplemental,
                    score=round(-row["rank"], 8),
                ))
            output.sort(key=lambda item: (-item["score"], item["ref"]))
            return dict(
                domain=self.domain,
                retrieval="bm25",
                results=output[:limit],
                note="Reference material. Scores indicate retrieval usefulness, not factual confidence.",
            )

    # ------------------------------------------------------------ feed switch

    def feed_get(self, key):
        with self.lock:
            row = self.db.execute(
                "SELECT value FROM feed_state WHERE key=?", (key,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def feed_set(self, key, value):
        with self._write_txn():
            self.db.execute(
                "INSERT OR REPLACE INTO feed_state VALUES(?,?)",
                (key, canonical(value)),
            )

    # ------------------------------------------------- feed staging (per commit)

    def feed_staging_prune(self, feed, commit):
        """Drop staged verification progress for any other commit of this feed."""
        with self._write_txn():
            self.db.execute(
                "DELETE FROM feed_staging WHERE feed=? AND git_commit != ?",
                (feed, commit),
            )

    def feed_stage_doc(self, feed, commit, path, entry_id, doc,
                       tokens=None, text_digest=None):
        """Persist one validated blob as verified progress for this commit,
        with its derived retrieval token stream precomputed off the switch.

        Each row lands in its own short transaction — no long SQLite write
        transaction is ever held across Git subprocess reads, and a crashed
        attempt resumes after the last staged blob instead of item zero.
        """
        with self._write_txn():
            self.db.execute(
                "INSERT OR REPLACE INTO feed_staging VALUES(?,?,?,?,?,?,?)",
                (feed, commit, path, entry_id, canonical(doc), tokens,
                 text_digest),
            )

    def feed_staging_state(self, feed, commit):
        """(paths, entry_ids) already verified for exactly this commit."""
        with self.lock:
            rows = self.db.execute(
                "SELECT path, entry_id FROM feed_staging WHERE feed=? AND git_commit=?",
                (feed, commit),
            ).fetchall()
        return {row[0] for row in rows}, {row[1] for row in rows}

    def feed_staging_count(self, feed, commit):
        with self.lock:
            return self.db.execute(
                "SELECT count(*) FROM feed_staging WHERE feed=? AND git_commit=?",
                (feed, commit),
            ).fetchone()[0]

    def feed_staged_docs(self, feed, commit):
        """Lazily yield staged (doc, tokens, text_digest) tuples in path
        order (one at a time); rows written before the tokens column existed
        yield None tokens and the switch recomputes them."""
        cursor = self.db.execute(
            "SELECT doc, tokens, text_digest FROM feed_staging "
            "WHERE feed=? AND git_commit=? ORDER BY path",
            (feed, commit),
        )
        for doc_json, tokens, text_digest in cursor:
            yield json.loads(doc_json), tokens, text_digest

    def feed_staging_clear(self, feed):
        with self._write_txn():
            self.db.execute("DELETE FROM feed_staging WHERE feed=?", (feed,))

    def install_feed(self, docs, *, feed_ident):
        """Atomically switch published membership to one validated Git tree.

        ``docs`` may be a lazy iterable (the feed streams validated blobs one
        at a time instead of holding every body in memory): it is consumed
        inside this single write transaction, so a mid-iteration parse or IO
        failure rolls the whole candidate back and keeps the old cache.
        Every revision body is retained; entries the tree no longer carries
        leave ordinary search but stay readable by reference. A published
        revision of the same entry folds over its draft in search; the local
        draft is then re-seated onto the new authoritative body, keeping only
        not-yet-sent observation additions (see ``rebase_draft_on_published``)
        — the feed switch never destroys unsubmitted local work.
        """
        now = time.time()
        seen = set()
        with self._write_txn():
            # Membership is authoritative for every entry, however it first
            # appeared locally. Clear then re-mark inside the same
            # transaction: no giant NOT IN variable list (SQLite has a
            # variable-count ceiling), and a mid-iteration failure rolls the
            # whole candidate back so the old cache stays.
            self.db.execute("UPDATE entries SET feed_active=0 WHERE feed_active=1")
            for item in docs:
                if isinstance(item, tuple):
                    # The feed staged the derived token stream per blob
                    # (off the switch); the atomic switch only applies it.
                    doc, tokens_text, text_digest = item
                else:
                    doc, tokens_text, text_digest = item, None, None
                if doc["entry_id"] in seen:
                    raise ValueError("duplicate entry identity in feed")
                seen.add(doc["entry_id"])
                row = self._row(doc["entry_id"])
                existing = (
                    self._revision_doc(doc["entry_id"], doc["revision"])
                    if row is not None else None
                )
                if existing is None:
                    self._insert_revision(doc, "feed", now)
                if row is None:
                    self.db.execute(
                        "INSERT INTO entries(entry_id, kind, title, origin, "
                        "draft_revision, published_revision, feed_active, "
                        "batched_revision, doc, updated, conditions) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (doc["entry_id"], doc["kind"], doc["title"],
                         "feed", None, doc["revision"], 1, None,
                         canonical(doc), now, canonical(doc["conditions"])),
                    )
                else:
                    self.db.execute(
                        "UPDATE entries SET kind=?, title=?, "
                        "published_revision=?, feed_active=1, doc=?, updated=?, "
                        "conditions=? WHERE entry_id=?",
                        (doc["kind"], doc["title"], doc["revision"],
                         canonical(doc), now, canonical(doc["conditions"]),
                         doc["entry_id"]),
                    )
                # The visible document is the published body; index exactly
                # it, with the feed-staged token stream when present.
                if tokens_text is None:
                    self._index_upsert_doc(doc["entry_id"], doc)
                else:
                    self._index_upsert_tokens(doc["entry_id"], tokens_text,
                                              text_digest)
                self.rebase_draft_on_published(doc["entry_id"])
            # Once an entry has a published revision its visibility is
            # governed by the feed alone: leaving the tree (or a valid empty
            # tree) removes it from search, and its stale draft copy is never
            # resurrected. All bodies stay readable by pinned reference. The
            # derived index rows leave in the same transaction.
            self._index_sweep_invisible()
            return dict(entries=len(seen))

    # ------------------------------------------------------- entry quarantine

    def quarantine_entry(self, entry_id, *, kind, detail):
        """Keep one entry's material out of every future automatic batch.

        Quarantine is per-entry and automatic: a final-scan content rejection
        or a proven closed-unmerged PR retires exactly that content (the local
        draft stays readable and searchable) while unrelated material keeps
        flowing. Append-only bodies cannot erase rejected text, so there is no
        automatic un-quarantine; genuinely new content lives under a new
        entry identity, and an explicit operator retry of the stored batch
        remains available for a proven failure.
        """
        if kind not in {"content-scan", "pr-rejected", "platform-envelope"}:
            raise ValueError("invalid quarantine kind")
        with self._write_txn():
            self.db.execute(
                "INSERT OR REPLACE INTO entry_quarantine VALUES(?,?,?,?)",
                (entry_id, kind, str(detail)[:500], time.time()),
            )

    def quarantined_entries(self):
        with self.lock:
            return {
                row[0]: row[1]
                for row in self.db.execute(
                    "SELECT entry_id, kind FROM entry_quarantine"
                )
            }

    # ------------------------------------------------------------------ votes

    def opaque_for(self, root_hash):
        """Stable opaque public identity for one local root; the raw native
        session id and this mapping never leave the store."""
        with self._write_txn():
            row = self.db.execute(
                "SELECT opaque FROM opaque_roots WHERE root_hash=?", (root_hash,)
            ).fetchone()
            if row:
                return row[0]
            opaque = new_identity()
            self.db.execute(
                "INSERT INTO opaque_roots VALUES(?,?,?)",
                (root_hash, opaque, time.time()),
            )
            return opaque

    def record_vote(self, *, root_hash, ref, rating, reason, publishable,
                    generation=None):
        """One current vote per opaque root and entry; a new vote on the same
        entry replaces it (including its revision and reason). A publishable
        vote's free-text reason is privacy-scanned at admission, so a single
        unsafe reason can never block unrelated material at batch time; local
        (non-publishable) votes are never scanned and never leave the store."""
        if rating not in RATINGS:
            raise ValueError("rating must be up or down")
        if not isinstance(reason, str) or len(reason) > MAX_VOTE_REASON:
            raise ValueError("reason must be text of at most 1000 characters")
        if publishable:
            from mindie_knowledge.redact import scan_text

            findings = scan_text(reason)
            if findings:
                rules = ", ".join(sorted({f.rule for f in findings}))
                raise ValueError(
                    f"publishable vote reason fails the privacy scan: {rules}"
                )
        with self._write_txn():
            entry_id, pinned = self._parse_ref(ref)
            row = self._row(entry_id)
            if row is None:
                raise ValueError("unknown reference in this domain")
            revision = pinned or json.loads(row["doc"])["revision"]
            if self._revision_doc(entry_id, revision) is None:
                raise ValueError("unknown pinned revision in this domain")
            opaque = self.opaque_for(root_hash)
            self.db.execute(
                "INSERT OR REPLACE INTO votes VALUES(?,?,?,?,?,?,NULL,?)",
                (opaque, entry_id, revision, rating, reason.strip(),
                 1 if publishable else 0, time.time()),
            )
            if publishable and generation:
                self.db.execute(
                    "INSERT OR REPLACE INTO grants VALUES(?,?,?,?,?)",
                    ("vote", self.vote_identity(opaque, entry_id), revision,
                     generation, time.time()),
                )
            return dict(
                vote_id=digest(["vote", opaque, entry_id, revision]),
                root_id=opaque, entry_id=entry_id, revision=revision,
                rating=rating, publishable=bool(publishable),
            )

    def unbatched_votes(self, *, generation=None):
        """Publishable unbatched votes. With ``generation`` (the outbound
        path), only votes explicitly granted to that sharing generation.
        Votes on an entry the feed no longer carries — or whose material was
        quarantined — stay local."""
        withdrawn = (
            "NOT EXISTS (SELECT 1 FROM entries e WHERE e.entry_id=votes.entry_id "
            "AND e.published_revision IS NOT NULL AND e.feed_active=0)"
        )
        quarantined = (
            "NOT EXISTS (SELECT 1 FROM entry_quarantine q "
            "WHERE q.entry_id=votes.entry_id)"
        )
        with self.lock:
            if generation is not None:
                return [
                    dict(r)
                    for r in self.db.execute(
                        "SELECT votes.* FROM votes JOIN grants ON grants.kind='vote' "
                        "AND grants.identity=votes.root_opaque||':'||votes.entry_id "
                        "AND grants.revision=votes.revision AND grants.generation=? "
                        "WHERE votes.publishable=1 AND votes.batch_id IS NULL "
                        f"AND {withdrawn} AND {quarantined}",
                        (generation,),
                    )
                ]
            return [
                dict(r)
                for r in self.db.execute(
                    "SELECT * FROM votes WHERE publishable=1 AND batch_id IS NULL "
                    f"AND {withdrawn} AND {quarantined}"
                )
            ]

    # ---------------------------------------------------------------- outbox

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
            try:
                record = json.loads(row[0])
            except ValueError:
                record = {"status": "attempted"}
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
                try:
                    record = json.loads(row[0])
                except ValueError:
                    record = {}
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
                     generation=None):
        """Record one built batch as pending and bind its material.

        Material bound to a batch is never silently re-batched: only a newer
        draft revision or a replacement vote becomes new work. The batch row
        is one durable payload, bounded by the same per-flush envelope the
        exporter chunks to; the envelope is a soft grouping budget, so a batch
        holding exactly one platform-legal entry document always fits.
        """
        from mindie_knowledge.community.common import MAX_BATCH_BYTES, MAX_FILE_BYTES

        if len(canonical(batch).encode("utf-8")) > MAX_BATCH_BYTES:
            entry_files = [
                f for f in batch.get("files", [])
                if isinstance(f, dict)
                and str(f.get("path", "")).startswith(("cases/", "topics/"))
            ]
            single_ok = (
                len(entry_files) == 1
                and isinstance(entry_files[0].get("content"), str)
                and len(entry_files[0]["content"].encode("utf-8")) <= MAX_FILE_BYTES
            )
            if not single_ok:
                raise ValueError("batch exceeds the per-flush storage envelope")
        now = time.time()
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
                self.db.execute("DELETE FROM outbox WHERE batch_id=?", (batch_id,))
            self.db.execute(
                "INSERT INTO outbox VALUES(?,?,?,?,?,NULL,NULL,?,NULL,?,0,NULL,?)",
                (batch_id, revision, canonical(batch), "pending", "", now, now,
                 generation),
            )
            for entry_id in entry_ids:
                row = self._row(entry_id)
                if row is not None:
                    self.db.execute(
                        "UPDATE entries SET batched_revision=? WHERE entry_id=?",
                        (row["draft_revision"], entry_id),
                    )
            for opaque, entry_id, revision in vote_keys:
                self.db.execute(
                    "UPDATE votes SET batch_id=? WHERE root_opaque=? "
                    "AND entry_id=? AND revision=?",
                    (batch_id, opaque, entry_id, revision),
                )

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
                    try:
                        batch = json.loads(row["batch"])
                    except ValueError:
                        batch = {}
                    self._record_sent_receipts(row, batch, actual_files=actual_files)
            if status == "rejected":
                # A proven closed-unmerged PR retires exactly this batch's
                # entry material from automatic publication; the lineage row
                # itself stays replaceable so unrelated material flows.
                row = self.db.execute(
                    "SELECT batch FROM outbox WHERE batch_id=?", (batch_id,)
                ).fetchone()
                try:
                    refs = json.loads(row[0]).get("entry_refs", []) if row else []
                except ValueError:
                    refs = []
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

    def outbox_pending(self):
        """Never-attempted batches (resumable after a forced shutdown)."""
        with self.lock:
            return [
                dict(r)
                for r in self.db.execute(
                    "SELECT * FROM outbox WHERE status='pending' AND attempted IS NULL"
                )
            ]

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

    def outbox_unresolved(self):
        """Attempted but outcome-unknown batches needing bounded reconciliation."""
        with self.lock:
            return [
                dict(r)
                for r in self.db.execute(
                    "SELECT * FROM outbox WHERE (status='unknown' "
                    "OR (status='pending' AND attempted IS NOT NULL))"
                )
            ]

    def outbox_unavailable(self):
        """Batches whose last send hit a transient environment failure.

        The stored payload is intact (nothing confirmed, nothing compacted);
        resubmission retries the exact same operation with persisted backoff.
        """
        with self.lock:
            return [
                dict(r)
                for r in self.db.execute(
                    "SELECT * FROM outbox WHERE status='unavailable' "
                    "AND attempted IS NOT NULL ORDER BY created"
                )
            ]

    # ---------------------------------------------- confirmed-PR compaction

    CONFIRMED_BATCH = ("submitted", "updated", "unchanged")

    def _protected_revisions(self, exclude_batch):
        """Revisions still referenced by any unsent or unresolved batch."""
        protected = set()
        for row in self.db.execute(
            "SELECT batch, status FROM outbox WHERE batch_id != ?", (exclude_batch,)
        ):
            if row["status"] in self.CONFIRMED_BATCH:
                continue
            try:
                batch = json.loads(row["batch"])
            except ValueError:
                continue
            for ref in batch.get("entry_refs", []):
                parsed = _exact_revision_ref(ref)
                if parsed is not None:
                    protected.add(parsed)
        return protected

    def compact_confirmed(self, batch_id):
        """Post-confirmation payload cleanup: the GitHub branch is the durable
        body source for a confirmed (submitted/updated/unchanged) batch.

        Removes exactly THIS batch's sent draft history (even when a newer
        unsent draft exists — that current draft and any protected/published
        revisions stay). Capture summaries are cleared only for organized
        captures whose recorded refs are a nonempty subset of this batch's
        exact entry@revision refs; unversioned or otherwise ambiguous coverage
        is left intact. The outbox row shrinks to a tiny receipt; a per-entry
        last-confirmed receipt (path/hash/revision/exact head/PR) is kept
        independently of later lineage-row replacement.
        """
        import shutil

        with self._write_txn():
            row = self.db.execute(
                "SELECT * FROM outbox WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if row is None or row["status"] not in self.CONFIRMED_BATCH:
                return None
            batch = json.loads(row["batch"])
            protected = self._protected_revisions(batch_id)
            removed = dict(entries=0, revisions=0, captures=0, staging=0)
            file_receipts = [
                {key: file[key] for key in ("path", "sha256") if key in file}
                for file in batch.get("files", [])
            ]
            batch_refs = set()
            for ref in batch.get("entry_refs", []):
                parsed = _exact_revision_ref(ref)
                if parsed is None:
                    continue
                entry_id, sent_revision = parsed
                batch_refs.add(parsed)
                entry = self._row(entry_id)
                if entry is None:
                    continue
                current = entry["draft_revision"]
                kept = {entry["published_revision"]}
                kept |= {
                    revision for (ent, revision) in protected if ent == entry_id
                }
                if current and current != sent_revision:
                    kept.add(current)
                if (entry_id, sent_revision) in protected:
                    kept.add(sent_revision)
                kept.discard(None)
                if (entry_id, sent_revision) not in protected:
                    kept.discard(sent_revision)
                if kept:
                    cursor = self.db.execute(
                        "DELETE FROM revisions WHERE entry_id=? AND source='draft' "
                        f"AND revision NOT IN ({','.join('?' for _ in kept)})",
                        (entry_id, *sorted(kept)),
                    )
                else:
                    cursor = self.db.execute(
                        "DELETE FROM revisions WHERE entry_id=? AND source='draft'",
                        (entry_id,),
                    )
                removed["revisions"] += cursor.rowcount
                if (
                    current == sent_revision
                    and (entry_id, sent_revision) not in protected
                ):
                    if entry["published_revision"]:
                        published = self._revision_doc(
                            entry_id, entry["published_revision"]
                        )
                    else:
                        published = None
                    header = published or {
                        **json.loads(entry["doc"]), "content": ""
                    }
                    self.db.execute(
                        "UPDATE entries SET draft_revision=NULL, doc=?, "
                        "updated=?, conditions=? WHERE entry_id=?",
                        (canonical(header), time.time(),
                         canonical(header["conditions"]), entry_id),
                    )
                    removed["entries"] += 1
                    if not entry["feed_active"]:
                        # A compacted, never-published draft leaves search
                        # entirely; the derived row goes with it.
                        self._index_remove(entry_id)
                    try:
                        (self.root / "drafts" / f"{entry_id}.md").unlink()
                    except OSError:
                        pass
            self._record_sent_receipts(row, batch)
            generation = row["generation"]
            if generation and batch_refs:
                removed["captures"] += self._clear_covered_captures(
                    generation, batch_refs
                )
            receipt = {
                "schema": "mindie-contribution-receipt/1",
                "batch_id": batch.get("batch_id", batch_id),
                "revision": batch.get("revision", row["revision"]),
                "domain": batch.get("domain", self.domain),
                "entry_refs": batch.get("entry_refs", []),
                "files": file_receipts,
                "summary": batch.get("summary", ""),
            }
            self.db.execute(
                "UPDATE outbox SET batch=? WHERE batch_id=?",
                (canonical(receipt), batch_id),
            )
        staging = self.root / "outbox" / "staging" / batch_id
        if staging.is_dir():
            shutil.rmtree(staging, ignore_errors=True)
            removed["staging"] = 1
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
        """Tiny per-entry last-confirmed receipt, independent of the newest
        lineage outbox row. No body/history.

        A confirmed receipt is sending history, never publication proof to
        write from: it records the content identity actually committed (when
        the publisher reports post-merge file identities) and the cumulative
        confirmed observation-marker set — enough to prove which additions are
        still unsent — plus path/PR linkage. The actual remote state is
        re-read wherever it is used (publication merge, draft restore)."""
        head = row["head_sha"] if isinstance(row, sqlite3.Row) else row.get("head_sha")
        if not isinstance(head, str) or not head:
            return
        generation = row["generation"] if isinstance(row, sqlite3.Row) else row.get("generation")
        pr_url = row["pr_url"] if isinstance(row, sqlite3.Row) else row.get("pr_url")
        batch_id = row["batch_id"] if isinstance(row, sqlite3.Row) else row.get("batch_id")
        batch_revision = row["revision"] if isinstance(row, sqlite3.Row) else row.get("revision")
        files = {
            f.get("path"): f
            for f in batch.get("files", [])
            if isinstance(f, dict) and isinstance(f.get("path"), str)
        }
        actual = {
            f.get("path"): f
            for f in (actual_files or [])
            if isinstance(f, dict) and isinstance(f.get("path"), str)
        }
        now = time.time()
        for ref in batch.get("entry_refs", []):
            parsed = _exact_revision_ref(ref)
            if parsed is None:
                continue
            entry_id, sent_revision = parsed
            path = next(
                (p for p in (f"cases/{entry_id}.md", f"topics/{entry_id}.md")
                 if p in files or p in actual),
                None,
            )
            if not entry_id or not sent_revision or not path:
                continue
            existing = self.db.execute(
                "SELECT sha256, sent_revision, markers FROM sent_receipts "
                "WHERE entry_id=? AND batch_revision=?",
                (entry_id, batch_revision),
            ).fetchone()
            payload_file = files.get(path) or {}
            actual_file = actual.get(path) or {}
            if actual_file.get("sha256") and actual_file.get("revision"):
                # What the remote actually committed (post-merge content).
                sha = actual_file["sha256"]
                revision = actual_file["revision"]
            elif existing is not None:
                # A later re-record of the same confirmed batch (e.g. payload
                # compaction) must not downgrade actual identities to the
                # pre-merge candidate values.
                sha = existing["sha256"]
                revision = existing["sent_revision"]
            else:
                # Legacy rows only: batches confirmed before actual-file
                # recording existed have no committed identity to recover;
                # the candidate identity is their best available record.
                sha = payload_file.get("sha256")
                revision = sent_revision
            if not isinstance(sha, str) or not sha:
                continue
            if not isinstance(revision, str) or not revision:
                continue
            content = payload_file.get("content")
            payload_markers = (
                documents.observation_markers(content)
                if isinstance(content, str) else None
            )
            prior_row = self.db.execute(
                "SELECT markers FROM sent_receipts WHERE entry_id=?", (entry_id,)
            ).fetchone()
            prior = []
            if prior_row is not None and prior_row[0]:
                try:
                    parsed_markers = json.loads(prior_row[0])
                except ValueError:
                    parsed_markers = []
                if isinstance(parsed_markers, list):
                    prior = [m for m in parsed_markers if isinstance(m, str)]
            if payload_markers is None and prior_row is None:
                markers_value = None
            else:
                markers_value = canonical(sorted({*prior, *(payload_markers or [])}))
            self.db.execute(
                "INSERT OR REPLACE INTO sent_receipts"
                "(entry_id, generation, sent_revision, path, sha256, head_sha,"
                " repository, pr_url, batch_id, updated, markers, batch_revision)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry_id, generation, revision, path, sha, head,
                    None, pr_url, batch_id, now, markers_value, batch_revision,
                ),
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
        """Observation markers present in the last confirmed sent body.

        A set proves which additions are still unsent; None means unknown
        (no receipt, or a receipt written before markers were recorded), in
        which case no delta may be inferred and publication reconciles the
        current remote head instead of guessing."""
        receipt = self.sent_receipt(entry_id)
        if not receipt:
            return None
        raw = receipt.get("markers")
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(value, list) or not all(
            isinstance(marker, str) for marker in value
        ):
            return None
        return set(value)

    def rebase_draft_on_published(self, entry_id):
        """Re-seat a local draft onto the authoritative published body.

        Submitted content obeys the remote: when the feed-installed body has
        moved away from the last confirmed send (bot/maintainer edits), the
        local draft is rebuilt as the published body plus only the
        not-yet-sent observation blocks (markers absent from the confirmed
        receipt and from the published body). Blocks the upstream removed are
        never brought back, and a remote header wins over a local one. When
        nothing remains unsent the redundant local draft is dropped. Returns
        the resulting draft doc, or None when there is nothing to rebase (or
        no draft remains). Never resurrects a withdrawn entry; a legacy
        receipt without marker knowledge cannot prove the delta and is left
        for the publication-time head reconciliation instead of guessing."""
        now = time.time()
        with self._write_txn():
            row = self._row(entry_id)
            if (
                row is None
                or not row["draft_revision"]
                or not row["published_revision"]
                or not row["feed_active"]
            ):
                return None
            sent_markers = self.sent_markers(entry_id)
            if sent_markers is None:
                return None
            receipt_hash = self.sent_file_hash(entry_id)
            published = self._revision_doc(entry_id, row["published_revision"])
            draft = self._revision_doc(entry_id, row["draft_revision"])
            if published is None or draft is None:
                return None
            published_sha = hashlib.sha256(
                documents.render_entry(published).encode("utf-8")
            ).hexdigest()
            if published_sha == receipt_hash:
                return None  # remote did not move; the draft base stands
            split = documents.split_observations(draft["content"])
            if split is None:
                return None  # opaque tail: publication reconciles, never guess
            _base, blocks = split
            published_markers = set(documents.observation_markers(published["content"]))
            unsent = [
                (marker, addition)
                for marker, addition in blocks
                if marker not in sent_markers and marker not in published_markers
            ]
            if not unsent and not blocks:
                # The draft predates any observation structure; if it differs
                # from the published body the divergence is not in transferable
                # observation form, so it is left for publication-time
                # reconciliation rather than silently dropped.
                if draft["content"] != published["content"]:
                    return None
            rebuilt = published
            for marker, addition in unsent:
                rebuilt, _appended = documents.append_observation(
                    rebuilt, addition, marker=marker
                )
            if rebuilt["revision"] == draft["revision"]:
                return None  # already seated on the published body
            if rebuilt["revision"] == published["revision"]:
                # Nothing unsent: the local copy converges to the remote body.
                self.db.execute(
                    "UPDATE entries SET draft_revision=NULL, title=?, updated=? "
                    "WHERE entry_id=?",
                    (published["title"], now, entry_id),
                )
                try:
                    (self.root / "drafts" / f"{entry_id}.md").unlink()
                except OSError:
                    pass
                return None
            self._insert_revision(rebuilt, row["origin"], now)
            self.db.execute(
                "INSERT OR REPLACE INTO grants "
                "SELECT 'draft', identity, ?, generation, ? FROM grants "
                "WHERE kind='draft' AND identity=? AND revision=?",
                (rebuilt["revision"], now, entry_id, row["draft_revision"]),
            )
            self.db.execute(
                "UPDATE entries SET draft_revision=?, title=?, updated=? "
                "WHERE entry_id=?",
                (rebuilt["revision"], published["title"], now, entry_id),
            )
            self._write_draft_file(rebuilt)
            return rebuilt

    def restore_draft(self, entry_id, doc, *, generation=None):
        """Re-seed a compacted append-base from the exact confirmed remote
        body (fetched boundedly by the caller). Never overwrites an existing
        local draft, never resurrects a withdrawn entry and never fabricates
        a base from only the new paragraph."""
        documents.validate(doc)
        # Derived tokenization is computed off the write transaction from the
        # caller-provided document; the apply writes exactly that content.
        from mindie_knowledge.retrieval import index_text

        source_text = self._doc_source_text(doc)
        tokens_text = index_text(source_text)
        text_digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        now = time.time()
        with self._write_txn():
            row = self._row(entry_id)
            if row is None:
                raise ValueError("unknown draft in this domain")
            if row["draft_revision"]:
                raise ValueError("entry already has a local draft")
            if self._withdrawn(row):
                raise ValueError("entry was withdrawn upstream; not restored")
            if not isinstance(doc, dict) or doc.get("entry_id") != entry_id:
                raise ValueError("fetched body does not match the entry identity")
            self._insert_revision(doc, "draft", now)
            if generation is not None:
                self.db.execute(
                    "INSERT OR REPLACE INTO grants VALUES(?,?,?,?,?)",
                    ("draft", entry_id, doc["revision"], generation, now),
                )
            visible = not row["feed_active"]
            self.db.execute(
                "UPDATE entries SET draft_revision=?, title=?, doc=?, "
                "updated=?, conditions=? WHERE entry_id=?",
                (doc["revision"], doc["title"],
                 canonical(doc) if visible else row["doc"], now,
                 canonical(doc["conditions"]) if visible else row["conditions"],
                 entry_id),
            )
            self._write_draft_file(doc)
            if visible:
                self._index_upsert_tokens(entry_id, tokens_text, text_digest)
            return doc

    # --------------------------------------------------------------- capture

    def add_capture(self, *, root_session, session, turn, transcript, summary,
                    generation=None, boundary=None, scope=None, namespace="",
                    activation_epoch=None, hold=None, kind="turn", event_key=None):
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
        if hold not in {None, "maintenance-paused"}:
            raise ValueError("invalid capture hold")
        with self._write_txn():
            result = commit_capture(
                self.db, namespace=namespace, root_session=root_session,
                session=session, turn=turn, transcript=transcript,
                summary=summary.strip(), generation=generation, boundary=boundary,
                scope=scope, activation_epoch=activation_epoch, hold=hold,
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

    def mark_capture(self, ident, status, detail=""):
        with self._write_txn():
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
                "INSERT INTO regions VALUES(?,?,?,?,?,?,?,?,?)",
                (ident, capture_id, file_identity, start, finish, region_digest,
                 status, str(detail)[:1000], now),
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

    def arm_apply_continuations(self):
        """Give a saved apply-pending result a due row if it has none.

        This does not apply the result and does not touch the network.
        """
        now = time.time()
        with self._write_txn():
            rows = self.db.execute(
                "SELECT id FROM maintenance_attempts WHERE status='apply' "
                "AND result IS NOT NULL"
            ).fetchall()
            for (attempt_id,) in rows:
                parts = str(attempt_id).split(":")
                if len(parts) < 3 or parts[0] != "organize":
                    continue
                capture_id = parts[1]
                found = self.db.execute(
                    "SELECT 1 FROM continuations WHERE capture_id=?",
                    (capture_id,),
                ).fetchone()
                if found is None:
                    _upsert_continuation(
                        self.db, capture_id, now, "apply-pending", True,
                    )

    def due_application(self):
        """One saved apply whose continuation is eligible and due."""
        with self.lock:
            rows = self.db.execute(
                "SELECT m.id, q.capture_id FROM maintenance_attempts m "
                "JOIN continuations q ON m.id LIKE ('organize:' || q.capture_id || ':%') "
                "JOIN captures c ON c.id = q.capture_id "
                "WHERE m.status='apply' AND m.result IS NOT NULL "
                "AND c.status='apply-pending' "
                "AND COALESCE(q.eligible, 1) != 0 AND COALESCE(q.due, 0) <= ? "
                "ORDER BY q.due, m.started LIMIT 5",
                (time.time(),),
            ).fetchall()
        for attempt_id, capture_id in rows:
            prefix = f"organize:{capture_id}:"
            if attempt_id.startswith(prefix) and len(attempt_id) > len(prefix):
                return attempt_id
        return None

    def finish_region(self, ident, status, detail=""):
        ok = status in {"succeeded", "summary-only"}
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

    # Legacy public migration was cancelled by explicit user steering (the
    # public knowledge base is being cleared and restarts empty in the new
    # format). There is intentionally no legacy import/mapping API; private
    # pre-existing user files simply stay inert on disk.

    # ---------------------------------------------------------------- status

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
                withdrawn=self.db.execute(
                    "SELECT count(*) FROM entries WHERE published_revision "
                    "IS NOT NULL AND feed_active=0"
                ).fetchone()[0],
                captures=[
                    dict(r)
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
