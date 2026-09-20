"""Small value object and atomic writes used by the current knowledge loop.

Public entries are parsed/rendered by loop.documents. There is no parallel
legacy Markdown/sidecar store or title-derived document identity.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Document:
    layer: str
    title: str
    content: str
    slug: str
    path: Path
    uri: str


def _atomic_write_bytes(path: Path, raw: bytes) -> None:
    """Replace ``path`` with exact ``raw`` bytes; no newline translation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))
