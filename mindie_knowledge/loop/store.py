"""Current task metadata, consent-bound capture and publication queues.

The state-v4 SQLite database stores small headers, identifiers, cursors and
receipts. Current material bodies live in canonical Markdown packages managed
by MaterialStore; ReMe owns the disposable local retrieval index. Writes stage
files first, commit queue metadata, then promote matching current pointers.
Superseded unreferenced bodies are retired, and old database files are inert.
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

SCHEMA = "mindie-store/4"

_STATE_COLUMNS = {
    'meta': 'key value'.split(),
    'entries': 'entry_id kind title origin draft_revision published_revision feed_active batched_revision doc updated conditions'.split(),
    'revisions': 'entry_id revision doc source created'.split(),
    'known_revisions': 'entry_id revision'.split(),
    'captures': 'id root_session session turn transcript summary status detail created generation boundary scope activation_epoch identity_kind event_key'.split(),
    'regions': 'id capture_id file_identity start finish digest status detail created recovery identity'.split(),
    'cursors': 'file_identity identity finish digest ok_finish updated'.split(),
    'votes': 'root_opaque entry_id revision rating reason publishable batch_id updated'.split(),
    'opaque_roots': 'root_hash opaque created'.split(),
    'outbox': 'batch_id revision batch status detail pr_url head_sha created attempted updated reconciliations next_attempt generation'.split(),
    'feed_state': 'key value'.split(),
    'state': 'key value'.split(),
    'continuations': 'capture_id due reason eligible'.split(),
    'grants': 'kind identity revision generation created'.split(),
    'owners': 'entry_id owner created'.split(),
    'sent_receipts': 'entry_id generation sent_revision path sha256 head_sha repository pr_url batch_id updated markers batch_revision'.split(),
    'entry_quarantine': 'entry_id kind detail created'.split(),
    'transcript_tasks': 'task_key entry_id capture_id body_digest summary_status summary_detail updated summary_due authorization'.split(),
    'material_streams': 'stream_key entry_id source_cursor source_identity redaction_state generation authorization updated'.split(),
    'material_batches': 'batch_id stream_key entry_id block_ids status detail authorization created'.split(),
}

def _initialize_state(db):
    db.executescript("""
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
            CREATE TABLE IF NOT EXISTS known_revisions(entry_id TEXT NOT NULL,
                revision TEXT NOT NULL, PRIMARY KEY(entry_id, revision));
            CREATE TABLE IF NOT EXISTS captures(id TEXT PRIMARY KEY,
                root_session TEXT NOT NULL, session TEXT NOT NULL, turn TEXT NOT NULL,
                transcript TEXT, summary TEXT NOT NULL, status TEXT NOT NULL,
                detail TEXT NOT NULL, created REAL NOT NULL,
                generation TEXT, boundary REAL, scope TEXT,
                activation_epoch TEXT, identity_kind TEXT, event_key TEXT);
            CREATE TABLE IF NOT EXISTS regions(id TEXT PRIMARY KEY,
                capture_id TEXT NOT NULL, file_identity TEXT NOT NULL,
                start INTEGER NOT NULL, finish INTEGER NOT NULL, digest TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL,
                recovery INTEGER NOT NULL DEFAULT 0, identity TEXT);
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
                updated REAL NOT NULL, markers TEXT, batch_revision TEXT);
            CREATE TABLE IF NOT EXISTS entry_quarantine(
                entry_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                detail TEXT NOT NULL,
                created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS transcript_tasks(
                task_key TEXT PRIMARY KEY, entry_id TEXT NOT NULL,
                capture_id TEXT NOT NULL, body_digest TEXT NOT NULL,
                summary_status TEXT NOT NULL, summary_detail TEXT NOT NULL,
                updated REAL NOT NULL, summary_due REAL NOT NULL, authorization TEXT);
        """)
    db.executescript("""
            CREATE INDEX IF NOT EXISTS captures_by_status ON captures(status, created);
            CREATE INDEX IF NOT EXISTS summaries_pending ON transcript_tasks(summary_due, updated)
                WHERE summary_status='pending';
            CREATE INDEX IF NOT EXISTS summaries_by_entry ON transcript_tasks(entry_id);
            CREATE INDEX IF NOT EXISTS outbox_by_status ON outbox(status, next_attempt, created);
            CREATE TABLE IF NOT EXISTS material_streams(
                stream_key TEXT PRIMARY KEY, entry_id TEXT NOT NULL,
                source_cursor INTEGER NOT NULL, source_identity TEXT NOT NULL,
                redaction_state TEXT NOT NULL, generation TEXT NOT NULL,
                authorization TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS material_batches(
                batch_id TEXT PRIMARY KEY, stream_key TEXT NOT NULL,
                entry_id TEXT NOT NULL, block_ids TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT NOT NULL,
                authorization TEXT NOT NULL, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS material_batches_due ON material_batches(created,batch_id,entry_id)
                WHERE status IN ('pending','retry-requested');
            CREATE INDEX IF NOT EXISTS material_batches_by_entry ON material_batches(entry_id,status);
        """)
    from ..materials.summarizer import SummaryLedger
    SummaryLedger(db, initialize=True)
    db.execute("INSERT INTO meta VALUES('schema', ?)", (SCHEMA,))

def _validate_schema_identity(db):
    row = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    if row is None or row[0] != SCHEMA:
        raise ValueError("authoritative runtime schema is missing or incompatible; state was not rebuilt")

def _validate_state(db):
    _validate_schema_identity(db)
    from ..materials.summarizer import SummaryLedger
    SummaryLedger(db)

MAX_VOTE_REASON = 1000
RATINGS = ("up", "down")
# Shared read-only projection; callers alias transcript_tasks as t. Missing
# durable work is a diagnostic fault, never permission to recreate a model call.
SUMMARY_STATUS_SQL = ("CASE WHEN t.summary_status='pending' AND NOT EXISTS "
                      "(SELECT 1 FROM material_batches b WHERE b.entry_id=t.entry_id) "
                      "THEN 'missing' ELSE t.summary_status END")
MISSING_MATERIAL_JOB_DETAIL = "material index batch job is missing; no model work was scheduled"
# A rejected lineage row may be replaced by a NEW batch of other material:
# the rejection quarantines the rejected entries, never the whole domain.
REPLACEABLE_BATCH = frozenset({"submitted", "updated", "unchanged", "needs_review",
                               "failed", "rejected"})

# Batch receipts that must never automatically write again.
TERMINAL_BATCH = ("submitted", "updated", "unchanged", "needs_review", "failed",
                  "rejected", "unknown", "disabled", "unavailable")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stored_json(raw, expected, name):
    """Corrupt persisted state cannot become a new empty authority."""
    try:
        value = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid stored {name}: JSON unreadable") from exc
    if not isinstance(value, expected):
        raise ValueError(f"invalid stored {name}: expected {expected.__name__}")
    return value


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _backoff_seconds(base, cap, attempt):
    """Exponential backoff; the exponent is clamped before computing so a
    long-failing persisted counter can never overflow to an error."""
    return min(cap, base * (2.0 ** min(max(0, attempt - 1), 20)))


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


def validate_capture_material_receipt(receipt, domain):
    """Validate only small event metadata, without resolving or loading bodies."""
    fields = {"pipeline", "refs", "redaction_rules", "body_model_calls"}
    if (not isinstance(receipt, dict) or set(receipt) != fields
            or receipt["pipeline"] != "public-transcript"
            or type(receipt["body_model_calls"]) is not int
            or receipt["body_model_calls"] != 0
            or not isinstance(receipt["refs"], list) or len(receipt["refs"]) != 1
            or not isinstance(receipt["refs"][0], str)
            or not re.fullmatch(re.escape(f"mindie://{domain}/") +
                                r"[0-9a-f]{64}@[0-9a-f]{64}", receipt["refs"][0])
            or not isinstance(receipt["redaction_rules"], list)
            or not all(isinstance(rule, str) and rule for rule in receipt["redaction_rules"])):
        raise ValueError("invalid stored capture material receipt")
    return receipt


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
    generation, boundary, scope, activation_epoch, kind="turn", event_key=None,
):
    """Insert or adopt one capture. The caller holds the write transaction.

    A repeat of the same kind and key returns the existing row. An unprocessed row whose
    activation epoch or sharing generation no longer matches is cancelled in
    place; the id stays so the event is not captured again. ``processing``
    and terminal rows are not rewritten. A repeat makes a dormant tail or
    admission wait eligible again.
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
    return dict(id=ident, status="queued", duplicate=False, revoked=None)


class Store:
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
            self.materials.recover_snapshot(snapshot)
            self.db.executemany("INSERT OR IGNORE INTO known_revisions VALUES(?,?)",
                                [(entry_id, revision) for entry_id, revisions in snapshot.items()
                                 for revision in set(revisions.values())])
        except Exception:
            self.db.rollback()
            self.db.close()
            self.materials.close()
            raise
        self.db.commit()

    @contextlib.contextmanager
    def _write_txn(self):
        """Serialize read-modify-write across processes sharing this root."""
        with self.lock, self.materials.validated_headers():
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
            self._finish_material_writes()

    def close(self):
        error = None
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
            self.materials.retain_snapshot(snapshot)
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
        with self.lock:
            entry_id, _ = self._parse_ref(ref.split("@", 1)[0])
            row = self.db.execute(
                "SELECT published_revision, feed_active FROM entries WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown reference in this domain")
            return self._withdrawn(row)

    def explain(self, ref):
        """Current task navigation or one immutable, currently admitted block.

        Block identity binds file bytes, independently of changing task
        navigation. No full-document assembly, historical package or old
        offset-based reading path is reachable through this interface.
        """
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

    def transcript_task(self, task_key):
        with self.lock:
            row = self.db.execute(f"SELECT t.*, {SUMMARY_STATUS_SQL} AS visible_status "
                                  "FROM transcript_tasks t WHERE t.task_key=?", (task_key,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result['summary_status'] = result.pop('visible_status')
        if result['summary_status'] == 'missing':
            result['summary_detail'] = MISSING_MATERIAL_JOB_DETAIL
        return result

    def material_stream(self, stream_key):
        with self.lock:
            row = self.db.execute('SELECT * FROM material_streams WHERE stream_key=?',
                                  (stream_key,)).fetchone()
        return dict(row) if row else None

    def _bind_material_draft(self, doc, *, generation, owner=None):
        """Bind staged file metadata. The outer transaction promotes files."""
        entry_id, now = doc['entry_id'], time.time()
        current = self._row(entry_id)
        self._record_material_revision(doc, 'draft', now)
        if current is None:
            self.db.execute('INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                            (entry_id, doc['kind'], doc['title'], 'draft', doc['revision'],
                             None, 0, None, self._header(doc), now, canonical(doc['conditions'])))
        else:
            self.db.execute('UPDATE entries SET draft_revision=?,title=?,doc=?,updated=? WHERE entry_id=?',
                            (doc['revision'], doc['title'], self._header(doc) if not current['feed_active']
                             else current['doc'], now, entry_id))
        if owner is not None:
            self.db.execute('INSERT OR IGNORE INTO owners VALUES(?,?,?)', (entry_id, owner, now))
        self.grant('draft', entry_id, doc['revision'], generation)
        self._prune_body_history(entry_id)

    def commit_material_increment(self, *, stream_key, entry_id, prepared, start,
                                  end, source_identity, authorization, owner,
                                  observed_stream=None):
        """Both intake paths use this file/cursor/queue transaction."""
        with self._write_txn():
            previous = self.material_stream(stream_key)
            if previous != observed_stream:
                raise BlockingIOError('material stream changed before commit')
            if previous and (previous['generation'] != authorization['generation']
                             or previous['source_cursor'] > start):
                raise ValueError('material source range or authorization changed')
            if self.db.execute('SELECT 1 FROM material_batches WHERE batch_id=?',
                               (prepared['batch_id'],)).fetchone():
                raise ValueError('material range already committed')
            current = self._row(entry_id)
            if self._withdrawn(current):
                raise ValueError('task was withdrawn from the current public feed; not resurrecting it')
            if current and current['feed_active']:
                self.rebase_draft_on_published(entry_id)
                current = self._row(entry_id)
            revision = ((current['draft_revision'] or
                         (current['published_revision'] if current['feed_active'] else None))
                        if current else None)
            prior = self.materials.read_task(entry_id, revision=revision, source='draft') if revision else None
            title = prior['entry']['title'] if prior else 'Task experience awaiting indexing'
            navigation = prior['navigation'] if prior else 'Reference material; indexing is pending.'
            result = self.materials.append_batch(entry_id, prepared['blocks'], navigation,
                                                 title=title, status='pending', source='draft',
                                                 revision=revision, promote=False)
            doc = result['entry']
            self._bind_material_draft(doc, generation=authorization['generation'], owner=owner)
            now = time.time()
            self.db.execute('INSERT OR REPLACE INTO material_streams VALUES(?,?,?,?,?,?,?,?)',
                            (stream_key, entry_id, end, source_identity,
                             canonical(prepared['redaction_state']), authorization['generation'],
                             canonical(authorization), now))
            self.db.execute('INSERT INTO material_batches VALUES(?,?,?,?,?,?,?,?)',
                            (prepared['batch_id'], stream_key, entry_id,
                             canonical([b['block_id'] for b in prepared['blocks']]),
                             'pending', '', canonical(authorization), now))
            self.db.execute('INSERT OR REPLACE INTO transcript_tasks VALUES(?,?,?,?,?,?,?,?,?)',
                            (stream_key, entry_id, authorization.get('id', ''), doc['material_digest'],
                             'pending', '', now, now, canonical(authorization)))
            return doc

    def apply_material_indexes(self, *, entry_id, indexes, navigation, generation):
        with self._write_txn():
            row = self._row(entry_id)
            if row is None or not row['draft_revision'] or not self.granted(
                    'draft', entry_id, row['draft_revision'], generation):
                raise ValueError('material authority changed before index application')
            task = self.materials.read_task(entry_id, revision=row['draft_revision'], source='draft')
            newly_indexed = {item['block_id'] for item in indexes}
            complete = all(item['indexed'] or item['block_id'] in newly_indexed for item in task['blocks'])
            result = self.materials.update_indexes(entry_id, indexes, navigation['summary'],
                                                   title=navigation['title'], source='draft',
                                                   revision=row['draft_revision'], promote=False,
                                                   status='complete' if complete else 'pending')
            self._bind_material_draft(result['entry'], generation=generation)
            return result

    def redaction_key(self):
        """Private random HMAC key; no original sensitive values are retained."""
        import secrets
        with self._write_txn():
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('redaction_key', ?)", (secrets.token_hex(32),))
            value = self.db.execute("SELECT value FROM meta WHERE key='redaction_key'").fetchone()[0]
        return bytes.fromhex(value)

    def update_draft_header(self, entry_id, *, expected_body, title, summary, generation):
        """Metadata-only compare/apply: a model can never supply body content."""
        with self._write_txn():
            row = self._row(entry_id)
            if row is None or not row['draft_revision']:
                return False
            base = self._revision_doc(entry_id, row['draft_revision'])
            if digest(base['content']) != expected_body or not self.granted('draft', entry_id, base['revision'], generation):
                return False
            doc = dict(base, title=title, summary=summary)
            doc['revision'] = documents.revision_of(doc)
            documents.validate(doc)
            now = time.time()
            self._insert_revision(doc, 'draft', now)
            self.db.execute("INSERT OR REPLACE INTO grants VALUES(?,?,?,?,?)", ('draft', entry_id, doc['revision'], generation, now))
            visible = not row['feed_active']
            self.db.execute("UPDATE entries SET draft_revision=?, title=?, doc=?, updated=? WHERE entry_id=?",
                            (doc['revision'], title, self._header(doc) if visible else row['doc'], now, entry_id))
            if visible:
                self._mark_material_changed(entry_id)
            self._prune_body_history(entry_id)
            return True

    def _prune_body_history(self, entry_id=None):
        """Keep only current work and the active published body; no historical bodies.

        Called in the same transaction as a draft update. The unscoped form
        runs once when an existing store adopts latest-only body retention.
        SQLite reuses freed pages; this does not run VACUUM on the hot path.
        """
        args = () if entry_id is None else (entry_id,)
        revision_scope = "" if entry_id is None else " AND r.entry_id=?"
        grant_scope = "" if entry_id is None else " AND g.identity=?"
        # Withdrawn published material leaves only its identity/current state.
        # An unsent working draft is still current work and stays separately.
        entry_scope = "" if entry_id is None else " AND entry_id=?"
        self.db.execute(
            "UPDATE entries SET doc=json_set(doc, '$.content', '') "
            "WHERE published_revision IS NOT NULL AND feed_active=0" + entry_scope,
            args,
        )
        # Feedback can arrive after a newer revision retires these headers.
        # Preserve only the two identities, never historical prose or bodies.
        self.db.execute(
            "INSERT OR IGNORE INTO known_revisions SELECT entry_id,revision FROM revisions AS r WHERE 1=1"
            + revision_scope, args,
        )
        removed = self.db.execute(
            "DELETE FROM revisions AS r WHERE 1=1" + revision_scope +
            " AND NOT EXISTS (SELECT 1 FROM entries AS e WHERE e.entry_id=r.entry_id"
            " AND (r.revision=e.draft_revision OR (e.feed_active=1"
            " AND r.revision=e.published_revision)))", args,
        ).rowcount
        self.db.execute(
            "DELETE FROM grants AS g WHERE g.kind='draft'" + grant_scope +
            " AND NOT EXISTS (SELECT 1 FROM entries AS e WHERE e.entry_id=g.identity"
            " AND e.draft_revision=g.revision)", args,
        )
        if entry_id is not None:
            self._material_dirty.add(entry_id)
        return removed

    def _insert_revision(self, doc, source, created):
        saved = self.materials.put_document(doc, source=source)
        doc.update(saved)
        self._record_material_revision(doc, source, created)

    def _record_material_revision(self, doc, source, created):
        self.db.execute('INSERT OR IGNORE INTO known_revisions VALUES(?,?)',
                        (doc['entry_id'], doc['revision']))
        self.db.execute(
            'INSERT INTO revisions VALUES(?,?,?,?,?) ON CONFLICT(entry_id, revision) DO UPDATE SET '
            "source=CASE WHEN revisions.source='feed' THEN 'feed' ELSE excluded.source END",
            (doc['entry_id'], doc['revision'], self._header(doc), source, created))
        self._material_dirty.add(doc['entry_id'])

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
                 None, 0, None, self._header(doc), now,
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
            self._mark_material_changed(entry_id)
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
                self._insert_revision(doc, "draft", now)
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
                     self._header(doc) if visible else current["doc"], now,
                     canonical(doc["conditions"]) if visible
                     else current["conditions"],
                     entry_id),
                )
                if visible:
                    # Only a genuinely visible document enters the index; for
                    # a feed-active entry the published body keeps the row.
                    self._mark_material_changed(entry_id)
                self._prune_body_history(entry_id)
                return doc, True
        raise BlockingIOError(
            "draft kept changing across bounded retries; append not applied "
            "now (transient contention, resumed by the existing scheduler)"
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

    def _changed_draft_refs(self, generation, limit=None, *, ready_only=False):
        sql = "SELECT entries.entry_id, entries.draft_revision FROM entries "
        params = []
        if generation is not None:
            sql += ("JOIN grants ON grants.kind='draft' "
                    "AND grants.identity=entries.entry_id "
                    "AND grants.revision=entries.draft_revision AND grants.generation=? ")
            params.append(generation)
        sql += ("WHERE entries.draft_revision IS NOT NULL "
                "AND entries.draft_revision != COALESCE(entries.batched_revision, '') "
                f"AND {self._NOT_WITHDRAWN} AND {self._NOT_QUARANTINED}")
        if ready_only:
            sql += (" AND NOT EXISTS (SELECT 1 FROM transcript_tasks t "
                    "WHERE t.entry_id=entries.entry_id AND t.summary_status!='complete')"
                    " AND (NOT EXISTS (SELECT 1 FROM material_streams h WHERE h.entry_id=entries.entry_id)"
                    " OR EXISTS (SELECT 1 FROM transcript_tasks t WHERE t.entry_id=entries.entry_id))")
        if limit is not None:
            sql += ' LIMIT ?'
            params.append(limit)
        return self.db.execute(sql, params)

    def has_changed_drafts(self, *, generation=None, ready_only=False):
        """Check the same publication eligibility without loading any body."""
        with self.lock:
            return self._changed_draft_refs(generation, limit=1, ready_only=ready_only).fetchone() is not None

    def drafts_changed(self, *, generation=None, ready_only=False, include_content=True):
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
            rows = self._changed_draft_refs(generation, ready_only=ready_only).fetchall()
            if not include_content:
                return [self.materials.read_task(r['entry_id'], revision=r['draft_revision'])['entry']
                        for r in rows]
            return [self._revision_doc(r["entry_id"], r["draft_revision"])
                    for r in rows]

    def summary_ready(self, doc):
        """A transcript summary must describe the exact body being exported.

        Other entry producers have no transcript job and keep their own
        validation contract. A stale summary cannot authorize a newer body.
        """
        with self.lock:
            tasks = self.db.execute('SELECT summary_status,body_digest FROM transcript_tasks WHERE entry_id=?',
                                    (doc['entry_id'],)).fetchall()
            if not tasks and self.db.execute('SELECT 1 FROM material_streams WHERE entry_id=?', (doc['entry_id'],)).fetchone():
                return False
        return all(t['summary_status'] == 'complete' and t['body_digest'] == doc['material_digest'] for t in tasks)


    # ---------------------------------------------------------------- search

    def query(self, query=None, limit=5, conditions=None, continuation=None):
        from ..materials.provenance import QueryContinuationError, query_request
        from ..materials.catalog import CurrentVisibility
        query_request(query, limit, conditions, continuation)
        with self.lock:
            try:
                if self._material_dirty:
                    raise ValueError("material metadata has uncompleted pointer promotion")
                generation = self.materials.current_generation()
                # A different local Store can commit metadata and then fail
                # before promoting the material directory. Catalog generation
                # alone must not authorize a cached older visibility snapshot.
                data_version = self.db.execute('PRAGMA data_version').fetchone()[0]
                cache_version = (generation, data_version)
                if getattr(self, '_query_version', None) != cache_version:
                    self._query_rows = {r['entry_id']: dict(r) for r in self.db.execute(
                        'SELECT * FROM entries WHERE ' + self._VISIBLE_SQL)}
                    self._query_allowed = CurrentVisibility(generation, {
                        key: r['published_revision'] if r['feed_active'] else r['draft_revision']
                        for key, r in self._query_rows.items()})
                    self._query_version = cache_version
                rows, allowed = self._query_rows, self._query_allowed
                found = self.materials.search(query, limit=limit, conditions=conditions, allowed_ids=allowed,
                                              continuation=continuation)
            except QueryContinuationError:
                raise
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
                raise MaterialReadError("Current material could not be searched; inspect service diagnostics.") from exc
            def observable(item):
                row = rows[item['entry_id']]
                result = dict(item, origin='feed' if row['feed_active'] else row['origin'],
                              supplemental=bool(row['feed_active'] and row['draft_revision']))
                if 'related' in item:
                    result['related'] = [observable(match) for match in item['related']]
                return result
            results = []
            for item in found:
                results.append(observable(item))
        return dict(domain=self.domain, retrieval='reme-bm25', results=results,
                    page='related' if continuation is not None else 'groups',
                    note='Reference material, including failed attempts and uncertainty. Citation groups are navigation, '
                         'not independent evidence or factual confidence. Current source links do not reproduce an '
                         'unavailable cited revision. Use each match ref to read its actual block. '
                         'Related excerpts are locating aids and may omit later corrections or applicability limits; '
                         'read the block when those details matter.')

    # ------------------------------------------------------------ feed switch

    def feed_get(self, key):
        with self.lock:
            row = self.db.execute(
                "SELECT value FROM feed_state WHERE key=?", (key,)
            ).fetchone()
        return _stored_json(row[0], dict, 'feed state') if row else None

    def feed_set(self, key, value):
        with self._write_txn():
            self.db.execute(
                "INSERT OR REPLACE INTO feed_state VALUES(?,?)",
                (key, canonical(value)),
            )

    def install_feed(self, packages, *, feed_ident, source_revision=None):
        """Install one whole Git snapshot into the current file material store.

        All package bytes validate and stage before changing current metadata;
        the root transaction excludes concurrent snapshot pruning and promotes
        the selected revisions together after commit. Only entry headers and
        memberships are retained in SQLite.
        """
        from mindie_knowledge.materials import validate_package_files

        headers = []
        def candidates():
            for package in packages:
                value = validate_package_files(package['files'], self.domain)
                headers.append(value['entry'])
                yield value
        now = time.time()
        with self._write_txn():
            self.materials.install_packages(candidates(), source="feed",
                                            source_revision=source_revision, promote=False)
            self.db.execute("UPDATE entries SET feed_active=0 WHERE feed_active=1")
            for doc in headers:
                row = self._row(doc["entry_id"])
                self._record_material_revision(doc, "feed", now)
                if row is None:
                    self.db.execute(
                        "INSERT INTO entries(entry_id,kind,title,origin,draft_revision,published_revision,"
                        "feed_active,batched_revision,doc,updated,conditions) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (doc["entry_id"], doc["kind"], doc["title"], "feed", None, doc["revision"],
                         1, None, self._header(doc), now, canonical(doc["conditions"])))
                else:
                    draft = None if row["draft_revision"] == doc["revision"] else row["draft_revision"]
                    self.db.execute(
                        "UPDATE entries SET kind=?,title=?,draft_revision=?,published_revision=?,"
                        "feed_active=1,doc=?,updated=?,conditions=? WHERE entry_id=?",
                        (doc["kind"], doc["title"], draft, doc["revision"], self._header(doc),
                         now, canonical(doc["conditions"]), doc["entry_id"]))
            # Upstream withdrawal removes public visibility and its file pointer;
            # a superseded local draft never silently reappears in consumer search.
            self._mark_all_material_changed()
            self._prune_body_history()
            if source_revision is not None:
                self.db.execute("INSERT OR REPLACE INTO feed_state VALUES(?,?)",
                                ("published-commit", canonical(dict(commit=source_revision, feed=feed_ident))))
        return dict(entries=len(headers))

    def retry_feed_cleanup(self):
        """Retry only retiring unused material files after a committed switch."""
        with self._write_txn():
            self._mark_all_material_changed()

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
        observed = parse_feedback_ref(ref, domain=self.domain)
        with self._write_txn():
            entry_id, revision = observed['task_id'], observed['revision']
            row = self._row(entry_id)
            if row is None:
                raise ValueError("unknown reference in this domain")
            if self.db.execute('SELECT 1 FROM known_revisions WHERE entry_id=? AND revision=?',
                               (entry_id, revision)).fetchone() is None:
                raise ValueError("unknown observed revision in this domain")
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
                     generation=None, source_revisions=None, entry_revisions=None):
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
                                   entry_revisions=entry_revisions))
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

    def cancel_pending(self, detail):
        """Cancel unsent operations without deserializing their payloads."""
        with self._write_txn():
            return self.db.execute(
                "UPDATE outbox SET status='disabled', detail=?, updated=? "
                "WHERE status='pending' AND attempted IS NULL", (detail, time.time())
            ).rowcount

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
                           batch_id=row["batch_id"], batch_revision=row["revision"])
            if entry_id in sources:
                receipt["source_published_revision"] = sources[entry_id]
            self.db.execute("INSERT OR REPLACE INTO feed_state VALUES(?,?)",
                            ("sent-package:" + entry_id, canonical(receipt)))
            self.db.execute(
                "INSERT OR REPLACE INTO sent_receipts(entry_id,generation,sent_revision,path,sha256,"
                "head_sha,repository,pr_url,batch_id,updated,markers,batch_revision) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (entry_id, row.get("generation"), revision, index_path, index["sha256"],
                 row["head_sha"], None, row.get("pr_url"), row["batch_id"], now, None, row["revision"]),
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

    def rebase_draft_on_published(self, entry_id):
        """Adopt a changed confirmed feed without restoring sent/removed blocks.

        Frozen or uncertain sends stay intact. Open-PR edits are still checked
        against their actual remote head by the publisher and may conflict;
        this method never treats main as an edited PR's replacement base.
        """
        with self._write_txn():
            row = self._row(entry_id)
            if row is None or not row["feed_active"]:
                return None
            previous = self.feed_get("continuation-base:" + entry_id)
            if previous and previous["published_revision"] == row["published_revision"]:
                return self.materials.read_task(entry_id, row["draft_revision"], "draft")["entry"] if row["draft_revision"] else None
            if self.db.execute("SELECT 1 FROM transcript_tasks WHERE entry_id=? AND summary_status!='complete'",
                               (entry_id,)).fetchone():
                # A saved/in-flight index result still owns its local input.
                # Let that existing attempt settle first; publication already
                # requires complete indexes, then adopts remote navigation.
                return self.materials.read_task(entry_id, row["draft_revision"], "draft")["entry"] if row["draft_revision"] else None
            # A frozen candidate is owned by its pending/uncertain operation.
            # It is never invalidated by feed synchronization or new capture.
            for operation in self.db.execute("SELECT status,batch FROM outbox"):
                if operation["status"] in REPLACEABLE_BATCH:
                    continue
                descriptor = _stored_json(operation["batch"], dict, "outbox batch")
                if any((_exact_revision_ref(ref) or (None,))[0] == entry_id
                       for ref in descriptor.get("entry_refs", [])):
                    return self.materials.read_task(entry_id, row["draft_revision"], "draft")["entry"] if row["draft_revision"] else None
            sent = self.sent_receipt(entry_id)
            if sent is None:
                return self.materials.read_task(entry_id, row["draft_revision"], "draft")["entry"] if row["draft_revision"] else None
            sent_package = self.feed_get("sent-package:" + entry_id)
            if not isinstance(sent_package, dict) or "source_published_revision" not in sent_package:
                raise ValueError("confirmed sent package lacks its prepared feed identity")
            if sent_package["source_published_revision"] == row["published_revision"]:
                # Main has not advanced since this PR was sent. Its blocks
                # may still await merge; a stale main must not discard them.
                return self.materials.read_task(entry_id, row["draft_revision"], "draft")["entry"] if row["draft_revision"] else None
            public = self.materials.read_task(entry_id, revision=row["published_revision"], source="feed")
            files = [dict(path=f"tasks/{entry_id}/index.md", sha256=hashlib.sha256(
                self.materials._manifest_path(entry_id, row["published_revision"]).read_bytes()).hexdigest())]
            files.extend(dict(path=f"tasks/{entry_id}/blocks/{block['block_id']}.md", sha256=block["sha256"])
                         for block in public["blocks"])
            if row["draft_revision"] and row["draft_revision"] != row["published_revision"]:
                rebased = self.materials.rebase_unsent_blocks(
                    entry_id, draft_revision=row["draft_revision"], published_revision=row["published_revision"],
                    sent_files=self.sent_package_files(entry_id))
                doc = rebased["header"]["entry"]
                if rebased["additions"]:
                    self._record_material_revision(doc, "draft", time.time())
                    self.db.execute("INSERT OR REPLACE INTO grants SELECT kind,identity,?,generation,created "
                                    "FROM grants WHERE kind='draft' AND identity=? AND revision=?",
                                    (doc["revision"], entry_id, row["draft_revision"]))
                    self.db.execute("UPDATE entries SET draft_revision=? WHERE entry_id=?",
                                    (doc["revision"], entry_id))
                    # Existing indexes still cover the same unsent blocks.
                    # No model call or replay is needed to adopt remote metadata.
                    self.db.execute("UPDATE transcript_tasks SET body_digest=? WHERE entry_id=? "
                                    "AND summary_status='complete'",
                                    (doc["material_digest"], entry_id))
                else:
                    self.db.execute("UPDATE entries SET draft_revision=NULL WHERE entry_id=?", (entry_id,))
                self._prune_body_history(entry_id)
            elif row["draft_revision"]:
                self.db.execute("UPDATE entries SET draft_revision=NULL WHERE entry_id=?", (entry_id,))
                self._prune_body_history(entry_id)
            self.feed_set("continuation-base:" + entry_id, dict(
                published_revision=row["published_revision"], sent_head_sha=sent["head_sha"], files=files))
            current = self._row(entry_id)
            return self.materials.read_task(entry_id, current["draft_revision"], "draft")["entry"] if current["draft_revision"] else None

    def contribution_base_files(self, entry_id):
        """Exact confirmed base used by the current append-only candidate."""
        with self.lock:
            base = self.feed_get("continuation-base:" + entry_id)
            sent = self.sent_receipt(entry_id)
            if base and sent and base["sent_head_sha"] == sent["head_sha"]:
                return base["files"]
            return self.sent_package_files(entry_id)

    def restore_draft(self, entry_id, doc, *, generation=None):
        """Re-seed a compacted append-base from the exact confirmed remote
        body (fetched boundedly by the caller). Never overwrites an existing
        local draft, never resurrects a withdrawn entry and never fabricates
        a base from only the new paragraph."""
        documents.validate(doc)
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
                 self._header(doc) if visible else row["doc"], now,
                 canonical(doc["conditions"]) if visible else row["conditions"],
                 entry_id),
            )
            if visible:
                self._mark_material_changed(entry_id)
            self._prune_body_history(entry_id)
            return doc

    # --------------------------------------------------------------- capture

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
