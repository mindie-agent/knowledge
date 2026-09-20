"""Build one coalesced ``mindie-contribution/1`` batch from local material.

Zero model calls here: changed draft revisions and unbatched publishable
votes are rendered to canonical bytes, scanned once more as final outbound
material, staged into a dedicated private staging directory (never the user's
business checkout) and recorded as one pending outbox batch. The community
package owns the actual publication intent/receipt ledger; core records its
receipt. A deterministic scan failure keeps the material local and visible —
nothing rewrites it to sneak past the rules.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from pathlib import Path

from mindie_knowledge.markdown import _atomic_write_text
from mindie_knowledge.redact import scan_text

from .documents import render_entry
from .store import canonical, digest

_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")


def _filename(doc):
    base = _SLUG_UNSAFE.sub("-", doc["title"].casefold()).strip("-") or "entry"
    subdir = "topics" if doc["kind"] == "knowledge" else "cases"
    return f"{subdir}/{base[:60]}-{doc['entry_id'][:12]}.md"


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_batch(store, *, settings):
    """Collect all pending material into one batch, or None when empty.

    On success the batch is already recorded pending in the outbox and its
    files staged; the caller submits it. Scan failures raise ValueError with
    the rule list and create nothing.
    """
    drafts = store.drafts_changed()
    votes = store.unbatched_votes()
    if not drafts and not votes:
        return None
    files = []
    for doc in sorted(drafts, key=lambda d: d["entry_id"]):
        content = render_entry(doc)
        base_sha = None
        row = store._row(doc["entry_id"])
        if row is not None and row["published_revision"]:
            base = store._revision_doc(doc["entry_id"], row["published_revision"])
            if base is not None:
                base_sha = _sha(render_entry(base))
        files.append(dict(path=_filename(doc), content=content,
                          sha256=_sha(content), base_sha256=base_sha))
    vote_keys = []
    if votes:
        payload = {
            "schema": "mindie-feedback/1",
            "votes": [
                dict(
                    vote_id=digest(["vote", v["root_opaque"], v["entry_id"]]),
                    root_id=v["root_opaque"], entry_id=v["entry_id"],
                    revision=v["revision"], rating=v["rating"],
                    reason=v["reason"],
                )
                for v in sorted(votes, key=lambda v: (v["root_opaque"], v["entry_id"]))
            ],
        }
        content = canonical(payload) + "\n"
        batch_tag = secrets.token_hex(8)
        files.append(dict(path=f"feedback/votes-{batch_tag}.json",
                          content=content, sha256=_sha(content), base_sha256=None))
        vote_keys = [(v["root_opaque"], v["entry_id"]) for v in votes]

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

    batch_id = "batch-" + secrets.token_hex(16)
    base_commit = None
    if settings.repository:
        from .feed import feed_ident

        receipt = store.feed_get(
            "feed:" + feed_ident(settings.repository, settings.branch)
        )
        if receipt and receipt.get("commit"):
            base_commit = receipt["commit"]
    summary = f"{store.domain}: {len(drafts)} entries, {len(vote_keys)} votes"
    revision = digest({
        "schema": "mindie-contribution/1",
        "domain": store.domain,
        "base_commit": base_commit,
        "files": sorted((f["path"], f["sha256"]) for f in files),
    })
    batch = dict(
        schema="mindie-contribution/1", batch_id=batch_id, revision=revision,
        domain=store.domain, base_commit=base_commit,
        entry_refs=[store.ref(d["entry_id"]) for d in drafts],
        files=[{k: f[k] for k in ("path", "content", "sha256", "base_sha256")}
               for f in files],
        summary=summary[:240],
    )
    staging = store.root / "outbox" / "staging" / batch_id
    for file in files:
        target = staging / file["path"]
        _atomic_write_text(target, file["content"])
    store.create_batch(
        batch_id=batch_id, revision=revision, batch=batch,
        entry_ids=[d["entry_id"] for d in drafts], vote_keys=vote_keys,
    )
    return batch_id, revision, batch, [d["entry_id"] for d in drafts], vote_keys
