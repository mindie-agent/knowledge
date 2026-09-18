"""Locate the corpus that ships in the ``mindie-knowledge`` wheel.

Checkout layout stays ``corpus/`` at the repository root. The wheel maps that
tree to ``mindie_knowledge/data/corpus`` so this module and the data directory
do not share a path.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from importlib import metadata
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
_PACKAGED_ROOT = _PACKAGE_DIR / "data" / "corpus"
_CHECKOUT_ROOT = _PACKAGE_DIR.parent / "corpus"


def corpus_root() -> Path:
    """Return the packaged corpus directory, or the checkout ``corpus/``."""

    if _PACKAGED_ROOT.is_dir():
        return _PACKAGED_ROOT
    if _CHECKOUT_ROOT.is_dir():
        return _CHECKOUT_ROOT
    return _PACKAGED_ROOT


def iter_entry_files() -> Iterator[Path]:
    """Yield bundled Markdown reference files, sorted by path."""
    root = corpus_root()
    yield from sorted(
        path for path in root.rglob("*.md")
        if path.is_file() and not any(part.startswith(".") for part in path.relative_to(root).parts)
    )


def installed_commit() -> str | None:
    """Git commit of the installed distribution, or ``None`` if unknown."""

    try:
        dist = metadata.distribution("mindie-knowledge")
    except metadata.PackageNotFoundError:
        return None
    raw = dist.read_text("direct_url.json")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    vcs = data.get("vcs_info")
    if not isinstance(vcs, dict):
        return None
    commit_id = vcs.get("commit_id")
    return commit_id if isinstance(commit_id, str) and commit_id else None
