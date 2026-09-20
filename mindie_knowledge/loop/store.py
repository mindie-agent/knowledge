"""Versioned local knowledge store, scoped to one domain.

This is the ``mindie-store/2`` runtime store (``store-v2.sqlite3``). It
replaces content-as-identity with stable opaque entry IDs plus an explicit
revision history: every known body — local draft revisions and published
revisions installed from the content repository — is retained, so an old
pinned reference always reads the exact historical bytes while ordinary
search shows the current published version. Local drafts update by
append-only, marker-deduplicated observations; publication state
(active/retired) arrives only from the canonical Git publication.

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
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from mindie_knowledge.markdown import _atomic_write_text

from . import documents
from .documents import DraftFull

SCHEMA = "mindie-store/2"
MAX_ENTRIES = 10000
MAX_VOTE_REASON = 1000
RATINGS = ("up", "down")

# Batch receipts that must never automatically write again.
TERMINAL_BATCH = ("submitted", "updated", "unchanged", "needs_review", "failed",
                  "unknown", "disabled", "unavailable")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def session_key(value):
    """Local hash of a native session id; never exported."""
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("session_id must be nonempty text of at most 256 characters")
    return digest(value.strip())


def new_identity():
    return secrets.token_hex(32)


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
            self.root / "store-v2.sqlite3", check_same_thread=False
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS entries(entry_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
                origin TEXT NOT NULL, draft_revision TEXT, published_revision TEXT,
                feed_active INTEGER NOT NULL DEFAULT 0, batched_revision TEXT,
                doc TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS revisions(entry_id TEXT NOT NULL,
                revision TEXT NOT NULL, doc TEXT NOT NULL, source TEXT NOT NULL,
                created REAL NOT NULL, PRIMARY KEY(entry_id, revision));
            CREATE TABLE IF NOT EXISTS captures(id TEXT PRIMARY KEY,
                root_session TEXT NOT NULL, session TEXT NOT NULL, turn TEXT NOT NULL,
                transcript TEXT, summary TEXT NOT NULL, status TEXT NOT NULL,
                detail TEXT NOT NULL, created REAL NOT NULL);
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
                updated REAL NOT NULL, PRIMARY KEY(root_opaque, entry_id));
            CREATE TABLE IF NOT EXISTS opaque_roots(root_hash TEXT PRIMARY KEY,
                opaque TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox(batch_id TEXT PRIMARY KEY,
                revision TEXT NOT NULL, batch TEXT NOT NULL, status TEXT NOT NULL,
                detail TEXT NOT NULL, pr_url TEXT, head_sha TEXT,
                created REAL NOT NULL, attempted REAL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS feed_state(key TEXT PRIMARY KEY,
                value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,
                value TEXT NOT NULL);
        """)
        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES('schema', ?)", (SCHEMA,)
        )
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

    # ------------------------------------------------------------------ refs

    def ref(self, entry_id, revision=None):
        base = f"mindie://{self.domain}/{entry_id}"
        return f"{base}@{revision}" if revision else base

    def _parse_ref(self, ref):
        prefix = f"mindie://{self.domain}/"
        text = ref if isinstance(ref, str) else ""
        if text.startswith(prefix):
            text = text[len(prefix):]
        elif re.fullmatch(r"[0-9a-f]{64}(@[0-9a-f]{64})?", text or ""):
            pass
        else:
            raise ValueError("reference is outside the selected domain")
        entry, _, revision = text.partition("@")
        if not documents.HEX_RE.fullmatch(entry):
            raise ValueError("reference is outside the selected domain")
        if revision and not documents.HEX_RE.fullmatch(revision):
            raise ValueError("invalid pinned revision")
        return entry, revision or None

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

    def get(self, ref):
        """Exact document for a reference; a pinned revision reads history."""
        entry_id, revision = self._parse_ref(ref)
        with self.lock:
            if revision:
                doc = self._revision_doc(entry_id, revision)
                if doc is None:
                    raise ValueError("unknown pinned revision in this domain")
                return doc
            row = self._row(entry_id)
            if row is None:
                raise ValueError("unknown reference in this domain")
            return json.loads(row["doc"])

    def explain(self, ref, *, offset=0, limit=None):
        doc = self.get(ref)
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 65536):
            raise ValueError("limit must be between 1 and 65536")
        body = doc["content"]
        total = len(body)
        sliced = body[offset : offset + limit if limit is not None else None]
        return dict(doc, content=sliced, content_offset=offset,
                    content_length=total, ref=self.ref(doc["entry_id"], doc["revision"]))

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

    def create_draft(self, *, kind, title, summary, content, conditions=None,
                     sources=(), producers=(), entry_id=None, origin="draft"):
        """Create one local draft with a fresh opaque stable identity."""
        if origin not in {"draft"}:
            raise ValueError("local entries start as drafts")
        entry_id = entry_id or new_identity()
        if not documents.HEX_RE.fullmatch(entry_id):
            raise ValueError("entry_id must be a 64-character hex identity")
        doc = documents.make_entry(
            entry_id=entry_id, domain=self.domain, kind=kind, title=title,
            summary=summary, content=content, conditions=conditions,
            sources=sources, producers=producers,
        )
        now = time.time()
        with self._write_txn():
            if self._row(entry_id) is not None:
                raise ValueError("entry identity already exists")
            self._insert_revision(doc, origin, now)
            self.db.execute(
                "INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (entry_id, kind, doc["title"], "active", origin, doc["revision"],
                 None, 0, None, canonical(doc), now),
            )
            self._write_draft_file(doc)
        return doc

    def append_observation(self, entry_id, addition, *, marker, producer=None):
        """Append one bounded observation/correction to a local draft.

        Idempotent on ``marker`` (the reserved increment identity): a repeated
        Stop never creates a near-duplicate section. Only the producing root
        may extend a draft; published bodies are never rewritten locally.
        Returns ``(doc, appended)``.
        """
        now = time.time()
        with self._write_txn():
            row = self._row(entry_id)
            if row is None:
                raise ValueError("unknown draft in this domain")
            if not row["draft_revision"]:
                raise ValueError("entry has no local draft to update")
            base = self._revision_doc(entry_id, row["draft_revision"])
            if producer is not None and producer not in base["producers"]:
                raise ValueError("only the producing task may update its draft")
            doc, appended = documents.append_observation(base, addition, marker=marker)
            if not appended:
                return doc, False
            self._insert_revision(doc, row["origin"], now)
            visible = not row["feed_active"]
            self.db.execute(
                "UPDATE entries SET draft_revision=?, title=?, doc=?, updated=? "
                "WHERE entry_id=?",
                (doc["revision"], doc["title"],
                 canonical(doc) if visible else row["doc"], now, entry_id),
            )
            self._write_draft_file(doc)
            return doc, True

    def drafts_changed(self):
        """Draft revisions not yet included in any outbox batch."""
        with self.lock:
            return [
                json.loads(r["doc"])
                for r in self.db.execute(
                    "SELECT doc FROM entries WHERE draft_revision IS NOT NULL "
                    "AND draft_revision != COALESCE(batched_revision, '')"
                )
            ]

    def draft_headers(self, *, producer=None, limit=8, excerpt=600):
        """Compact headers plus short excerpts as organizer context."""
        with self.lock:
            rows = self.db.execute(
                "SELECT doc FROM entries WHERE draft_revision IS NOT NULL "
                "ORDER BY updated DESC LIMIT ?", (limit * 4,),
            ).fetchall()
        headers = []
        for row in rows:
            doc = json.loads(row["doc"])
            if producer is not None and producer not in doc["producers"]:
                continue
            headers.append(dict(
                entry_id=doc["entry_id"], title=doc["title"],
                revision=doc["revision"],
                summary=doc["summary"], excerpt=doc["content"][:excerpt],
            ))
            if len(headers) >= limit:
                break
        return headers

    # ---------------------------------------------------------------- search

    def query(self, query, limit=5, conditions=None):
        """BM25 over visible entries: published versions win; retired and
        withdrawn-from-feed entries stay explainable but leave the results.
        Draft overlays on published entries are labeled, never a second hit."""
        from mindie_knowledge.markdown import Document
        from mindie_knowledge.retrieval import lexical_search

        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("query must be nonempty text of at most 2000 characters")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        if conditions is not None and not isinstance(conditions, dict):
            raise ValueError("conditions must be an object")
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM entries WHERE status='active' AND (feed_active=1 "
                "OR draft_revision IS NOT NULL) LIMIT ?", (MAX_ENTRIES + 1,),
            ).fetchall()
            if len(rows) > MAX_ENTRIES:
                raise ValueError(
                    "domain exceeds the initial lexical capacity; split it or configure a larger index"
                )
            selected, docs = {}, []
            for row in rows:
                doc = json.loads(row["doc"])
                if (
                    doc["kind"] == "knowledge"
                    and conditions
                    and any(
                        key in doc["conditions"] and doc["conditions"][key] != value
                        for key, value in conditions.items()
                    )
                ):
                    continue
                supplemental = bool(
                    row["feed_active"]
                    and row["draft_revision"]
                    and row["draft_revision"] != row["published_revision"]
                )
                selected[doc["entry_id"]] = (doc, row, supplemental)
                docs.append(Document(
                    layer=doc["kind"], title=doc["title"],
                    content=doc["summary"] + "\n" + doc["content"],
                    slug=doc["entry_id"],
                    path=self.root / "drafts" / f"{doc['entry_id']}.md",
                    uri=doc["entry_id"],
                ))
            hits = lexical_search(query, docs, limit=len(docs))
            output = []
            for hit in hits:
                doc, row, supplemental = selected[hit.uri]
                output.append(dict(
                    ref=self.ref(doc["entry_id"]), revision=doc["revision"],
                    kind=doc["kind"], title=doc["title"], summary=doc["summary"],
                    conditions=doc["conditions"], status=doc["status"],
                    origin=row["origin"], supplemental=supplemental,
                    score=round(hit.score, 8),
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

    def install_feed(self, docs, *, feed_ident):
        """Atomically switch published membership to one validated Git tree.

        Every revision body is retained; entries the tree no longer carries
        leave ordinary search but stay readable by reference. Local drafts are
        untouched; a published revision of the same entry folds over its draft
        in search while the draft remains a labeled private supplement.
        """
        now = time.time()
        seen = set()
        with self._write_txn():
            for doc in docs:
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
                        "INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (doc["entry_id"], doc["kind"], doc["title"], doc["status"],
                         "feed", None, doc["revision"], 1, None,
                         canonical(doc), now),
                    )
                else:
                    self.db.execute(
                        "UPDATE entries SET kind=?, title=?, status=?, "
                        "published_revision=?, feed_active=1, doc=?, updated=? "
                        "WHERE entry_id=?",
                        (doc["kind"], doc["title"], doc["status"], doc["revision"],
                         canonical(doc), now, doc["entry_id"]),
                    )
            if seen:
                self.db.execute(
                    "UPDATE entries SET feed_active=0 WHERE origin='feed' "
                    f"AND entry_id NOT IN ({','.join('?' for _ in seen)})",
                    tuple(seen),
                )
                # Entries with a local draft keep it visible; feed-only entries
                # leave search entirely but remain explainable.
                self.db.execute(
                    "UPDATE entries SET doc=(SELECT revisions.doc FROM revisions "
                    "WHERE revisions.entry_id=entries.entry_id "
                    "AND revisions.revision=entries.draft_revision), "
                    "status='active' "
                    "WHERE feed_active=0 AND origin='feed' AND draft_revision IS NOT NULL"
                )
            else:
                self.db.execute(
                    "UPDATE entries SET feed_active=0 WHERE origin='feed'"
                )
                self.db.execute(
                    "UPDATE entries SET doc=(SELECT revisions.doc FROM revisions "
                    "WHERE revisions.entry_id=entries.entry_id "
                    "AND revisions.revision=entries.draft_revision), "
                    "status='active' "
                    "WHERE feed_active=0 AND origin='feed' AND draft_revision IS NOT NULL"
                )
            return dict(entries=len(docs))

    # ----------------------------------------------------------------- votes

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

    def record_vote(self, *, root_hash, ref, rating, reason, publishable):
        """One current vote per opaque root and entry; a new vote on the same
        entry replaces it (including its revision and reason)."""
        if rating not in RATINGS:
            raise ValueError("rating must be up or down")
        if not isinstance(reason, str) or len(reason) > MAX_VOTE_REASON:
            raise ValueError("reason must be text of at most 1000 characters")
        entry_id, pinned = self._parse_ref(ref)
        with self._write_txn():
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
            return dict(
                vote_id=digest(["vote", opaque, entry_id]), root_id=opaque,
                entry_id=entry_id, revision=revision, rating=rating,
                publishable=bool(publishable),
            )

    def unbatched_votes(self):
        with self.lock:
            return [
                dict(r)
                for r in self.db.execute(
                    "SELECT * FROM votes WHERE publishable=1 AND batch_id IS NULL"
                )
            ]

    # ---------------------------------------------------------------- outbox

    def create_batch(self, *, batch_id, revision, batch, entry_ids, vote_keys):
        """Record one built batch as pending and bind its material.

        Material bound to a batch is never silently re-batched: only a newer
        draft revision or a replacement vote becomes new work. Batches are
        bounded in size by construction (draft bodies are 64 KiB capped).
        """
        if len(canonical(batch).encode("utf-8")) > 4 * 1024 * 1024:
            raise ValueError("batch exceeds the storage envelope")
        now = time.time()
        with self._write_txn():
            self.db.execute(
                "INSERT INTO outbox VALUES(?,?,?,?,?,NULL,NULL,?,NULL,?)",
                (batch_id, revision, canonical(batch), "pending", "", now, now),
            )
            for entry_id in entry_ids:
                row = self._row(entry_id)
                if row is not None:
                    self.db.execute(
                        "UPDATE entries SET batched_revision=? WHERE entry_id=?",
                        (row["draft_revision"], entry_id),
                    )
            for opaque, entry_id in vote_keys:
                self.db.execute(
                    "UPDATE votes SET batch_id=? WHERE root_opaque=? AND entry_id=?",
                    (batch_id, opaque, entry_id),
                )

    def mark_batch(self, batch_id, status, *, detail="", pr_url=None, head_sha=None,
                   attempted=False):
        if status not in {"pending", *TERMINAL_BATCH}:
            raise ValueError("invalid batch status")
        with self._write_txn():
            self.db.execute(
                "UPDATE outbox SET status=?, detail=?, pr_url=COALESCE(?, pr_url), "
                "head_sha=COALESCE(?, head_sha), attempted=COALESCE(?, attempted), "
                "updated=? WHERE batch_id=?",
                (status, str(detail)[:1000], pr_url, head_sha,
                 time.time() if attempted else None, time.time(), batch_id),
            )

    def batch(self, batch_id):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM outbox WHERE batch_id=?", (batch_id,)
            ).fetchone()
        return dict(row) if row else None

    def outbox_pending(self):
        """Never-attempted batches (resumable after a forced shutdown)."""
        with self.lock:
            return [
                dict(r)
                for r in self.db.execute(
                    "SELECT * FROM outbox WHERE status='pending' AND attempted IS NULL"
                )
            ]

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

    # --------------------------------------------------------------- capture

    def add_capture(self, *, root_session, session, turn, transcript, summary):
        if not isinstance(turn, str) or not turn.strip() or len(turn) > 256:
            raise ValueError("turn_id must be nonempty text of at most 256 characters")
        if not isinstance(summary, str) or len(summary) > 32768:
            raise ValueError("summary exceeds the bounded envelope")
        ident = digest(["capture", root_session, turn.strip()])
        with self._write_txn():
            old = self.db.execute(
                "SELECT status FROM captures WHERE id=?", (ident,)
            ).fetchone()
            if old:
                return dict(id=ident, status=old[0], duplicate=True)
            self.db.execute(
                "INSERT INTO captures VALUES(?,?,?,?,?,?,?,?,?)",
                (ident, root_session, session, turn.strip(), transcript,
                 summary.strip(), "queued", "", time.time()),
            )
        return dict(id=ident, status="queued", duplicate=False)

    def capture_row(self, ident):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM captures WHERE id=?", (ident,)
            ).fetchone()
        return dict(row) if row else None

    def mark_capture(self, ident, status, detail=""):
        with self._write_txn():
            self.db.execute(
                "UPDATE captures SET status=?, detail=? WHERE id=?",
                (status, str(detail)[:1000], ident),
            )

    def cursor(self, file_identity):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM cursors WHERE file_identity=?", (file_identity,)
            ).fetchone()
        return dict(row) if row else None

    def reserve_region(self, *, capture_id, file_identity, identity, start, finish,
                       region_digest, status="attempted", detail=""):
        """Durably reserve ``(file identity, start, finish, digest)`` BEFORE any
        model call. Every outcome — success, failure, crash, cancellation —
        consumes the region; the attempted boundary moves forward and failed
        regions remain visible as coverage gaps, never silent rereads."""
        ident = digest(["region", capture_id, file_identity, start, finish, region_digest])
        now = time.time()
        with self._write_txn():
            self.db.execute(
                "INSERT OR REPLACE INTO regions VALUES(?,?,?,?,?,?,?,?,?)",
                (ident, capture_id, file_identity, start, finish, region_digest,
                 status, str(detail)[:1000], now),
            )
            self.db.execute(
                "INSERT INTO cursors VALUES(?,?,?,?,0,?) "
                "ON CONFLICT(file_identity) DO UPDATE SET "
                "identity=excluded.identity, finish=excluded.finish, "
                "digest=excluded.digest, updated=excluded.updated",
                (file_identity, identity, finish, region_digest, now),
            )
        return ident

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
                retired=self.db.execute(
                    "SELECT count(*) FROM entries WHERE status='retired'"
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
