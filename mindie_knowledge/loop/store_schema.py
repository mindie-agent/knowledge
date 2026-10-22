"""[Role] Schema, validation, pure identity helpers and read-only projections for `mindie-store/4`.

[Invariants]
- `_STATE_COLUMNS` is the single static description checked once by
  `open_database`; there is no in-memory DDL reflection on a hot path.
- Schema identity and required columns are validated before any mutation;
  damage raises instead of rebuilding or truncating state.
- Utility functions are deterministic and never open a database.

[Timeouts & Locks]
- No lock, wait or SQLite connection is created here; the owning Store holds
  its RLock and passes its already-open connection.

[Side-Effect Policy]
- Pure values and schema DDL helpers only; writes happen through the caller's
  connection inside its explicit transaction.
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
    'material_summary_attempts': ('attempt_id task_id batch_id body_version input_digest '
                                  'policy_identity block_ids status response error created updated').split(),
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
    from ..materials.summarizer import _initialize_ledger
    _initialize_ledger(db)
    db.execute("INSERT INTO meta VALUES('schema', ?)", (SCHEMA,))

def _validate_schema_identity(db):
    row = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    if row is None or row[0] != SCHEMA:
        raise ValueError("authoritative runtime schema is missing or incompatible; state was not rebuilt")

def _validate_state(db):
    # Column presence is already checked once by open_database's static
    # `_STATE_COLUMNS`; do not reflect or re-probe the schema here.
    _validate_schema_identity(db)

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


class QueryMixin:

    def query(self, query=None, limit=5, conditions=None, continuation=None,
              domain_path=None, token_budget=1200, include_unknown=False,
              include_archived=False, navigation_only=False, run_id=None,
              mode="reuse", ledger_entries=None, bottleneck_summary=None,
              native_subcontext=False, goal=""):
        options = dict(mode=mode, ledger_entries=ledger_entries,
                       bottleneck_summary=bottleneck_summary,
                       native_subcontext=native_subcontext, goal=goal)
        if continuation is not None or not self.graded.available():
            result = self._query_materials(query, limit, conditions, continuation)
            if mode != "reuse":
                result["mode"] = mode
            return result
        legacy = self._query_materials(query, limit, conditions)
        if legacy["results"]:
            from ..graded.retrieval import mixed_query
            return mixed_query(self, legacy, query, limit=limit, conditions=conditions,
                               domain_path=domain_path, token_budget=token_budget,
                               include_unknown=include_unknown, include_archived=include_archived,
                               navigation_only=navigation_only,
                               run_id=run_id, **options)
        return self.graded.query(query, limit=limit, conditions=conditions,
                                 domain_path=domain_path, token_budget=token_budget,
                                 include_unknown=include_unknown, include_archived=include_archived,
                                 navigation_only=navigation_only,
                                 run_id=run_id, **options)
    def _query_materials(self, query=None, limit=5, conditions=None, continuation=None):
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
                # A graded-only store has no current material to rank. Keep
                # legacy cursor validation, but do not load its search engine
                # when SQLite already proves there are no visible entries.
                found = (self.materials.search(query, limit=limit, conditions=conditions, allowed_ids=allowed,
                                               continuation=continuation)
                         if rows or continuation is not None else [])
            except QueryContinuationError:
                raise
            except TimeoutError:
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
    def material_feeds(self):
        """Per-source membership; old stores have one evidenced active source."""
        recorded = self.feed_get("material-feeds")
        if recorded is not None:
            return recorded["feeds"]
        previous = self.feed_get("published-commit") or {}
        if not previous.get("feed") or previous.get("contract_schema") == "mindie-publication-contract/2":
            return {}
        with self.lock:
            entries = {row["entry_id"]: row["published_revision"] for row in self.db.execute(
                "SELECT entry_id,published_revision FROM entries WHERE feed_active=1")}
        return {previous["feed"]: dict(entries=entries, source_revision=previous.get("commit"))}
    def retire_material_feed(self, feed_ident):
        """Withdraw exactly one old-format source after its verified switch."""
        feeds = self.material_feeds()
        previous = feeds.pop(feed_ident, None)
        # Materialize a legacy store's one-source receipt before another format
        # replaces the compatibility published-commit pointer.
        self.feed_set("material-feeds", {"feeds": feeds})
        if previous is None:
            return {}
        retained = {ident for source in feeds.values() for ident in source["entries"]}
        for ident in set(previous["entries"]) - retained:
            self.db.execute("UPDATE entries SET feed_active=0 WHERE entry_id=?", (ident,))
            self._mark_material_changed(ident)
        self._query_version = None
        return previous["entries"]
    def retained_material_revisions(self):
        """Format-switch evidence retains exact bytes without public pointers."""
        retained = {}
        for row in self.db.execute("SELECT value FROM feed_state WHERE key LIKE 'graded-format-switch:%'"):
            receipt = _stored_json(row[0], dict, "format switch")
            for ident, revisions in receipt.get("retained_task_revisions", {}).items():
                retained.setdefault(ident, set()).update(revisions)
        return retained
    def install_feed(self, packages, *, feed_ident, source_revision=None):
        """Install one whole Git snapshot into the current file material store.

        All package bytes validate and stage before changing current metadata;
        the root transaction excludes concurrent snapshot pruning and promotes
        the selected revisions together after commit. Only entry headers and
        memberships are retained in SQLite.
        """
        headers = []

        def accept_metadata(package):
            if not package["ready"]:
                raise ValueError("published task package has unfinished material indexes")
            headers.append(package["entry"])

        now = time.time()
        with self._write_txn():
            self.materials.install_packages(
                packages, source="feed", source_revision=source_revision,
                promote=False, on_validated=accept_metadata,
            )
            feeds = self.material_feeds()
            incoming = {doc["entry_id"]: doc["revision"] for doc in headers}
            for identity, source in feeds.items():
                if identity != feed_ident and any(ident in incoming and incoming[ident] != revision
                                                 for ident, revision in source["entries"].items()):
                    raise ValueError("material feeds disagree on one stable task revision")
            # One immutable source snapshot has one selected format. Withdraw
            # its graded authority before publishing the material replacement.
            self.graded.retire_feed(feed_ident)
            previous = feeds.get(feed_ident, {"entries": {}})
            feeds[feed_ident] = dict(entries=incoming, source_revision=source_revision)
            retained = {ident for source in feeds.values() for ident in source["entries"]}
            for ident in set(previous["entries"]) - retained:
                self.db.execute("UPDATE entries SET feed_active=0 WHERE entry_id=?", (ident,))
            self.feed_set("material-feeds", {"feeds": feeds})
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
            for ident in set(previous["entries"]) | set(incoming):
                self._prune_body_history(ident)
            if source_revision is not None:
                self.db.execute("INSERT OR REPLACE INTO feed_state VALUES(?,?)",
                                ("published-commit", canonical(dict(commit=source_revision, feed=feed_ident))))
        return dict(entries=len(headers))
    def retry_feed_cleanup(self):
        """Retry only retiring unused material files after a committed switch."""
        with self._write_txn():
            self._mark_all_material_changed()

    # ------------------------------------------------------- entry quarantine
