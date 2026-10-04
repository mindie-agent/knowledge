"""Model-free publication of complete, current task Markdown packages.

The file material store is the only body authority. This module freezes the
navigation index and all ordered blocks together, then records only file
identities in the outbox. Existing Git/GitHub publication owns remote writes.
"""
from __future__ import annotations

import hashlib

from mindie_knowledge.community.batch import batch_revision, validate_batch
from mindie_knowledge.community.common import MAX_BATCH_BYTES, MAX_FILE_BYTES
from mindie_knowledge.materials.publication import CleanupReceiptError, freeze_batch
from mindie_knowledge.redact import scan_text

from .store import REPLACEABLE_BATCH, canonical, digest

MAX_BATCH_VOTES = 500


class ExportContentError(ValueError):
    """Unsafe or structurally invalid outbound material; never a transport error."""


def lineage_of(domain, generation=None):
    return "batch-" + digest(["lineage", domain, generation or ""])[:24]


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _vote_payload(votes):
    return {
        "schema": "mindie-feedback/1",
        "votes": [dict(
            vote_id=digest(["vote", vote["root_opaque"], vote["entry_id"], vote["revision"]]),
            root_id=vote["root_opaque"], entry_id=vote["entry_id"],
            revision=vote["revision"], rating=vote["rating"], reason=vote["reason"],
        ) for vote in votes],
    }


def _task_files(store, doc):
    package = store.materials.export_task(
        doc["entry_id"], source="draft", revision=doc["revision"],
    )
    if package["task_id"] != doc["entry_id"] or package["revision"] != doc["revision"]:
        raise ValueError("current task package changed while preparing its contribution")
    if not package["ready"]:
        raise ValueError("current task package still has unfinished material indexes")
    prefix = f"tasks/{package['task_id']}/"
    previous = {file["path"]: file["sha256"] for file in store.sent_package_files(doc["entry_id"])
                if file.get("sha256") is not None}
    files = []
    for relative, content in sorted(package["files"].items()):
        path = prefix + relative
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ExportContentError("task package file exceeds the GitHub file envelope")
        findings = scan_text(content)
        if findings:
            rules = ", ".join(sorted({finding.rule for finding in findings}))
            raise ExportContentError(f"final outbound redaction scan: {rules}")
        files.append(dict(path=path, content=content, sha256=_sha(content),
                          base_sha256=previous.get(path)))
    current = {file["path"] for file in files}
    for path, sha in sorted(previous.items()):
        if not path.startswith(prefix):
            raise ValueError("sent task receipt contains another task's file")
        if path not in current:
            files.append(dict(path=path, content=None, sha256=None,
                              base_sha256=sha, delete=True))
    return files


def build_batch(store, *, settings, revision_fn=None):
    """Freeze one complete package batch, returning its existing engine tuple."""
    lineage = lineage_of(store.domain, settings.generation)
    previous = store.batch(lineage)
    if previous is not None and previous["status"] not in REPLACEABLE_BATCH:
        return None
    drafts = store.drafts_changed(generation=settings.generation, ready_only=True)
    drafts = [doc for doc in drafts if store.summary_ready(doc)]
    votes = sorted(store.unbatched_votes(generation=settings.generation),
                   key=lambda vote: (vote["root_opaque"], vote["entry_id"], vote["revision"]))[:MAX_BATCH_VOTES]
    if not drafts and not votes:
        return None
    files, kept = [], []
    total = 0
    for doc in sorted(drafts, key=lambda item: item["entry_id"]):
        try:
            task_files = _task_files(store, doc)
        except ExportContentError as exc:
            store.quarantine_entry(doc["entry_id"], kind="content-scan", detail=str(exc))
            continue
        size = len(canonical(task_files).encode("utf-8"))
        if kept and total + size > MAX_BATCH_BYTES:
            continue  # whole task remains pending; never publish half a package
        files.extend(task_files)
        kept.append(doc)
        total += size
    if votes:
        content = canonical(_vote_payload(votes)) + "\n"
        vote_file = dict(path=f"feedback/votes-{lineage[6:]}.json", content=content,
                         sha256=_sha(content), base_sha256=None)
        if not files or total + len(canonical(vote_file).encode("utf-8")) <= MAX_BATCH_BYTES:
            files.append(vote_file)
        else:
            votes = []
    if not files:
        return None
    refs = sorted({store.ref(doc["entry_id"], doc["revision"]) for doc in kept}
                  | {store.ref(vote["entry_id"], vote["revision"]) for vote in votes})
    fingerprint = digest([settings.generation, [(file["path"], file["sha256"]) for file in files], refs])
    if not store.reserve_export(fingerprint):
        return None
    try:
        base_commit = store.feed_get("published-commit")
        if isinstance(base_commit, dict):
            base_commit = base_commit.get("commit")
        if not isinstance(base_commit, str):
            base_commit = None
        revision = (revision_fn or batch_revision)(files, store.domain, base_commit, refs)
        batch = dict(schema="mindie-contribution/1", batch_id=lineage, revision=revision,
                     domain=store.domain, base_commit=base_commit, entry_refs=refs, files=files,
                     summary=f"{store.domain}: {len(kept)} task packages, {len(votes)} votes"[:240])
        validate_batch(batch)
        descriptor = freeze_batch(store.root, batch)
        entry_ids = [doc["entry_id"] for doc in kept]
        vote_keys = [(vote["root_opaque"], vote["entry_id"], vote["revision"]) for vote in votes]
        store.create_batch(batch_id=lineage, revision=revision, batch=descriptor,
                           entry_ids=entry_ids, vote_keys=vote_keys, generation=settings.generation)
    except CleanupReceiptError:
        # The replacement is already durable and pending. Its local cleanup
        # error cannot turn it into a failed export or authorize another build.
        store.finish_export(fingerprint, "staged")
        raise
    except Exception as exc:
        store.finish_export(fingerprint, "failed", str(exc),
                            classification="content" if isinstance(exc, ExportContentError) else "transient")
        raise
    store.finish_export(fingerprint, "staged")
    return lineage, revision, batch, entry_ids, vote_keys
