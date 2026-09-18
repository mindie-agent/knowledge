"""Mechanical Markdown/code navigation, without semantic truth judgments."""

from .service import affected_documents, backlinks, build_relations

__all__ = ["affected_documents", "backlinks", "build_relations"]
