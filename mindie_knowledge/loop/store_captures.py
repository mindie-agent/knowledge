"""[Role] Material, vote and feed mutation mixin for the authoritative Store.

[Invariants]
- Every mutation runs inside ``Store._write_txn``; package bodies are staged
  before SQLite metadata and promoted only by the owning Store.
- Quarantined entries stay local/readable but never re-enter an automatic batch;
  vote receipts retain their exact revision and grant generation.

[Timeouts & Locks]
- Callers hold the Store RLock and SQLite transaction; this module adds no
  subprocess, network or filesystem lock.

[Side-Effect Policy]
- Mutates the shared SQLite authority and marks material rows dirty for the
  owner's single promotion pass.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from . import documents
from ..materials.references import parse_feedback_ref
from .store_schema import (
    MAX_VOTE_REASON,
    MISSING_MATERIAL_JOB_DETAIL,
    SUMMARY_STATUS_SQL,
    _exact_revision_ref,
    _stored_json,
    RATINGS,
    REPLACEABLE_BATCH,
    TERMINAL_BATCH,
    canonical,
    digest,
    new_identity,
    session_key,
)


class MaterialMixin:

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
    def record_vote(self, *, root_hash, ref, rating=None, reason="", publishable=False,
                    generation=None, event_type=None, run_id="", outcome=None,
                    step_id="", evidence_source="agent_declared"):
        """One current vote per opaque root and entry; a new vote on the same
        entry replaces it (including its revision and reason). A publishable
        vote's free-text reason is privacy-scanned at admission, so a single
        unsafe reason can never block unrelated material at batch time; local
        (non-publishable) votes are never scanned and never leave the store.
        Graded usage events are idempotent and may accompany a vote."""
        if isinstance(ref, str) and "/units/" in ref:
            from ..graded.feedback import record_vote
            return record_vote(self, root_hash=root_hash, ref=ref, rating=rating, reason=reason,
                               publishable=publishable, generation=generation,
                               event_type=event_type, run_id=run_id, outcome=outcome,
                               step_id=step_id, evidence_source=evidence_source)
        if event_type is not None or run_id or outcome is not None or step_id:
            raise ValueError("usage events require a graded unit reference")
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
        rows = self._unbatched_votes(generation=generation)
        public = self.graded.public_ids() if self.graded.available() else set()
        return [row for row in rows if not row["entry_id"].startswith("unit-") or row["entry_id"] in public]
    def _unbatched_votes(self, *, generation=None):
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
    def contribution_base_files(self, entry_id, *, target=None):
        """Exact confirmed base used by the current append-only candidate."""
        with self.lock:
            if target:
                feeds = self.material_feeds()
                members = feeds.get(target["feed_ident"], {}).get("entries", {})
                if entry_id not in members and any(entry_id in source["entries"] for source in feeds.values()):
                    raise ValueError("task correction belongs to a different publication repository")
                receipt = self.feed_get("sent-package:" + entry_id) or {}
                sent_target = receipt.get("publication_target")
                if not sent_target or sent_target["feed_ident"] != target["feed_ident"]:
                    if entry_id not in members:
                        return []
                    package = self.materials.export_task(entry_id, source="feed", revision=members[entry_id])
                    return [dict(path=f"tasks/{entry_id}/{path}", sha256=hashlib.sha256(content.encode()).hexdigest())
                            for path, content in package["files"].items()]
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
