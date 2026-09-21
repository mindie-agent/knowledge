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
MAX_ENTRIES = 10000
MAX_VOTE_REASON = 1000
RATINGS = ("up", "down")
REPLACEABLE_BATCH = frozenset({"submitted", "updated", "unchanged", "needs_review", "failed"})

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
                doc TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS revisions(entry_id TEXT NOT NULL,
                revision TEXT NOT NULL, doc TEXT NOT NULL, source TEXT NOT NULL,
                created REAL NOT NULL, PRIMARY KEY(entry_id, revision));
            CREATE TABLE IF NOT EXISTS captures(id TEXT PRIMARY KEY,
                root_session TEXT NOT NULL, session TEXT NOT NULL, turn TEXT NOT NULL,
                transcript TEXT, summary TEXT NOT NULL, status TEXT NOT NULL,
                detail TEXT NOT NULL, created REAL NOT NULL,
                generation TEXT, boundary REAL, scope TEXT);
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
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,
                value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS continuations(capture_id TEXT PRIMARY KEY,
                due REAL NOT NULL, reason TEXT NOT NULL);
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
        """)
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

    def explain(self, ref, *, offset=0, limit=None):
        doc = self.get(ref)
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 65536):
            raise ValueError("limit must be between 1 and 65536 characters")
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
                "INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?)",
                (entry_id, kind, doc["title"], origin, doc["revision"],
                 None, 0, None, canonical(doc), now),
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
        """
        now = time.time()
        with self._write_txn():
            row = self._row(entry_id)
            if row is None:
                raise ValueError("unknown draft in this domain")
            if not row["draft_revision"]:
                raise ValueError("entry has no local draft to update")
            if generation is not None and not self.granted(
                "draft", entry_id, row["draft_revision"], generation
            ):
                raise ValueError(
                    "draft material belongs to another sharing generation; "
                    "it stays local instead of being republished"
                )
            base = self._revision_doc(entry_id, row["draft_revision"])
            if producer is not None and producer != self._owner_of(entry_id):
                raise ValueError("only the owning task may append its draft")
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
            self._insert_revision(doc, row["origin"], now)
            if generation is not None:
                self.db.execute(
                    "INSERT OR REPLACE INTO grants VALUES(?,?,?,?,?)",
                    ("draft", entry_id, doc["revision"], generation, now),
                )
            visible = not row["feed_active"]
            self.db.execute(
                "UPDATE entries SET draft_revision=?, title=?, doc=?, updated=? "
                "WHERE entry_id=?",
                (doc["revision"], doc["title"],
                 canonical(doc) if visible else row["doc"], now, entry_id),
            )
            self._write_draft_file(doc)
            return doc, True

    _NOT_WITHDRAWN = (
        "NOT (entries.published_revision IS NOT NULL AND entries.feed_active=0)"
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
        longer carries is never a new publication."""
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
                    f"AND {self._NOT_WITHDRAWN}",
                    (generation,),
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT entry_id, draft_revision FROM entries "
                    "WHERE draft_revision IS NOT NULL "
                    "AND draft_revision != COALESCE(batched_revision, '') "
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
        """BM25 over visible entries: published versions win;
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
                "SELECT * FROM entries WHERE (feed_active=1 "
                "OR (draft_revision IS NOT NULL AND published_revision IS NULL)) "
                "LIMIT ?", (MAX_ENTRIES + 1,),
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
                    ref=self._short_ref(doc["entry_id"], doc["revision"]),
                    kind=doc["kind"], title=doc["title"], summary=doc["summary"],
                    conditions=doc["conditions"],
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
                        "INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (doc["entry_id"], doc["kind"], doc["title"],
                         "feed", None, doc["revision"], 1, None,
                         canonical(doc), now),
                    )
                else:
                    self.db.execute(
                        "UPDATE entries SET kind=?, title=?, "
                        "published_revision=?, feed_active=1, doc=?, updated=? "
                        "WHERE entry_id=?",
                        (doc["kind"], doc["title"], doc["revision"],
                         canonical(doc), now, doc["entry_id"]),
                    )
            # Membership is authoritative for every entry, however it first
            # appeared locally. Once an entry has a published revision its
            # visibility is governed by the feed alone: leaving the tree (or a
            # valid empty tree) removes it from search, and its stale draft
            # copy is never resurrected. All bodies stay readable by pinned
            # reference.
            if seen:
                self.db.execute(
                    "UPDATE entries SET feed_active=0 WHERE feed_active=1 "
                    f"AND entry_id NOT IN ({','.join('?' for _ in seen)})",
                    tuple(seen),
                )
            else:
                self.db.execute("UPDATE entries SET feed_active=0 WHERE feed_active=1")
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

    def record_vote(self, *, root_hash, ref, rating, reason, publishable,
                    generation=None):
        """One current vote per opaque root and entry; a new vote on the same
        entry replaces it (including its revision and reason)."""
        if rating not in RATINGS:
            raise ValueError("rating must be up or down")
        if not isinstance(reason, str) or len(reason) > MAX_VOTE_REASON:
            raise ValueError("reason must be text of at most 1000 characters")
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
        Votes on an entry the feed no longer carries stay local."""
        withdrawn = (
            "NOT EXISTS (SELECT 1 FROM entries e WHERE e.entry_id=votes.entry_id "
            "AND e.published_revision IS NOT NULL AND e.feed_active=0)"
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
                        f"AND {withdrawn}",
                        (generation,),
                    )
                ]
            return [
                dict(r)
                for r in self.db.execute(
                    "SELECT * FROM votes WHERE publishable=1 AND batch_id IS NULL "
                    f"AND {withdrawn}"
                )
            ]

    # ---------------------------------------------------------------- outbox

    def reserve_export(self, fingerprint):
        """Consume this material revision before final scanning/staging.

        A scan failure or interrupted build must not be repeated every idle
        tick. New draft content or an explicit new vote gets a new fingerprint.
        """
        with self._write_txn():
            return self.db.execute(
                "INSERT OR IGNORE INTO state VALUES(?,?)",
                ("export:" + fingerprint, canonical({"status": "attempted"})),
            ).rowcount == 1

    def finish_export(self, fingerprint, status, detail=""):
        with self._write_txn():
            self.db.execute(
                "UPDATE state SET value=? WHERE key=?",
                (canonical({"status": status, "detail": str(detail)[:500]}),
                 "export:" + fingerprint),
            )

    def create_batch(self, *, batch_id, revision, batch, entry_ids, vote_keys,
                     generation=None):
        """Record one built batch as pending and bind its material.

        Material bound to a batch is never silently re-batched: only a newer
        draft revision or a replacement vote becomes new work. Batches are
        bounded in size by construction (draft bodies are 64 KiB capped).
        """
        if len(canonical(batch).encode("utf-8")) > 4 * 1024 * 1024:
            raise ValueError("batch exceeds the storage envelope")
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
            if status in self.CONFIRMED_BATCH:
                row = self.db.execute(
                    "SELECT * FROM outbox WHERE batch_id=?", (batch_id,)
                ).fetchone()
                if row is not None:
                    try:
                        batch = json.loads(row["batch"])
                    except ValueError:
                        batch = {}
                    self._record_sent_receipts(row, batch)

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

    def reconcile_due(self, batch_id, *, limit=5):
        """Durable finite reconciliation: bounded count and exponential
        backoff persisted across restarts. True when another bounded read-only
        reconciliation is allowed now."""
        now = time.time()
        with self._write_txn():
            row = self.db.execute(
                "SELECT reconciliations, next_attempt FROM outbox WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if row is None or row["reconciliations"] >= limit:
                return False
            if row["next_attempt"] is not None and now < row["next_attempt"]:
                return False
            count = row["reconciliations"] + 1
            self.db.execute(
                "UPDATE outbox SET reconciliations=?, next_attempt=? "
                "WHERE batch_id=?",
                (count, now + min(60.0, 2.0 ** count), batch_id),
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
                        "UPDATE entries SET draft_revision=NULL, doc=?, updated=? "
                        "WHERE entry_id=?",
                        (canonical(header), time.time(), entry_id),
                    )
                    removed["entries"] += 1
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

    def _record_sent_receipts(self, row, batch):
        """Tiny per-entry last-confirmed receipt, independent of the newest
        lineage outbox row. No body/history."""
        head = row["head_sha"] if isinstance(row, sqlite3.Row) else row.get("head_sha")
        if not isinstance(head, str) or not head:
            return
        generation = row["generation"] if isinstance(row, sqlite3.Row) else row.get("generation")
        pr_url = row["pr_url"] if isinstance(row, sqlite3.Row) else row.get("pr_url")
        batch_id = row["batch_id"] if isinstance(row, sqlite3.Row) else row.get("batch_id")
        files = {
            f.get("path"): f
            for f in batch.get("files", [])
            if isinstance(f, dict) and isinstance(f.get("path"), str)
        }
        now = time.time()
        for ref in batch.get("entry_refs", []):
            parsed = _exact_revision_ref(ref)
            if parsed is None:
                continue
            entry_id, sent_revision = parsed
            path = next(
                (p for p in (f"cases/{entry_id}.md", f"topics/{entry_id}.md") if p in files),
                None,
            )
            if not entry_id or not sent_revision or not path:
                continue
            sha = files[path].get("sha256")
            if not isinstance(sha, str) or not sha:
                continue
            self.db.execute(
                "INSERT OR REPLACE INTO sent_receipts "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    entry_id, generation, sent_revision, path, sha, head,
                    None, pr_url, batch_id, now,
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

    def restore_draft(self, entry_id, doc, *, generation=None):
        """Re-seed a compacted append-base from the exact confirmed remote
        body (fetched boundedly by the caller). Never overwrites an existing
        local draft, never resurrects a withdrawn entry and never fabricates
        a base from only the new paragraph."""
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
            documents.validate(doc)
            self._insert_revision(doc, "draft", now)
            if generation is not None:
                self.db.execute(
                    "INSERT OR REPLACE INTO grants VALUES(?,?,?,?,?)",
                    ("draft", entry_id, doc["revision"], generation, now),
                )
            visible = not row["feed_active"]
            self.db.execute(
                "UPDATE entries SET draft_revision=?, title=?, doc=?, updated=? "
                "WHERE entry_id=?",
                (doc["revision"], doc["title"],
                 canonical(doc) if visible else row["doc"], now, entry_id),
            )
            self._write_draft_file(doc)
            return doc

    # --------------------------------------------------------------- capture

    def add_capture(self, *, root_session, session, turn, transcript, summary,
                    generation=None, boundary=None, scope=None):
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
                "INSERT INTO captures VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (ident, root_session, session, turn.strip(), transcript,
                 summary.strip(), "queued", "", time.time(),
                 generation, boundary, scope),
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
            if status not in {"queued", "pending", "deferred"}:
                self.db.execute("DELETE FROM continuations WHERE capture_id=?", (ident,))

    def defer_capture(self, ident, *, due, reason):
        with self._write_txn():
            self.db.execute("UPDATE captures SET status='pending', detail=? WHERE id=?",
                            (reason[:1000], ident))
            self.db.execute("INSERT OR REPLACE INTO continuations VALUES(?,?,?)",
                            (ident, due, reason[:1000]))

    def due_capture(self):
        with self.lock:
            row = self.db.execute(
                "SELECT c.id FROM captures c LEFT JOIN continuations q ON q.capture_id=c.id "
                "WHERE c.status IN ('queued','pending','deferred') "
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
