"""Build one coalesced ``mindie-contribution/1`` batch from local material.

Zero model calls here: changed draft revisions and unbatched publishable
votes are rendered to canonical bytes, scanned once more as final outbound
material, staged into a dedicated private staging directory (never the user's
business checkout) and recorded as one pending outbox batch. The batch
revision digest is computed by the community package's own authoritative
``batch_revision`` — there is no parallel local implementation, so what core
builds is exactly what the community validator accepts. The batch lineage is
stable and opaque per domain, so a later flush updates the same open PR
instead of creating duplicates; the feedback file path and contents are
deterministic per lineage. The expected base for an update is the last
outbound body this lineage actually sent — never a newer bot-edited main
body that an old full draft must not overwrite.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from mindie_knowledge.markdown import _atomic_write_text
from mindie_knowledge.redact import scan_text

from .documents import render_entry
from .store import REPLACEABLE_BATCH, canonical, digest

def _filename(doc):
    subdir = "topics" if doc["kind"] == "knowledge" else "cases"
    # Correcting a title must edit the same file, not add a duplicate identity.
    return f"{subdir}/{doc['entry_id']}.md"


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def lineage_of(domain, generation=None):
    """Stable opaque batch/PR lineage for one domain and settings generation.

    A settings mutation (including an off/on toggle) starts a fresh lineage,
    so a cancelled old-generation batch is never replayed or overwritten,
    while repeated flushes within one generation update the same open PR."""
    return "batch-" + digest(["lineage", domain, generation or ""])[:24]


def _community_revision():
    try:
        from mindie_knowledge.community.batch import batch_revision
    except Exception:
        raise RuntimeError(
            "mindie_knowledge.community is not installed; building a "
            "contribution batch is a hard dependency failure, not a no-op"
        ) from None
    return batch_revision


def build_batch(store, *, settings, revision_fn=None):
    """Collect all pending material into one batch, or None when empty.

    On success the batch is already recorded pending in the outbox and its
    files staged; the caller submits it. A durable receipt is reserved before
    scanning, so a failed unchanged material revision is never rebuilt.
    """
    # Waiting for an older write is not an attempt to export new material.
    # Keep new drafts/votes unconsumed until the lineage is available. The
    # create_batch transaction repeats this check before replacing any receipt.
    previous = store.batch(lineage_of(store.domain, settings.generation))
    if previous is not None and previous["status"] not in REPLACEABLE_BATCH:
        return None
    if revision_fn is None:
        revision_fn = _community_revision()
    drafts = store.drafts_changed(generation=settings.generation)
    votes = store.unbatched_votes(generation=settings.generation)
    if not drafts and not votes:
        return None
    fingerprint = digest([
        settings.generation,
        sorted((d["entry_id"], d["revision"]) for d in drafts),
        sorted((v["root_opaque"], v["entry_id"], v["revision"],
                v["rating"], v["reason"]) for v in votes),
    ])
    if not store.reserve_export(fingerprint):
        return None
    try:
        result = _stage_batch(store, settings, drafts, votes, revision_fn)
    except Exception as exc:
        store.finish_export(fingerprint, "failed", str(exc))
        raise
    store.finish_export(fingerprint, "staged")
    return result


def _stage_batch(store, settings, drafts, votes, revision_fn):
    lineage = lineage_of(store.domain, settings.generation)
    files = []
    for doc in sorted(drafts, key=lambda d: d["entry_id"]):
        content = render_entry(doc)
        row = store._row(doc["entry_id"])
        # The honest expected base is the last body this lineage actually sent
        # (unmerged own-PR update); a fresh contribution has none. The newest
        # published body is only a base when it IS our last outbound body.
        base_sha = None
        if row is not None and row["batched_revision"]:
            sent = store._revision_doc(doc["entry_id"], row["batched_revision"])
            if sent is not None:
                base_sha = _sha(render_entry(sent))
        files.append(dict(path=_filename(doc), content=content,
                          sha256=_sha(content), base_sha256=base_sha))
    vote_keys = []
    if votes:
        payload = {
            "schema": "mindie-feedback/1",
            "votes": [
                dict(
                    vote_id=digest(["vote", v["root_opaque"], v["entry_id"],
                                    v["revision"]]),
                    root_id=v["root_opaque"], entry_id=v["entry_id"],
                    revision=v["revision"], rating=v["rating"],
                    reason=v["reason"],
                )
                for v in sorted(
                    votes,
                    key=lambda v: (v["root_opaque"], v["entry_id"], v["revision"]),
                )
            ],
        }
        content = canonical(payload) + "\n"
        files.append(dict(path=f"feedback/votes-{lineage[6:]}.json",
                          content=content, sha256=_sha(content), base_sha256=None))
        vote_keys = [(v["root_opaque"], v["entry_id"], v["revision"]) for v in votes]

    problems = []
    for file in files:
        findings = scan_text(file["content"])
        if findings:
            rules = ", ".join(sorted({f.rule for f in findings}))
            problems.append(f"{file['path']}: {rules}")
    if problems:
        raise ValueError(
            "outbound material failed the final redaction scan: "
            + "; ".join(problems)
        )

    base_commit = None
    if settings.repository:
        from .feed import feed_ident

        receipt = store.feed_get(
            "feed:" + feed_ident(settings.repository, settings.branch)
        )
        if receipt and receipt.get("commit"):
            base_commit = receipt["commit"]
    entry_refs = sorted({
        store.ref(d["entry_id"], d["revision"]) for d in [*drafts, *votes]
    })
    summary = f"{store.domain}: {len(drafts)} entries, {len(vote_keys)} votes"
    file_dicts = [
        {k: f[k] for k in ("path", "content", "sha256", "base_sha256")}
        for f in files
    ]
    revision = revision_fn(file_dicts, store.domain, base_commit, entry_refs)
    batch = dict(
        schema="mindie-contribution/1", batch_id=lineage, revision=revision,
        domain=store.domain, base_commit=base_commit,
        entry_refs=entry_refs, files=file_dicts,
        summary=summary[:240],
    )
    staging = store.root / "outbox" / "staging" / lineage
    for file in files:
        target = staging / file["path"]
        _atomic_write_text(target, file["content"])
    store.create_batch(
        batch_id=lineage, revision=revision, batch=batch,
        entry_ids=[d["entry_id"] for d in drafts], vote_keys=vote_keys,
        generation=settings.generation,
    )
    return lineage, revision, batch, [d["entry_id"] for d in drafts], vote_keys
