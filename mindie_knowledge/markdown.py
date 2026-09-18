"""Minimal Markdown knowledge documents.

A document is a title plus non-empty body. Optional known fields (source,
conditions, evidence, status) live in a sibling ``.meta.json`` so the Markdown
file stays ordinary prose. Unknown values are omitted, never invented.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

LAYERS = ("shared", "project", "candidate")
URI_ROOT = "viking://resources"
SHARED_BOOTSTRAP_URI = f"{URI_ROOT}/shared/bootstrap"
_TITLE_DIGEST_LEN = 12
MAX_REFERENCE_BYTES = 4 * 1024 * 1024
MAX_METADATA_BYTES = 256 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def slugify(text: str) -> str:
    """ASCII filename helper. Not a unique document identity."""

    slug = _SLUG_UNSAFE.sub("-", (text or "").strip().lower()).strip("-")
    return slug[:80] or "captured-entry"


def title_digest(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()[:_TITLE_DIGEST_LEN]


def document_slug(title: str) -> str:
    """Stable, collision-resistant identity derived only from the title."""

    heading = (title or "").strip()
    digest = title_digest(heading)
    readable = slugify(heading)
    if readable == "captured-entry":
        return f"entry-{digest}"
    return f"{readable}-{digest}"


def relative_posix(path: Path, root: Path | None = None) -> str:
    if root is not None:
        try:
            return path.resolve().relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            try:
                return Path(os.path.relpath(path, root)).as_posix()
            except ValueError:
                pass
    return Path(path).name


def uri_for(layer: str, relative: str) -> str:
    name = str(relative or "").replace("\\", "/").lstrip("/")
    if not name.endswith(".md"):
        name = f"{name}.md"
    root = SHARED_BOOTSTRAP_URI if layer == "shared" else f"{URI_ROOT}/{layer}"
    return f"{root}/{name}"


def layer_from_uri(uri: str) -> str | None:
    text = str(uri or "")
    prefix = URI_ROOT + "/"
    if not text.startswith(prefix):
        return None
    rest = text[len(prefix) :]
    layer = rest.split("/", 1)[0]
    return layer if layer in LAYERS else None


def parse_markdown(text: str) -> tuple[str, str]:
    """Return ``(title, body)``. Title is the first ATX heading, else the first line."""

    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    match = _TITLE_RE.search(raw)
    if match:
        title = match.group(1).strip()
        body = (raw[: match.start()] + raw[match.end() :]).strip()
        return title, body
    body = raw.strip()
    title = next((line.strip() for line in raw.splitlines() if line.strip()), "untitled")
    return title, body


def render_markdown(title: str, content: str) -> str:
    heading = (title or "").strip() or "untitled"
    body = (content or "").strip()
    return f"# {heading}\n\n{body}\n" if body else f"# {heading}\n"


def normalized_sha256(text: str) -> str:
    """Content identity shared by generated enrichment and quoted evidence."""
    return hashlib.sha256(text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")).hexdigest()


def retrieval_metadata(value: Any, text: str) -> dict[str, Any]:
    """Ignore stale generated suggestions without discarding their diagnosis."""
    if not isinstance(value, Mapping):
        return {}
    actual = normalized_sha256(text)
    if value.get("source_sha256") != actual:
        return {"source_sha256": value.get("source_sha256"), "current_sha256": actual,
                "ignored": "source_changed" if value.get("source_sha256") else "source_hash_missing"}
    aliases = []
    for alias in value.get("aliases", []) if isinstance(value.get("aliases"), list) else []:
        if isinstance(alias, str) and alias.strip():
            aliases.append(alias.strip())
        elif isinstance(alias, Mapping) and isinstance(alias.get("text"), str) and alias["text"].strip():
            item = {"text": alias["text"].strip(), "relation": str(alias.get("relation") or "related")}
            scope = alias.get("scope")
            if isinstance(scope, str) and scope.strip():
                item["scope"] = scope.strip()
            elif isinstance(scope, list):
                # A malformed explicit scope must not become an unscoped
                # suggestion merely because none of its entries are usable.
                if any(not isinstance(part, str) or not part.strip() for part in scope):
                    continue
                item["scope"] = [part.strip() for part in scope]
            aliases.append(item)
    topics = value.get("topics", [])
    return {"source_sha256": actual, "aliases": aliases,
            "topics": sorted({topic.strip() for topic in topics if isinstance(topic, str) and topic.strip()}) if isinstance(topics, list) else []}


def retrieval_aliases(document: Document) -> list[str]:
    """Source-validated alias text whose optional topic scope covers the note."""
    if document.retrieval.get("ignored"):
        return []
    topics = {topic.casefold() for topic in document.retrieval.get("topics", [])}
    result = []
    for alias in document.retrieval.get("aliases", []):
        if isinstance(alias, str):
            result.append(alias)
        elif isinstance(alias, Mapping):
            scope = alias.get("scope") or []
            scope = [scope] if isinstance(scope, str) else scope
            if not scope or topics.intersection(part.casefold() for part in scope):
                result.append(alias["text"])
    return result


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _clean_mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    out: dict[str, Any] = {}
    for key, item in value.items():
        name = str(key).strip()
        if not name:
            continue
        if item is None:
            continue
        if isinstance(item, str):
            text = item.strip()
            if not text or text.lower() == "unknown":
                continue
            out[name] = text
            continue
        out[name] = item
    return out or None


@dataclass
class Document:
    layer: str
    title: str
    content: str
    slug: str
    path: Path
    uri: str
    status: str | None = None
    source: dict[str, Any] | None = None
    conditions: dict[str, str] = field(default_factory=dict)
    evidence: Any = None
    captured_at: str | None = None
    raw_text: str = field(default="", repr=False)
    retrieval: dict[str, Any] = field(default_factory=dict)

    def excerpt(self, limit: int = 240) -> str:
        text = " ".join((self.content or "").split())
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "layer": self.layer,
            "title": self.title,
            "content": self.content,
            "slug": self.slug,
            "path": str(self.path),
            "uri": self.uri,
            "ref": self.uri,
        }
        if self.status:
            payload["status"] = self.status
        if self.source:
            payload["source"] = dict(self.source)
        if self.conditions:
            payload["conditions"] = dict(self.conditions)
        if self.evidence is not None:
            payload["evidence"] = self.evidence
        if self.captured_at:
            payload["captured_at"] = self.captured_at
        if self.retrieval:
            payload["retrieval"] = dict(self.retrieval)
        return payload


def meta_path(markdown_path: Path) -> Path:
    return markdown_path.with_suffix(".meta.json")


def read_bounded(path: Path, limit: int) -> bytes:
    """Bound actual reads even if a file grows after a directory observation."""
    with path.open("rb") as stream:
        raw = stream.read(max(0, limit) + 1)
    if len(raw) > limit:
        raise ValueError(f"reference source exceeds the {limit}-byte read budget")
    return raw


def load_document(path: Path, *, layer: str, root: Path | None = None,
                  max_bytes: int | None = None, max_metadata_bytes: int | None = None) -> Document:
    text = path.read_text(encoding="utf-8") if max_bytes is None else read_bounded(path, max_bytes).decode("utf-8")
    meta: dict[str, Any] = {}
    sidecar = meta_path(path)
    if sidecar.is_file():
        try:
            encoded = sidecar.read_text(encoding="utf-8") if max_metadata_bytes is None else read_bounded(sidecar, max_metadata_bytes).decode("utf-8")
            loaded = json.loads(encoded)
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, Mapping):
            meta = dict(loaded)
    return document_from_text(text, path=path, layer=layer, root=root, metadata=meta)


def document_from_text(text: str, *, path: Path, layer: str, root: Path | None = None,
                       metadata: Mapping[str, Any] | None = None) -> Document:
    """Parse an already observed snapshot without reading its source again."""
    title, content = parse_markdown(text)
    rel = relative_posix(path, root)
    rel_slug = rel[:-3] if rel.lower().endswith(".md") else rel
    meta = dict(metadata or {})
    conditions: dict[str, str] = {}
    raw_conditions = meta.get("conditions") if isinstance(meta.get("conditions"), Mapping) else {}
    for key, value in raw_conditions.items():
        text_value = str(value).strip()
        if text_value and text_value.lower() != "unknown":
            conditions[str(key)] = text_value
    source = _clean_mapping(meta.get("source"))
    return Document(
        layer=layer,
        title=str(meta.get("title") or title),
        content=content,
        slug=str(meta.get("slug") or rel_slug),
        path=path,
        # Files mounted as shared are the local bootstrap source. Old sidecar
        # identities must not place them inside a versioned imported pack.
        uri=uri_for(layer, rel) if layer == "shared" else str(meta.get("uri") or uri_for(layer, rel)),
        status=str(meta["status"]) if meta.get("status") else None,
        source=source,
        conditions=conditions,
        evidence=meta.get("evidence"),
        captured_at=meta.get("captured_at") if isinstance(meta.get("captured_at"), str) else None,
        raw_text=text,
        retrieval=retrieval_metadata(meta.get("retrieval"), text),
    )


def find_by_title(root: Path, title: str, *, layer: str) -> Document | None:
    heading = (title or "").strip()
    if not heading or not root.is_dir():
        return None
    matches: list[Document] = []
    for path in iter_markdown_files(root):
        try:
            document = load_document(path, layer=layer, root=root)
        except (OSError, UnicodeDecodeError):
            continue
        if document.title.strip() == heading:
            matches.append(document)
    if not matches:
        return None
    wanted = document_slug(heading)
    for document in matches:
        if document.slug == wanted or document.path.stem == wanted:
            return document
    return matches[0]


def save_document(
    root: Path,
    *,
    layer: str,
    title: str,
    content: str,
    slug: str | None = None,
    path: Path | None = None,
    status: str | None = None,
    source: Mapping[str, Any] | None = None,
    conditions: Mapping[str, Any] | None = None,
    evidence: Any = None,
    captured_at: str | None = None,
    retrieval: Mapping[str, Any] | None = None,
) -> Document:
    heading = (title or "").strip()
    body = (content or "").strip()
    if not heading:
        raise ValueError("title is required")
    if not body:
        raise ValueError("content is required")
    if path is not None:
        target = Path(path)
        ident = target.stem
    else:
        ident = slug or document_slug(heading)
        target = root / f"{ident}.md"
        if target.is_file():
            existing = load_document(target, layer=layer, root=root, max_bytes=MAX_REFERENCE_BYTES,
                                     max_metadata_bytes=MAX_METADATA_BYTES)
            if existing.title.strip() != heading:
                ident = f"{ident}-{title_digest(heading + ident)}"
                target = root / f"{ident}.md"
                if target.exists() or target.is_symlink():
                    if target.is_symlink() or not target.is_file():
                        raise ValueError("alternate capture target is occupied; existing state was preserved")
                    alternate = load_document(target, layer=layer, root=root, max_bytes=MAX_REFERENCE_BYTES,
                                              max_metadata_bytes=MAX_METADATA_BYTES)
                    if alternate.title.strip() != heading:
                        raise ValueError("alternate capture target is occupied; existing state was preserved")
    previous: dict[str, Any] = {}
    if meta_path(target).is_file():
        meta_path(target).resolve().relative_to(root.resolve())
        try:
            loaded = json.loads(read_bounded(meta_path(target), MAX_METADATA_BYTES).decode("utf-8"))
            if isinstance(loaded, dict):
                previous = loaded
        except (OSError, json.JSONDecodeError):
            pass
    rendered = render_markdown(heading, body)
    _atomic_write_text(target, rendered)
    cleaned_source = _clean_mapping(source)
    cleaned_conditions: dict[str, str] = {}
    if isinstance(conditions, Mapping):
        for key, value in conditions.items():
            text_value = str(value).strip()
            if text_value and text_value.lower() != "unknown":
                cleaned_conditions[str(key)] = text_value
    rel = relative_posix(target, root)
    meta: dict[str, Any] = {
        **previous,
        "slug": ident,
        "title": heading,
        "layer": layer,
        "uri": uri_for(layer, rel),
        "captured_at": captured_at or utc_now(),
    }
    if status is not None:
        meta["status"] = status
    if source is not None:
        if cleaned_source:
            meta["source"] = cleaned_source
        else:
            meta.pop("source", None)
    if conditions is not None:
        if cleaned_conditions:
            meta["conditions"] = cleaned_conditions
        else:
            meta.pop("conditions", None)
    if evidence is not None:
        meta["evidence"] = evidence
    if retrieval is not None:
        meta["retrieval"] = dict(retrieval)
    _atomic_write_text(meta_path(target), json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    return document_from_text(rendered.replace("\r\n", "\n").replace("\r", "\n"),
                              path=target, layer=layer, root=root, metadata=meta)


def delete_document(path: Path) -> None:
    sidecar = meta_path(path)
    path.unlink(missing_ok=True)
    sidecar.unlink(missing_ok=True)


def iter_markdown_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*.md") if path.is_file())
