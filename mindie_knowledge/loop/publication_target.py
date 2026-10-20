"""[Role] Bind a frozen contribution to its configured repository and branch.

[Invariants] A read feed never becomes the publication target by syncing last.
Frozen source identities survive later configuration or feed changes.
[Timeouts & Locks] Local metadata reads only; callers own the Store transaction.
[Side-Effect Policy] No configuration creation, authorization or remote writes.
"""
from __future__ import annotations

from .feed import feed_ident


def publication_target(store, settings):
    identity = feed_ident(settings.repository, settings.branch)
    source = store.feed_get("feed:" + identity) or {}
    graded = store.graded._catalog()["feeds"].get(identity)
    material = store.material_feeds().get(identity)
    commit = (graded or material or {}).get("source_revision") or source.get("commit")
    return dict(feed_ident=identity, repository=settings.repository, branch=settings.branch,
                source_commit=commit)


def matches_settings(target, settings):
    return (target["repository"], target["branch"]) == (settings.repository, settings.branch)


def receipt_key(kind, target, identity=None):
    return kind + ":" + target["feed_ident"] + (":" + identity if identity else "")
