"""[Role] Freeze authorized C5 candidates into the existing contribution outbox.

[Invariants] Only the original sharing generation can export a candidate.
Local import is not consent. Frozen files bind semantic revisions and exact Git
base hashes; publication continues through the existing unknown/reconcile ledger.
[Timeouts & Locks] Uses the Store WAL transaction; no subprocess or model calls.
[Side-Effect Policy] Writes local frozen candidate files and existing outbox,
grants and receipt metadata. No Git push, PR write or corpus deletion here.
"""
from __future__ import annotations

import json
from pathlib import PurePosixPath
import time

from ..community.batch import batch_revision, validate_batch
from ..community.common import sha256_text
from ..materials.publication import freeze_batch, load_batch_payload
from ..loop.publication_target import publication_target, receipt_key
from .contribution import DERIVED_PATHS, is_unit_path, scan_unit
from .feedback import outbound_votes, render_feedback
from .index import RoutingIndex
from .maintenance import _payloads


def _files(store, descriptors, generation, target):
    files = []
    feed = store.graded._catalog()["feeds"].get(target["feed_ident"], {})
    public = {item["id"]: item for item in feed.get("units", [])}
    for item in descriptors:
        unit = store.graded._read(item)
        path = item["path"]
        text = unit.render()
        scan_unit(text, path)
        base = item.get("base")
        current = public.get(unit.id)
        if base and base.get("feed_ident") != target["feed_ident"]:
            if current and (base["content_sha256"], base["path"]) == (current["content_sha256"], current["path"]):
                base = current
            else:
                raise ValueError("graded correction belongs to a different publication repository")
        prior = store.feed_get(receipt_key("sent-graded", target, unit.id))
        current_hash = current["content_sha256"] if current else None
        if (prior and prior.get("generation") == generation
                and prior.get("source_published_revision") == current_hash):
            base = prior
        files.append(dict(path=path, content=text, sha256=sha256_text(text),
                          base_sha256=base.get("file_sha256") if base and base["path"] == path else None))
        if base and base["path"] != path:
            files.append(dict(path=base["path"], content=None, sha256=None,
                              base_sha256=base["file_sha256"], delete=True))
    if descriptors:
        projected = {ident: store.graded._read(item) for ident, item in public.items()}
        projected.update({item["id"]: store.graded._read(item) for item in descriptors})
        index = RoutingIndex(projected.values())
        manifest, topology = _payloads(list(projected.values()), index)
        feed_files = dict(feed.get("files", {}))
        prior = store.feed_get(receipt_key("sent-graded-derived", target))
        if prior and prior.get("generation") == generation and prior.get("source_commit") == target["source_commit"]:
            feed_files.update(prior["files"])
        for path, payload in zip(sorted(DERIVED_PATHS), [manifest, topology]):
            # Timing is diagnostic data and cannot create spurious Git deltas.
            for key in ("build_elapsed_ms", "index_build_elapsed_ms", "build_ms"):
                payload.pop(key, None)
            text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
            files.append(dict(path=path, content=text, sha256=sha256_text(text), base_sha256=feed_files.get(path)))
    return files


def build_batch(store, *, settings, revision_fn=None):
    from ..loop.export import lineage_of
    from ..loop.store import REPLACEABLE_BATCH
    generation = settings.generation
    target = publication_target(store, settings)
    lineage = lineage_of(store.domain + ":graded:" + target["feed_ident"], generation)
    previous = store.batch(lineage)
    if previous is not None and previous["status"] not in REPLACEABLE_BATCH:
        return None
    with store._write_txn():
        descriptors = store.graded.pending(generation)
        target_units = {item["id"] for item in store.graded._catalog()["feeds"].get(target["feed_ident"], {}).get("units", [])}
        votes = [row for row in store.unbatched_votes(generation=generation) if row["entry_id"] in target_units][:500]
        if not descriptors and not votes:
            return None
        files = _files(store, descriptors, generation, target)
        if votes:
            text = render_feedback(outbound_votes(store, votes))
            files.append(dict(path=f"feedback/votes-{lineage[6:]}.json", content=text,
                              sha256=sha256_text(text), base_sha256=None))
        refs = sorted({store.graded.ref(item["id"], item["revision"]) for item in descriptors}
                      | {store.graded.ref(vote["entry_id"], vote["revision"]) for vote in votes})
        base_commit = target["source_commit"]
        revision = (revision_fn or batch_revision)(files, store.domain, base_commit, refs)
        batch = dict(schema="mindie-contribution/1", batch_id=lineage, revision=revision,
                     domain=store.domain, base_commit=base_commit, entry_refs=refs, files=files,
                     summary=f"{len(descriptors)} graded units, {len(votes)} observed-revision votes")
        validate_batch(batch)
        descriptor = freeze_batch(store.root, batch)
        store.create_batch(batch_id=lineage, revision=revision, batch=descriptor,
                           entry_ids=[item["id"] for item in descriptors], generation=generation,
                           entry_revisions={item["id"]: item["content_sha256"] for item in descriptors},
                           source_revisions={item["id"]: (item.get("base") or {}).get("content_sha256")
                                             for item in descriptors},
                           vote_keys=[(vote["root_opaque"], vote["entry_id"], vote["revision"]) for vote in votes],
                           publication_target=target)
        store.graded.mark_batched(descriptors)
    return lineage, revision, load_batch_payload(store.root, descriptor), [item["id"] for item in descriptors], [
        (vote["root_opaque"], vote["entry_id"], vote["revision"]) for vote in votes]


def record_sent_receipts(store, row, batch, actual_files):
    """Persist actual committed identities in the existing receipt tables."""
    actual = {item["path"]: item for item in actual_files or []}
    prepared = store.feed_get("prepared-package-bases:" + row["batch_id"])
    units = [item for item in batch["files"] if is_unit_path(item["path"]) and not item.get("delete")]
    if units and (not prepared or prepared.get("batch_revision") != row["revision"]):
        raise ValueError("confirmed graded contribution lacks its frozen source identities")
    target = (prepared or {}).get("publication_target")
    if units and not target:
        raise ValueError("confirmed graded contribution lacks its frozen publication target")
    for candidate in units:
        path = candidate["path"]
        ident = PurePosixPath(path).stem
        proven = actual.get(path)
        expected = prepared["entry_revisions"][ident]
        if not proven or proven.get("sha256") != candidate["sha256"] or proven.get("revision") != expected:
            raise ValueError("confirmed graded contribution lacks its actual committed unit receipt")
        receipt = dict(path=path, file_sha256=proven["sha256"], content_sha256=expected,
                       generation=row["generation"], head_sha=row["head_sha"], batch_id=row["batch_id"],
                       source_published_revision=prepared["source_revisions"][ident])
        store.feed_set(receipt_key("sent-graded", target, ident), receipt)
        store.db.execute(
            "INSERT OR REPLACE INTO sent_receipts(entry_id,generation,sent_revision,path,sha256,"
            "head_sha,repository,pr_url,batch_id,updated,markers,batch_revision) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (ident + "@" + target["feed_ident"], row["generation"], expected, path, proven["sha256"], row["head_sha"], target["repository"],
             row["pr_url"], row["batch_id"], time.time(), None, row["revision"]))
    derived = {path: item["sha256"] for path, item in actual.items() if path in DERIVED_PATHS}
    if derived:
        store.feed_set(receipt_key("sent-graded-derived", target), dict(files=derived, generation=row["generation"],
                                                                       source_commit=target["source_commit"]))
