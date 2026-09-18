"""Write path: candidate Markdown only.

Capture requires a title and non-empty content. Known source, conditions, and
evidence are kept when supplied; unknown values are omitted. Shared and
project layers are refused. Indexing through OpenViking is best-effort: a
down index still leaves the Markdown file on disk.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

from mindie_knowledge.local.backend import backend_for_config
from mindie_knowledge.local.reconcile import remember_document
from mindie_knowledge.markdown import (
    delete_document,
    Document,
    document_from_text,
    document_slug,
    iter_markdown_files,
    load_document,
    MAX_METADATA_BYTES,
    MAX_REFERENCE_BYTES,
    meta_path,
    read_bounded,
    relative_posix,
    save_document,
    uri_for,
    utc_now,
)
from mindie_knowledge.server.layers import WRITABLE_LAYERS, ServiceConfig, load_config


class CaptureRefused(Exception):
    """The requested write is not allowed (wrong layer, read-only mount)."""

    def __init__(self, message: str, *, layer: str | None = None):
        super().__init__(message)
        self.layer = layer


class CaptureRejected(Exception):
    """The document itself is not well formed enough to store."""

    def __init__(self, problems: Sequence[str]):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


def candidate_root(config: ServiceConfig, *, create: bool = True) -> Path:
    mount = config.mount("candidate")
    if not mount.roots:
        raise CaptureRefused("candidate layer is not configured", layer="candidate")
    if mount.read_only:
        raise CaptureRefused("candidate layer is read-only", layer="candidate")
    root = Path(mount.roots[0])
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


@dataclass
class CaptureLookup:
    document: Document | None = None
    method: str = "bounded_scan"
    incomplete: bool = False
    missing_ref: str | None = None
    snapshot: str | None = None

    def describe(self) -> dict[str, Any]:
        result: dict[str, Any] = {"method": self.method, "incomplete": self.incomplete}
        if self.snapshot:
            result["snapshot"] = self.snapshot
        if self.incomplete:
            result["detail"] = "Title lookup was incomplete; an unobserved renamed note may exist. Existing notes are preserved."
        return result


def _read_note(path: Path, root: Path, *, byte_limit: int = MAX_REFERENCE_BYTES + MAX_METADATA_BYTES) -> tuple[Document, int]:
    path.resolve().relative_to(root.resolve())
    sidecar = meta_path(path)
    sidecar.resolve().relative_to(root.resolve())
    raw = read_bounded(path, min(MAX_REFERENCE_BYTES, byte_limit))
    metadata = read_bounded(sidecar, min(MAX_METADATA_BYTES, byte_limit - len(raw))) if sidecar.is_file() else b""
    value = json.loads(metadata.decode("utf-8")) if metadata else {}
    if not isinstance(value, dict):
        raise ValueError("note metadata must be an object")
    return document_from_text(raw.decode("utf-8"), path=path, layer="candidate", root=root,
                              metadata=value), len(raw) + len(metadata)


def lookup_capture(root: Path, heading: str, *, config: ServiceConfig, preserve_identity: bool = False) -> CaptureLookup:
    """Resolve one title using exact paths and the maintained catalog.

    Cold compatibility work is capped at 32 notes, 256 directory entries,
    512 KiB and 25 ms between filesystem operations. It never builds an index.
    A catalog snapshot describes observed titles, not arbitrary later edits.
    """
    started = time.perf_counter()
    target = root / f"{document_slug(heading)}.md"
    if target.exists():
        try:
            document, _ = _read_note(target, root)
        except (OSError, UnicodeError, ValueError):
            return CaptureLookup(method="unreadable_target", incomplete=True,
                                 missing_ref=uri_for("candidate", target.name))
        if preserve_identity or document.title.strip() == heading:
            return CaptureLookup(document=document, method="exact_path")

    from mindie_knowledge.catalog import catalog_title_matches

    observed = catalog_title_matches(config, heading, root=root)
    result = CaptureLookup(method="catalog" if observed["available"] else "bounded_scan",
                           incomplete=bool(observed["incomplete"]), snapshot=observed.get("snapshot"))
    if observed["available"]:
        for match in observed["matches"]:
            path = Path(match["path"])
            try:
                document, _ = _read_note(path, root)
            except FileNotFoundError:
                result.missing_ref = result.missing_ref or match["uri"]
                result.incomplete = True
                continue
            except (OSError, UnicodeError, ValueError):
                result.missing_ref = result.missing_ref or match["uri"]
                result.incomplete = True
                continue
            if preserve_identity or document.title.strip() == heading:
                result.document = document
                return result
            result.incomplete = True
        return result

    result.incomplete = False
    pending, entries_seen, notes_seen, read_bytes = [root], 0, 0, 0

    def exhausted() -> bool:
        stopped = (entries_seen >= 256 or notes_seen >= 32 or read_bytes >= 524288
                   or (time.perf_counter() - started) * 1000 >= 25)
        result.incomplete = result.incomplete or stopped
        return stopped

    while pending:
        if exhausted():
            break
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                while not exhausted():
                    try:
                        entry = next(entries)
                    except StopIteration:
                        break
                    entries_seen += 1
                    if entry.is_dir(follow_symlinks=False):
                        if not entry.name.startswith("."):
                            pending.append(Path(entry.path))
                    elif entry.name.endswith(".md"):
                        notes_seen += 1
                        try:
                            document, size = _read_note(Path(entry.path), root, byte_limit=524288 - read_bytes)
                            read_bytes += size
                        except (OSError, UnicodeError, ValueError):
                            result.incomplete = True
                            # A failed bounded read may have consumed the
                            # remaining byte budget. Never restart it per file.
                            return result
                        if document.title.strip() == heading:
                            result.document = document
                            return result
        except FileNotFoundError:
            if directory != root:
                result.incomplete = True
        except OSError:
            result.incomplete = True
    return result


def _proposed_identity(root: Path, heading: str, lookup: CaptureLookup) -> tuple[str, str, Path, bool]:
    if lookup.method == "unreadable_target":
        raise CaptureRejected(["existing deterministic target cannot be read safely; it was preserved"])
    existing = lookup.document
    if existing is not None:
        return existing.slug, existing.uri, existing.path, True
    ident = document_slug(heading)
    path = root / f"{ident}.md"
    return ident, uri_for("candidate", relative_posix(path, root)), path, False


def capture(
    *,
    title: str | None = None,
    content: str | None = None,
    layer: str = "candidate",
    config: ServiceConfig | None = None,
    source: Mapping[str, Any] | None = None,
    conditions: Mapping[str, Any] | None = None,
    evidence: Any = None,
    dry_run: bool = False,
    index: bool = True,
    _lookup: CaptureLookup | None = None,
) -> dict[str, Any]:
    """Save one candidate document. Required inputs are title and content."""

    if layer != "candidate":
        raise CaptureRefused(
            f"refusing to write into layer {layer!r}: capture only ever writes the "
            f"candidate layer. Public contribution is a separate step.",
            layer=layer,
        )
    if layer not in WRITABLE_LAYERS:
        raise CaptureRefused(f"layer {layer!r} is not writable", layer=layer)

    heading = (title or "").strip()
    body = (content or "").strip()
    problems: list[str] = []
    if not heading:
        problems.append("title is required")
    if not body:
        problems.append("content is required")
    if problems:
        raise CaptureRejected(problems)

    config = config or load_config()
    root = candidate_root(config, create=not dry_run)
    lookup = _lookup if _lookup is not None else lookup_capture(root, heading, config=config)
    ident, uri, path, updating = _proposed_identity(root, heading, lookup)
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "title": heading,
            "slug": ident,
            "uri": uri,
            "path": str(path),
            "layer": "candidate",
            "index": "skipped",
            "would_update": updating,
            "title_lookup": lookup.describe(),
        }

    document = save_document(
        root,
        layer="candidate",
        title=heading,
        content=body,
        path=path if updating else None,
        slug=None if updating else ident,
        source=source,
        conditions=conditions,
        evidence=evidence,
        captured_at=utc_now(),
    )

    backend = backend_for_config(config) if index else None
    ok, detail = backend.available() if backend is not None else (False, "indexing deferred to retrieval")
    indexed = False
    index_error = None
    if ok:
        try:
            # Keep the indexed text and ledger hash on the same snapshot. A
            # concurrent edit during upsert must remain visible to reconciliation.
            indexed_bytes = document.path.read_bytes()
            indexed_text = indexed_bytes.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            backend.upsert(document.uri, indexed_text, layer="candidate")
            indexed = True
            remember_document(config, document, indexed_bytes=indexed_bytes)
        except Exception as exc:  # noqa: BLE001 - Markdown is already saved
            index_error = f"{type(exc).__name__}: {exc}"
    else:
        index_error = detail
    payload = {
        "ok": True,
        "title": document.title,
        "slug": document.slug,
        "uri": document.uri,
        "ref": document.uri,
        "path": str(document.path),
        "layer": "candidate",
        "index": "ready" if indexed else "pending",
        "document": document.to_dict(),
        "title_lookup": lookup.describe(),
    }
    if document.source:
        payload["source"] = dict(document.source)
    if document.conditions:
        payload["conditions"] = dict(document.conditions)
    if not indexed:
        payload["index_detail"] = index_error
        payload["degraded"] = bool(index)
    from mindie_knowledge.publishing import queue_capture

    payload["contribution"] = queue_capture(config, document.path)
    return payload


def delete(
    ref: str,
    *,
    config: ServiceConfig | None = None,
) -> dict[str, Any]:
    """Delete one candidate document and drop it from the index."""

    config = config or load_config()
    root = candidate_root(config)
    target = None
    for path in iter_markdown_files(root):
        document = load_document(path, layer="candidate", root=root)
        if ref in {document.uri, document.slug, str(document.path), document.path.name}:
            target = document
            break
    if target is None:
        raise CaptureRejected([f"candidate not found: {ref}"])
    backend = backend_for_config(config)
    ok, _detail = backend.available()
    if ok:
        try:
            backend.delete(target.uri)
        except Exception:  # noqa: BLE001 - still delete the file
            pass
    delete_document(target.path)
    return {"ok": True, "deleted": target.uri, "path": str(target.path)}
