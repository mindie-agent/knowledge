"""Markdown content and a separate use/feedback ledger, scoped to one domain.

The initial small-corpus implementation reuses the package's BM25 retrieval.
Scores measure usefulness for retrieval, never factual confidence. Snapshots
contain sanitized published entries; raw hook inputs and use evidence stay local.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from pathlib import Path

from mindie_knowledge.markdown import Document, _atomic_write_text, render_markdown
from mindie_knowledge.retrieval import lexical_search

SCHEMA = "mindie-domain/1"
MAX_TEXT = 32768
MAX_ENTRIES = 10000


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def text(value, name, limit=MAX_TEXT):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be nonempty text of at most {limit} characters")
    return value.strip()


def session_key(value):
    return digest(text(value, "session_id", 256))


def content_id(kind, title, content, source, conditions):
    # Whitespace-only repetitions do not create another experience.
    return digest(
        [kind, " ".join(title.split()), " ".join(content.split()), source, conditions]
    )


class Store:
    def __init__(self, root, domain):
        if not isinstance(domain, str) or not re.fullmatch(
            r"[a-z][a-z0-9-]{0,63}", domain
        ):
            raise ValueError("invalid domain")
        self.domain = domain
        self.root = Path(root).resolve() / domain
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "ledger.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS entries(id TEXT PRIMARY KEY, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS captures(id TEXT PRIMARY KEY, session TEXT NOT NULL,
                turn TEXT NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS uses(id TEXT PRIMARY KEY, entry_id TEXT NOT NULL,
                session TEXT NOT NULL, application TEXT NOT NULL, evidence TEXT NOT NULL,
                outcome TEXT NOT NULL, origin TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS feedback(use_id TEXT PRIMARY KEY, entry_id TEXT NOT NULL,
                consumer TEXT NOT NULL, judge TEXT NOT NULL, verdict TEXT NOT NULL,
                reason TEXT NOT NULL, evidence_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS publication(entry_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS feed_entries(feed TEXT NOT NULL, path TEXT NOT NULL,
                entry_id TEXT NOT NULL, fingerprint TEXT NOT NULL, active INTEGER NOT NULL,
                PRIMARY KEY(feed,entry_id));
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def add(self, *, kind, title, content, source=None, conditions=None, producers=()):
        if kind not in {"knowledge", "experience"}:
            raise ValueError("kind must be knowledge or experience")
        title, content = text(title, "title", 240), text(content, "content")
        source, conditions = source or {}, conditions or {}
        if not isinstance(source, dict) or not isinstance(conditions, dict):
            raise ValueError("source and conditions must be objects")
        if kind == "knowledge" and (
            not source.get("url") or not source.get("revision") or not conditions
        ):
            raise ValueError(
                "knowledge requires source.url, source.revision and applicability conditions"
            )
        if any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in producers):
            raise ValueError("producer identities must be session hashes")
        ident = content_id(kind, title, content, source, conditions)
        with self.lock:
            old = self.db.execute(
                "SELECT document FROM entries WHERE id=?", (ident,)
            ).fetchone()
            old_doc = json.loads(old[0]) if old else {}
            # Repeating an experience after consuming it does not turn its
            # consumer into an independent producer or invalidate its use vote.
            consumers = {
                r[0]
                for r in self.db.execute(
                    "SELECT session FROM uses WHERE entry_id=?", (ident,)
                )
            }
            doc = dict(
                id=ident,
                kind=kind,
                title=title,
                content=content,
                source=source,
                conditions=conditions,
                producers=sorted(
                    (set(producers) - consumers) | set(old_doc.get("producers", []))
                ),
            )
            _atomic_write_text(
                self.root / "content" / kind / f"{ident}.md",
                render_markdown(title, content),
            )
            _atomic_write_text(
                self.root / "content" / kind / f"{ident}.meta.json",
                canonical(doc) + "\n",
            )
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO entries VALUES(?,?)",
                    (ident, canonical(doc)),
                )
            return doc

    def get(self, ref):
        prefix = f"mindie://{self.domain}/"
        ident = (
            ref[len(prefix) :]
            if isinstance(ref, str) and ref.startswith(prefix)
            else ref
        )
        if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{64}", ident):
            raise ValueError("reference is outside the selected domain")
        with self.lock:
            row = self.db.execute(
                "SELECT document FROM entries WHERE id=?", (ident,)
            ).fetchone()
        if row is None:
            raise ValueError("unknown reference in this domain")
        return json.loads(row[0])

    def ref(self, ident):
        return f"mindie://{self.domain}/{ident}"

    def weight(self, ident):
        with self.lock:
            rows = self.db.execute(
                "SELECT verdict FROM feedback WHERE entry_id=?", (ident,)
            ).fetchall()
        positive = sum(row[0] == "helpful" for row in rows)
        negative = sum(row[0] == "unhelpful" for row in rows)
        # A bounded first-release policy, explicitly not a confidence estimate.
        multiplier = max(
            0.1, min(3.0, math.exp(max(-3.0, min(2.0, 0.35 * (positive - negative)))))
        )
        return dict(
            helpful=positive,
            unhelpful=negative,
            unknown=sum(r[0] == "unknown" for r in rows),
            multiplier=round(multiplier, 6),
            withdrawn=negative >= 3 and negative >= positive + 3,
        )

    def query(self, query, limit=5, conditions=None, session_id=None):
        query = text(query, "query", 2000)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        if conditions is not None and not isinstance(conditions, dict):
            raise ValueError("conditions must be an object")
        with self.lock:
            if session_id is not None:
                with self.db:
                    self.db.execute(
                        "INSERT OR IGNORE INTO sessions VALUES(?)",
                        (session_key(session_id),),
                    )
            rows = self.db.execute(
                "SELECT document FROM entries WHERE NOT EXISTS "
                "(SELECT 1 FROM feed_entries WHERE entry_id=entries.id AND active=0) "
                "OR EXISTS (SELECT 1 FROM feed_entries WHERE entry_id=entries.id AND active=1) "
                "LIMIT ?",
                (MAX_ENTRIES + 1,),
            ).fetchall()
            if len(rows) > MAX_ENTRIES:
                raise ValueError(
                    "domain exceeds the initial lexical capacity; split it or configure a larger index"
                )
            docs = [json.loads(row[0]) for row in rows]
            selected = {}
            documents = []
            for doc in docs:
                if (
                    doc["kind"] == "knowledge"
                    and conditions
                    and any(
                        k in doc["conditions"] and doc["conditions"][k] != v
                        for k, v in conditions.items()
                    )
                ):
                    continue
                weight = (
                    self.weight(doc["id"])
                    if doc["kind"] == "experience"
                    else dict(multiplier=1.0, withdrawn=False)
                )
                if weight["withdrawn"]:
                    continue
                selected[doc["id"]] = (doc, weight)
                documents.append(
                    Document(
                        layer=doc["kind"],
                        title=doc["title"],
                        content=doc["content"],
                        slug=doc["id"],
                        path=self.root / "content" / doc["kind"] / f"{doc['id']}.md",
                        uri=doc["id"],
                    )
                )
            hits = lexical_search(query, documents, limit=len(documents))
            output = []
            for hit in hits:
                doc, weight = selected[hit.uri]
                output.append(
                    dict(
                        ref=self.ref(doc["id"]),
                        kind=doc["kind"],
                        title=doc["title"],
                        excerpt=doc["content"][:600],
                        source=doc["source"],
                        conditions=doc["conditions"],
                        score=round(hit.score * weight["multiplier"], 8),
                        usefulness=weight,
                    )
                )
            output.sort(key=lambda row: (-row["score"], row["ref"]))
            return dict(
                domain=self.domain,
                retrieval="bm25_with_use_effect",
                results=output[:limit],
                note="Reference material. Scores indicate retrieval usefulness, not factual confidence.",
            )

    def capture(self, session_id, turn_id, summary):
        session = session_key(session_id)
        turn_id, summary = text(turn_id, "turn_id", 256), text(summary, "summary")
        ident = digest([session, turn_id])
        with self.lock, self.db:
            old = self.db.execute(
                "SELECT * FROM captures WHERE id=?", (ident,)
            ).fetchone()
            if old:
                return dict(id=ident, status=old["status"], duplicate=True)
            self.db.execute(
                "INSERT INTO captures VALUES(?,?,?,?,?,?)",
                (ident, session, turn_id, summary, "queued", ""),
            )
            self.db.execute(
                "UPDATE uses SET outcome=? WHERE session=? AND outcome=''",
                (summary, session),
            )
        return dict(id=ident, status="queued", duplicate=False)

    def attach(self, session_id):
        """Explicitly bind a session to this domain.

        Domain binding is its own operation: the adapter calls this when the
        user explicitly activates the plugin for a task, so a task that only
        uses remote tools and never queries knowledge is still bound and its
        Stop summary is eligible for bounded capture. Querying or recording a
        use also binds (a real knowledge interaction is itself an explicit
        choice), but binding never requires a query.
        """
        session = session_key(session_id)
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO sessions VALUES(?)", (session,))
        return dict(attached=True, domain=self.domain)

    def attached(self, session_id):
        with self.lock:
            return (
                self.db.execute(
                    "SELECT id FROM sessions WHERE id=?", (session_key(session_id),)
                ).fetchone()
                is not None
            )

    def mark_capture(self, ident, status, detail=""):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE captures SET status=?,detail=? WHERE id=?",
                (status, detail[:1000], ident),
            )

    def use(self, *, ref, session_id, application, evidence):
        doc = self.get(ref)
        if doc["kind"] != "experience":
            raise ValueError("use-effect feedback applies to experience")
        session = session_key(session_id)
        application, evidence = (
            text(application, "application", 4000),
            text(evidence, "evidence", 8000),
        )
        ident = digest([doc["id"], session])
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO sessions VALUES(?)", (session,))
            self.db.execute(
                "INSERT OR IGNORE INTO uses VALUES(?,?,?,?,?,?,?)",
                (ident, doc["id"], session, application, evidence, "", "local"),
            )
            # Multi-round semantics: one use record per (entry, session).
            # Later rounds of the same session refine the same record instead
            # of creating new votes, so consumption echoes never add weight.
            # Refinement is accepted only while the use is unjudged; once an
            # independent verdict exists it stays bound to the evidence hash it
            # evaluated, and the record no longer mutates underneath it.
            judged = self.db.execute(
                "SELECT 1 FROM feedback WHERE use_id=?", (ident,)
            ).fetchone()
            if not judged:
                self.db.execute(
                    "UPDATE uses SET application=?,evidence=? WHERE id=?",
                    (application, evidence, ident),
                )
        return dict(
            use_id=ident,
            eligible=session not in doc["producers"],
            refined=not judged,
            note="Stop capture supplies this session's outcome; the independent judge evaluates asynchronously.",
        )

    def pending_uses(self):
        with self.lock:
            return [
                dict(r)
                for r in self.db.execute(
                    "SELECT uses.* FROM uses LEFT JOIN feedback ON uses.id=feedback.use_id "
                    "WHERE feedback.use_id IS NULL AND uses.outcome!='' "
                    "AND NOT EXISTS (SELECT 1 FROM state WHERE key='judge_failed:' || uses.id)"
                ).fetchall()
            ]

    def failed_judge(self, use_id, detail):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO state VALUES(?,?)",
                ("judge_failed:" + use_id, detail[:1000]),
            )

    def upstream_entry(self, ident):
        with self.lock:
            return (
                self.db.execute(
                    "SELECT 1 FROM state WHERE key=?", ("upstream_entry:" + ident,)
                ).fetchone()
                is not None
            )

    def receive_use(self, usage):
        required = {
            "id",
            "entry_id",
            "session",
            "application",
            "evidence",
            "outcome",
            "origin",
        }
        if set(usage) != required:
            raise ValueError("invalid use record")
        doc = self.get(usage["entry_id"])
        if doc["kind"] != "experience" or not re.fullmatch(
            r"[0-9a-f]{64}", usage["session"]
        ):
            raise ValueError("invalid experience use")
        if usage["id"] != digest([doc["id"], usage["session"]]):
            raise ValueError("use identity mismatch")
        for name in ("application", "evidence", "outcome"):
            text(usage[name], name)
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO uses VALUES(?,?,?,?,?,?,?)",
                tuple(
                    usage[k]
                    for k in (
                        "id",
                        "entry_id",
                        "session",
                        "application",
                        "evidence",
                        "outcome",
                    )
                )
                + ("upstream",),
            )
        return usage["id"]

    def judge(self, use_id, *, judge_id, verdict, reason):
        if verdict not in {"helpful", "unhelpful", "unknown"}:
            raise ValueError("invalid verdict")
        judge_id, reason = text(judge_id, "judge_id", 256), text(reason, "reason", 4000)
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM uses WHERE id=?", (use_id,)).fetchone()
            if row is None or not row["outcome"]:
                raise ValueError("judge requires a completed observed use")
            doc = self.get(row["entry_id"])
            if row["session"] in doc["producers"]:
                raise ValueError("producer's own use is not independent feedback")
            if session_key(judge_id) in set(doc["producers"]) | {row["session"]}:
                raise ValueError("judge must be independent of producer and consumer")
            record = dict(
                use_id=use_id,
                entry_id=doc["id"],
                consumer=row["session"],
                judge=session_key(judge_id),
                verdict=verdict,
                reason=reason,
                evidence_hash=digest(
                    [row["application"], row["evidence"], row["outcome"]]
                ),
            )
            existing = self.db.execute(
                "SELECT * FROM feedback WHERE use_id=?", (use_id,)
            ).fetchone()
            if existing:
                return dict(existing)
            self.db.execute(
                "INSERT INTO feedback VALUES(?,?,?,?,?,?,?)", tuple(record.values())
            )
            return record

    def publish(self, ident):
        # Publication is an explicit operator/config choice. The rendered
        # content and all metadata must pass the source-side redaction
        # ruleset as-is; an entry that needs rewriting is published only as a
        # separately reviewed sanitized copy added under its own identity.
        from mindie_knowledge.redact import scan_text

        doc = self.get(ident)
        findings = scan_text(render_markdown(doc["title"], doc["content"]))
        findings += scan_text(canonical(doc["source"]))
        findings += scan_text(canonical(doc["conditions"]))
        if findings:
            rules = ", ".join(sorted({finding.rule for finding in findings}))
            raise ValueError(f"entry requires sanitization before publication: {rules}")
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO publication VALUES(?)", (doc["id"],))

    def withdraw(self, ident):
        """Remove an entry from the authorized publication set.

        The local content and its use/feedback history are retained; only the
        authorization to include the entry in future snapshots is revoked, so
        downstream installs stop offering it after their next sync while old
        references remain explainable.
        """
        doc = self.get(ident)
        with self.lock, self.db:
            self.db.execute("DELETE FROM publication WHERE entry_id=?", (doc["id"],))
        return dict(withdrawn=True, ref=self.ref(doc["id"]))

    def snapshot(self):
        with self.lock:
            entries = [
                json.loads(r[0])
                for r in self.db.execute(
                    "SELECT document FROM entries JOIN publication ON entries.id=publication.entry_id ORDER BY entries.id"
                )
            ]
            # Evidence and judge prose remain private. Export only value signals.
            feedback = [
                dict(r)
                for r in self.db.execute(
                    "SELECT use_id,feedback.entry_id,consumer,judge,verdict,evidence_hash FROM feedback JOIN publication ON feedback.entry_id=publication.entry_id ORDER BY use_id"
                )
            ]
        payload = dict(
            schema=SCHEMA, domain=self.domain, entries=entries, feedback=feedback
        )
        return dict(version=digest(payload), **payload)

    def install_snapshot(self, snapshot):
        if not isinstance(snapshot, dict) or set(snapshot) != {
            "version",
            "schema",
            "domain",
            "entries",
            "feedback",
        }:
            raise ValueError("invalid domain snapshot")
        payload = {k: v for k, v in snapshot.items() if k != "version"}
        if (
            snapshot["schema"] != SCHEMA
            or snapshot["domain"] != self.domain
            or digest(payload) != snapshot["version"]
        ):
            raise ValueError("snapshot schema, domain or digest mismatch")
        entries, feedback = snapshot["entries"], snapshot["feedback"]
        if (
            not isinstance(entries, list)
            or len(entries) > MAX_ENTRIES
            or not isinstance(feedback, list)
        ):
            raise ValueError("invalid snapshot size")
        by_id = {}
        for doc in entries:
            if set(doc) != {
                "id",
                "kind",
                "title",
                "content",
                "source",
                "conditions",
                "producers",
            }:
                raise ValueError("invalid published entry")
            if (
                doc["kind"] not in {"knowledge", "experience"}
                or not isinstance(doc["source"], dict)
                or not isinstance(doc["conditions"], dict)
            ):
                raise ValueError("invalid entry metadata")
            text(doc["title"], "title", 240)
            text(doc["content"], "content")
            if doc["id"] != content_id(
                doc["kind"],
                doc["title"],
                doc["content"],
                doc["source"],
                doc["conditions"],
            ):
                raise ValueError("content digest mismatch")
            if (
                doc["id"] in by_id
                or not isinstance(doc["producers"], list)
                or any(
                    not isinstance(p, str) or not re.fullmatch(r"[0-9a-f]{64}", p)
                    for p in doc["producers"]
                )
            ):
                raise ValueError("invalid published identities")
            if doc["kind"] == "knowledge" and (
                not doc["source"].get("url")
                or not doc["source"].get("revision")
                or not doc["conditions"]
            ):
                raise ValueError("knowledge lacks source applicability")
            by_id[doc["id"]] = doc
        seen = set()
        for vote in feedback:
            if set(vote) != {
                "use_id",
                "entry_id",
                "consumer",
                "judge",
                "verdict",
                "evidence_hash",
            }:
                raise ValueError("invalid published feedback")
            doc = by_id.get(vote["entry_id"])
            if (
                not doc
                or doc["kind"] != "experience"
                or vote["verdict"] not in {"helpful", "unhelpful", "unknown"}
            ):
                raise ValueError("feedback has no matching experience")
            if any(
                not isinstance(vote[k], str)
                or not re.fullmatch(r"[0-9a-f]{64}", vote[k])
                for k in ("use_id", "consumer", "judge", "evidence_hash")
            ):
                raise ValueError("invalid feedback identity")
            if vote["consumer"] in doc["producers"] or vote["judge"] in doc[
                "producers"
            ] + [vote["consumer"]]:
                raise ValueError("non-independent feedback")
            if (
                vote["use_id"] != digest([doc["id"], vote["consumer"]])
                or vote["use_id"] in seen
            ):
                raise ValueError("duplicate or inconsistent feedback identity")
            seen.add(vote["use_id"])
        # Validate everything before making any content visible.
        with self.lock:
            for doc in entries:
                self.add(**{k: v for k, v in doc.items() if k != "id"})
            with self.db:
                for doc in entries:
                    self.db.execute(
                        "INSERT OR REPLACE INTO state VALUES(?,?)",
                        ("upstream_entry:" + doc["id"], snapshot["version"]),
                    )
                for vote in feedback:
                    # A configured authority owns the distributed verdict.
                    self.db.execute(
                        "INSERT OR REPLACE INTO feedback VALUES(?,?,?,?,?,?,?)",
                        (
                            vote["use_id"],
                            vote["entry_id"],
                            vote["consumer"],
                            vote["judge"],
                            vote["verdict"],
                            "Published independent-use feedback; raw evidence remains at its origin.",
                            vote["evidence_hash"],
                        ),
                    )
                self.db.execute(
                    "INSERT OR REPLACE INTO state VALUES('upstream_version',?)",
                    (snapshot["version"],),
                )
        return dict(
            version=snapshot["version"], entries=len(entries), feedback=len(feedback)
        )

    def status(self):
        with self.lock:
            return dict(
                domain=self.domain,
                entries=self.db.execute("SELECT count(*) FROM entries").fetchone()[0],
                captures=[
                    dict(r)
                    for r in self.db.execute(
                        "SELECT id,status,detail FROM captures ORDER BY rowid DESC LIMIT 20"
                    )
                ],
                uses=self.db.execute("SELECT count(*) FROM uses").fetchone()[0],
                feedback=[
                    dict(r)
                    for r in self.db.execute(
                        "SELECT use_id,entry_id,verdict,reason FROM feedback"
                    )
                ],
                failed_judges=[
                    dict(r)
                    for r in self.db.execute(
                        "SELECT key,value FROM state WHERE key LIKE 'judge_failed:%'"
                    )
                ],
                version=self.snapshot()["version"],
            )
