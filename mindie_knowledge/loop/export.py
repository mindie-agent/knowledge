"""[Role] Freeze authorized task or graded material into the shared outbox.

[Invariants] File stores remain the body authority; the outbox records exact
file identities. Existing grants bind export eligibility to its generation.
[Timeouts & Locks] Store transactions retain the shared SQLite lock budget;
no model, subprocess or network wait is introduced during batch construction.
[Side-Effect Policy] Write frozen local files and the existing outbox. The
existing Git/GitHub publisher alone owns external writes.
"""
from __future__ import annotations

import hashlib

from mindie_knowledge.community.batch import batch_revision, validate_batch
from mindie_knowledge.community.common import MAX_BATCH_BYTES, MAX_FILE_BYTES
from mindie_knowledge.materials.publication import CleanupReceiptError, freeze_batch, load_batch_payload
from mindie_knowledge.redact import scan_text

from .store import REPLACEABLE_BATCH, canonical, digest
from .publication_target import publication_target

MAX_BATCH_VOTES = 500


class ExportContentError(ValueError):
    """Unsafe or structurally invalid outbound material; never a transport error."""


class DistillationRequired(ValueError):
    """Retained material drafts need explicit C6 admission for a graded target."""


class ExportQuarantinedError(ExportContentError):
    """A partial local build survives, but rejected packages remain visible."""

    def __init__(self, rejections, built=None):
        self.rejections = rejections
        self.built = built
        self.publication = None
        super().__init__("task publication incomplete: " + "; ".join(
            item["entry_id"] + ": " + item["detail"] for item in rejections))


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


def _task_files(store, doc, *, source_revisions=None, target=None):
    # Bind the files and the observed feed identity together. A concurrent
    # feed/capture change is visible and retried through existing export state.
    with store._write_txn():
        store.rebase_draft_on_published(doc["entry_id"])
        row = store._row(doc["entry_id"])
        if row is None or row["draft_revision"] != doc["revision"]:
            raise BlockingIOError("current task changed before publication preparation")
        files = _task_files_for_revision(store, doc, target=target)
        if source_revisions is not None:
            source_revisions[doc["entry_id"]] = row["published_revision"] if row["feed_active"] else None
        return files


def _task_files_for_revision(store, doc, *, target=None):
    package = store.materials.export_task(
        doc["entry_id"], source="draft", revision=doc["revision"], streaming=True,
    )
    if package["task_id"] != doc["entry_id"] or package["revision"] != doc["revision"]:
        raise ValueError("current task package changed while preparing its contribution")
    if not package["ready"]:
        raise ValueError("current task package still has unfinished material indexes")
    prefix = f"tasks/{package['task_id']}/"
    previous = {file["path"]: file["sha256"] for file in store.contribution_base_files(doc["entry_id"], target=target)
                if file.get("sha256") is not None}
    files = []
    for relative in sorted(package['files']):
        source = package['files'].file(relative)
        content = source['content']
        path = prefix + relative
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ExportContentError("task package file exceeds the GitHub file envelope")
        findings = scan_text(content)
        if findings:
            rules = ", ".join(sorted({finding.rule for finding in findings}))
            raise ExportContentError(f"final outbound redaction scan: {rules}")
        files.append(source.with_metadata(path=path, base_sha256=previous.get(path)))
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
    from ..graded.export import build_batch as build_graded_batch
    graded = build_graded_batch(store, settings=settings, revision_fn=revision_fn)
    if graded is not None:
        return graded
    target = publication_target(store, settings)
    lineage = lineage_of(store.domain + ":" + target["feed_ident"], settings.generation)
    previous = store.batch(lineage)
    if previous is not None and previous["status"] not in REPLACEABLE_BATCH:
        return None
    with store.lock:
        continued = [row[0] for row in store.db.execute(
            "SELECT entry_id FROM entries WHERE feed_active=1 AND draft_revision IS NOT NULL")]
    for entry_id in continued:
        store.rebase_draft_on_published(entry_id)
    drafts = store.drafts_changed(generation=settings.generation, ready_only=True, include_content=False)
    drafts = [doc for doc in drafts if store.summary_ready(doc)]
    votes = sorted((vote for vote in store.unbatched_votes(generation=settings.generation)
                    if not vote["entry_id"].startswith("unit-")
                    and vote["entry_id"] in store.material_feeds().get(target["feed_ident"], {}).get("entries", {})),
                   key=lambda vote: (vote["root_opaque"], vote["entry_id"], vote["revision"]))[:MAX_BATCH_VOTES]
    if not drafts and not votes:
        return None
    if target["feed_ident"] in store.graded._catalog()["feeds"]:
        raise DistillationRequired(
            f"{len(drafts)} local material drafts remain retained; this publication target uses graded units. "
            "Select their evidence through C6 before publication; no legacy batch was frozen.")
    files, kept, source_revisions, rejections = [], [], {}, []
    total = 0
    for doc in sorted(drafts, key=lambda item: item["entry_id"]):
        try:
            task_files = _task_files(store, doc, source_revisions=source_revisions, target=target)
        except ExportContentError as exc:
            store.quarantine_entry(doc["entry_id"], kind="content-scan", detail=str(exc))
            rejections.append(dict(entry_id=doc["entry_id"], stage="content-scan", detail=str(exc)))
            continue
        # Preserve the grouping envelope while encoding only one file at a
        # time. A long task never becomes one giant JSON allocation.
        size = 2 + max(0, len(task_files) - 1) + sum(
            len(canonical(dict(file)).encode('utf-8')) for file in task_files)
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
        if rejections:
            raise ExportQuarantinedError(rejections)
        return None
    refs = sorted({store.ref(doc["entry_id"], doc["revision"]) for doc in kept}
                  | {store.ref(vote["entry_id"], vote["revision"]) for vote in votes})
    fingerprint = digest([settings.generation, [(file["path"], file["sha256"]) for file in files], refs])
    if not store.reserve_export(fingerprint):
        if rejections:
            raise ExportQuarantinedError(rejections)
        return None
    try:
        base_commit = target["source_commit"]
        revision = (revision_fn or batch_revision)(files, store.domain, base_commit, refs)
        batch = dict(schema="mindie-contribution/1", batch_id=lineage, revision=revision,
                     domain=store.domain, base_commit=base_commit, entry_refs=refs, files=files,
                     summary=f"{store.domain}: {len(kept)} task packages, {len(votes)} votes"[:240])
        validate_batch(batch)
        descriptor = freeze_batch(store.root, batch)
        batch = load_batch_payload(store.root, descriptor)
        entry_ids = [doc["entry_id"] for doc in kept]
        vote_keys = [(vote["root_opaque"], vote["entry_id"], vote["revision"]) for vote in votes]
        store.create_batch(batch_id=lineage, revision=revision, batch=descriptor,
                           entry_ids=entry_ids, vote_keys=vote_keys, generation=settings.generation,
                           source_revisions={ident: source_revisions[ident] for ident in entry_ids},
                           entry_revisions={doc["entry_id"]: doc["revision"] for doc in kept},
                           publication_target=target)
    except CleanupReceiptError:
        # The replacement is already durable and pending. Its local cleanup
        # error cannot turn it into a failed export or authorize another build.
        store.finish_export(fingerprint, "staged")
        raise
    except Exception as exc:
        if rejections:
            exc.add_note(str(ExportQuarantinedError(rejections)))
        store.finish_export(fingerprint, "failed", str(exc),
                            classification="content" if isinstance(exc, ExportContentError) else "transient")
        raise
    store.finish_export(fingerprint, "staged")
    built = lineage, revision, batch, entry_ids, vote_keys
    if rejections:
        raise ExportQuarantinedError(rejections, built)
    return built
