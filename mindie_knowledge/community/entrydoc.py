"""Canonical ``mindie-entry/2`` document access for the community package.

Core owns the single document definition in ``mindie_knowledge.loop.documents``
(``render_entry`` / ``parse_entry`` / ``revision_of`` / ``append_observation``).
This module only delegates. If the integrated core module is missing, that is
an explicit integration dependency error — never an alternate schema
implementation. (Root note 6: no duplicate fallback parser.)
"""

from __future__ import annotations

from typing import Any, Mapping

SCHEMA = "mindie-entry/2"


def _core():
    try:
        from mindie_knowledge.loop import documents
    except ImportError as exc:
        raise RuntimeError(
            "mindie_knowledge.loop.documents is required (core package provides the "
            "canonical mindie-entry/2 implementation); integrate the core module first"
        ) from exc
    missing = [
        name
        for name in ("render_entry", "parse_entry", "revision_of")
        if not hasattr(documents, name)
    ]
    if missing:
        raise RuntimeError(
            f"mindie_knowledge.loop.documents lacks {missing}; the integrated core "
            "must provide the canonical contract interface"
        )
    return documents


def render_entry(doc: Mapping[str, Any]) -> str:
    return _core().render_entry(dict(doc))


def parse_entry(markdown: str) -> dict[str, Any]:
    return _core().parse_entry(markdown)


def revision_of(doc: Mapping[str, Any]) -> str:
    return _core().revision_of(dict(doc))
