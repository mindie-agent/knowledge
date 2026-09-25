"""Build one coalesced ``mindie-contribution/1`` batch from local material.

Zero model calls here: changed draft revisions and unbatched publishable
votes are rendered to canonical bytes, staged into a dedicated private
staging directory (never the user's business checkout) and recorded as one
pending outbox batch. The batch revision digest is computed by the community
package's own authoritative ``batch_revision`` — there is no parallel local
implementation, so what core builds is exactly what the community validator
accepts. The batch lineage is stable and opaque per domain, so a later flush
updates the same open PR instead of creating duplicates; the feedback file
path and contents are deterministic per lineage.

This local deterministic packaging does no remote writes, so its failures are
classified, never uniformly permanent: a final redaction-scan rejection is
quarantined to exactly the unsafe entry (its material never reruns and
unrelated entries keep flowing), while a transient local failure
(unwritable staging, interrupted process) is persisted with a backoff
``next_check`` and resumes automatically once due — no fingerprint is ever
permanently consumed by a crash before the outbox row exists. One flush is a
bounded in-memory operation: material beyond the per-flush envelope simply
stays unbatched for the next automatic batch after this one resolves — never
dropped, never user-managed. The expected base and the confirmed observation
markers for an entry update come only from confirmed sending history (the
per-entry sent receipt) — never from local batching state
(``batched_revision``), which cannot prove a remote write happened.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from mindie_knowledge.markdown import _atomic_write_text
from mindie_knowledge.redact import scan_text

# The envelopes core chunks to are exactly the community validator's (same
# package release): the real per-file platform envelope and one bounded
# in-memory per-flush operation.
from mindie_knowledge.community.common import MAX_BATCH_BYTES, MAX_FILE_BYTES

from .documents import render_entry
from .store import REPLACEABLE_BATCH, canonical, digest

# One feedback file's vote bound; further votes go to the next batch.
MAX_BATCH_VOTES = 500


class ExportContentError(ValueError):
    """Deterministic content/integrity rejection at the outbound boundary.

    Only this class is quarantined permanently per fingerprint; every other
    build failure (local IO, dependency, lineage race) is transient and
    resumes with backoff, so valid material is never stranded by a
    misclassified permanent record."""


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
    """Collect pending material into one batch, or None when empty/waiting.

    On success the batch is already recorded pending in the outbox and its
    files staged; the caller submits it. The material fingerprint is reserved
    before staging with an advance backoff timestamp, so an interrupted or
    transiently failing build resumes when due instead of spinning every idle
    tick or locking the material forever. Drafts are first re-seated onto the
    authoritative published bodies, so the fingerprint covers the content
    that will actually be sent.
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
    for doc in drafts:
        store.rebase_draft_on_published(doc["entry_id"])
    if drafts:
        # A rebase may have advanced or dropped drafts; fingerprint the
        # content that will actually be sent.
        drafts = store.drafts_changed(generation=settings.generation)
    votes = store.unbatched_votes(generation=settings.generation)
    if not drafts and not votes:
        return None
    planned = _plan_batch(
        store, drafts, votes, lineage_of(store.domain, settings.generation)
    )
    if planned is None:
        return None  # everything pending was quarantined to its own content
    fingerprint = digest([
        settings.generation,
        sorted(planned["draft_refs"]),
        sorted((v["root_opaque"], v["entry_id"], v["revision"],
                v["rating"], v["reason"]) for v in planned["vote_rows"]),
    ])
    if not store.reserve_export(fingerprint):
        return None
    try:
        result = _stage_batch(store, settings, planned, revision_fn)
    except Exception as exc:
        classification = (
            "content" if isinstance(exc, ExportContentError) else "transient"
        )
        store.finish_export(
            fingerprint, "failed", str(exc), classification=classification
        )
        raise
    store.finish_export(fingerprint, "staged")
    return result


def _vote_payload(votes):
    return {
        "schema": "mindie-feedback/1",
        "votes": [
            dict(
                vote_id=digest(["vote", v["root_opaque"], v["entry_id"],
                                v["revision"]]),
                root_id=v["root_opaque"], entry_id=v["entry_id"],
                revision=v["revision"], rating=v["rating"],
                reason=v["reason"],
            )
            for v in votes
        ],
    }


def _canonical_len(value):
    return len(canonical(value).encode("utf-8"))


def _batch_serialized_size(file_lens, ref_lens, *, summary, domain, lineage):
    """Exact UTF-8 length of the canonical batch JSON for these contents.

    Accounting happens against the complete serialized payload (file content
    escapes, metadata, refs), not raw Markdown bytes, so the durable row
    envelope check can never fire after planning. ``base_commit`` is accounted
    at its maximum length."""
    total = 2 + 7  # braces plus commas between the eight top-level keys
    for key, value in (
        ("schema", "mindie-contribution/1"), ("batch_id", lineage),
        ("revision", "0" * 64), ("domain", domain),
        ("base_commit", "0" * 64), ("summary", summary),
    ):
        total += _canonical_len(key) + 1 + _canonical_len(value)
    files_part = 2 + sum(file_lens) + max(0, len(file_lens) - 1)
    refs_part = 2 + sum(ref_lens) + max(0, len(ref_lens) - 1)
    total += _canonical_len("files") + 1 + files_part
    total += _canonical_len("entry_refs") + 1 + refs_part
    return total


def _plan_batch(store, drafts, votes, lineage):
    """Scan and chunk pending material for one flush.

    The per-entry final scan quarantines exactly the unsafe entries (their
    content can never leave, and it never blocks or suppresses unrelated
    valid material). Returns ``None`` when nothing sendable remains. Chunking
    measures the complete serialized batch payload — a platform-legal single
    file always fits alone, and material beyond one flush simply stays
    unbatched for the next automatic batch (never dropped, never
    user-managed)."""
    kept_votes = sorted(
        votes, key=lambda v: (v["root_opaque"], v["entry_id"], v["revision"])
    )[:MAX_BATCH_VOTES]
    files = []
    if kept_votes:
        feedback_content = canonical(_vote_payload(kept_votes)) + "\n"
        files.append(dict(path=f"feedback/votes-{lineage[6:]}.json",
                          content=feedback_content,
                          sha256=_sha(feedback_content),
                          base_sha256=None, sent_markers=None))
    file_lens = [_canonical_len(f) for f in files]
    summary = f"{store.domain}: {len(drafts)} entries, {len(kept_votes)} votes"[:240]
    ref_lens = []
    vote_ref_lens = [_canonical_len(store.ref(v["entry_id"], v["revision"]))
                     for v in kept_votes]

    kept_drafts = []
    for doc in sorted(drafts, key=lambda d: d["entry_id"]):
        content = render_entry(doc)
        findings = scan_text(content)
        if findings:
            rules = ", ".join(sorted({f.rule for f in findings}))
            store.quarantine_entry(
                doc["entry_id"], kind="content-scan",
                detail=f"final outbound redaction scan: {rules}",
            )
            continue
        size = len(content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            # Unreachable through append (the platform envelope is enforced
            # there); a tampered store must not silently drop the entry.
            store.quarantine_entry(
                doc["entry_id"], kind="platform-envelope",
                detail="rendered entry exceeds the per-file platform envelope",
            )
            continue
        sent_markers = store.sent_markers(doc["entry_id"])
        file_dict = dict(
            path=_filename(doc), content=content, sha256=_sha(content),
            base_sha256=_expected_base(store, doc["entry_id"]),
            sent_markers=sorted(sent_markers) if sent_markers is not None else None,
        )
        new_file_len = _canonical_len(file_dict)
        ref_len = _canonical_len(store.ref(doc["entry_id"], doc["revision"]))
        if (
            kept_drafts  # the grouping budget binds only groups of records…
            and _batch_serialized_size(
                file_lens + [new_file_len], ref_lens + vote_ref_lens + [ref_len],
                summary=summary, domain=store.domain, lineage=lineage,
            ) > MAX_BATCH_BYTES
        ):
            continue  # stays unbatched; the next automatic batch includes it
        # …while a single platform-legal document with its required JSON
        # overhead always goes through, even when it alone exceeds the soft
        # per-flush budget.
        kept_drafts.append(doc)
        files.append(file_dict)
        file_lens.append(new_file_len)
        ref_lens.append(ref_len)
    if not kept_drafts and not kept_votes:
        return None
    entry_refs = sorted({store.ref(d["entry_id"], d["revision"])
                         for d in kept_drafts}
                        | {store.ref(v["entry_id"], v["revision"])
                           for v in kept_votes})
    return dict(
        files=files, entry_refs=entry_refs,
        draft_refs=[(d["entry_id"], d["revision"]) for d in kept_drafts],
        vote_keys=[(v["root_opaque"], v["entry_id"], v["revision"])
                   for v in kept_votes],
        vote_rows=kept_votes,
    )


def _expected_base(store, entry_id):
    """The last confirmed-sent body hash for one entry, or None (creation).

    Only confirmed sending history counts: the per-entry sent receipt is
    written after a confirmed submission, so a failed first send leaves no
    base and the next batch is a creation, not a false "remote deleted"
    conflict. The hash is provenance for the conflict checks; a moved remote
    body is reconciled by observation-marker merge at publication time, never
    by restoring an old full draft over it.
    """
    receipt = store.sent_receipt(entry_id)
    if receipt is None:
        return None
    return receipt["sha256"]


def _stage_batch(store, settings, plan, revision_fn):
    lineage = lineage_of(store.domain, settings.generation)
    files = plan["files"]
    for file in files:
        if file["path"].startswith("feedback/"):
            # The feedback file was admitted through per-vote scans; a finding
            # here means local tampering, so the whole build still fails closed.
            findings = scan_text(file["content"])
            if findings:
                rules = ", ".join(sorted({f.rule for f in findings}))
                raise ExportContentError(
                    f"outbound feedback failed the final redaction scan: {rules}"
                )

    base_commit = None
    if settings.repository:
        from .feed import feed_ident

        receipt = store.feed_get(
            "feed:" + feed_ident(settings.repository, settings.branch)
        )
        if receipt and receipt.get("commit"):
            base_commit = receipt["commit"]
    entry_refs = plan["entry_refs"]
    summary = (
        f"{store.domain}: {len(plan['draft_refs'])} entries, "
        f"{len(plan['vote_keys'])} votes"
    )
    revision = revision_fn(files, store.domain, base_commit, entry_refs)
    batch = dict(
        schema="mindie-contribution/1", batch_id=lineage, revision=revision,
        domain=store.domain, base_commit=base_commit,
        entry_refs=entry_refs, files=files,
        summary=summary[:240],
    )
    staging = store.root / "outbox" / "staging" / lineage
    for file in files:
        target = staging / file["path"]
        _atomic_write_text(target, file["content"])
    try:
        store.create_batch(
            batch_id=lineage, revision=revision, batch=batch,
            entry_ids=[entry_id for entry_id, _rev in plan["draft_refs"]],
            vote_keys=plan["vote_keys"],
            generation=settings.generation,
        )
    except ValueError as exc:
        # The planner already accounted the exact serialized payload, so an
        # envelope failure here is a deterministic accounting/store defect —
        # never a transient retry loop.
        raise ExportContentError(
            f"serialized batch was rejected after exact planning: {exc}"
        ) from exc
    return (lineage, revision, batch,
            [entry_id for entry_id, _rev in plan["draft_refs"]],
            plan["vote_keys"])
