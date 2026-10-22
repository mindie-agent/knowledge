"""[Role] Manifest, block and package validation for the canonical material store.

[Invariants]
- A package is accepted only when every block hash, byte bound, source range and
  navigation descriptor is internally consistent; extra/unlisted files fail.
- Manifests bind the exact entry header, material digest and ordered block ids;
  validation never rewrites or repairs a candidate in place.

[Timeouts & Locks]
- Pure parsing and hashing over caller-supplied bytes/text; no SQLite, lock,
  subprocess or network wait is performed here.

[Side-Effect Policy]
- No filesystem writes. Values returned to `MaterialStore` are the only result.
"""
from __future__ import annotations

import hashlib
import contextlib
import json
import os
import re
import sqlite3
import threading
from uuid import uuid4

import yaml

from ..loop import documents
from ..loop.locks import StartLock
from ..filesystem import filesystem_path

TASK_SCHEMA = "mindie-material-task/1"
BLOCK_SCHEMA = "mindie-material-block/1"
PACKAGE_SCHEMA = "mindie-material-package/1"
MAX_BLOCK_BYTES = 16 * 1024
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE = {"draft", "feed"}


class MaterialCleanupError(RuntimeError):
    """The current pointer committed, but retiring unused files failed."""

    committed = True


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _identity(value, name="identity"):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")
    return value


def _source(value):
    if value not in _SOURCE:
        raise ValueError("source must be draft or feed")
    return value


def _render(header, body):
    return "---\n" + yaml.safe_dump(header, sort_keys=True, allow_unicode=True, width=10**6) + "---\n\n" + body


def _parse(text):
    if not isinstance(text, str) or not text.startswith("---\n") or "\n---\n\n" not in text:
        raise ValueError("material file requires canonical YAML frontmatter")
    raw, body = text[4:].split("\n---\n\n", 1)
    header = yaml.load(raw, Loader=documents._UniqueKeyLoader)
    if not isinstance(header, dict) or _render(header, body) != text:
        raise ValueError("material file is not canonical Markdown")
    return header, body


def _navigation_body(header):
    lines = ["# " + header["entry"]["title"], "", header["navigation"], "", "## Materials", ""]
    for block in header["blocks"]:
        lines.append(f"- [{block['title']}](blocks/{block['block_id']}.md): {block['summary']}")
    return "\n".join(lines) + "\n"


def _material_digest(blocks, navigation, status):
    return _digest(dict(blocks=blocks, navigation=navigation, status=status))


def _entry_header(entry):
    return {key: value for key, value in entry.items() if key != "content"}


def _make_manifest(entry, blocks, navigation, status):
    entry = _entry_header(entry)
    entry["schema"] = "mindie-entry/3"
    entry["material_digest"] = _material_digest(blocks, navigation, status)
    entry["revision"] = documents.revision_of(entry)
    header = dict(schema=TASK_SCHEMA, task_id=entry["entry_id"], entry=entry,
                  blocks=blocks, navigation=navigation, status=status)
    return header, _render(header, _navigation_body(header))


def _parse_manifest(text, domain=None):
    header, body = _parse(text)
    return _validate_manifest(header, body, domain)


def _validate_manifest(header, body, domain=None):
    if set(header) != {"schema", "task_id", "entry", "blocks", "navigation", "status"}:
        raise ValueError("invalid material task fields")
    if header["schema"] != TASK_SCHEMA:
        raise ValueError("unsupported material task schema")
    _identity(header["task_id"], "task_id")
    entry = header["entry"]
    if not isinstance(entry, dict) or entry.get("entry_id") != header["task_id"]:
        raise ValueError("material task and entry identity differ")
    if entry.get("schema") != "mindie-entry/3" or "content" in entry:
        raise ValueError("task entry must be a mindie-entry/3 header")
    if domain is not None and entry.get("domain") != domain:
        raise ValueError("material package belongs to another domain")
    if not isinstance(header["navigation"], str) or not header["navigation"].strip():
        raise ValueError("navigation must be nonempty text")
    if header["status"] not in {"pending", "incomplete", "complete", "failed", "uncertain"}:
        raise ValueError("invalid material task status")
    blocks = header["blocks"]
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("material task requires ordered blocks")
    seen = set()
    for block in blocks:
        if not isinstance(block, dict) or set(block) != {"block_id", "sha256", "title", "summary", "source_range", "indexed"}:
            raise ValueError("invalid material block descriptor")
        _identity(block["block_id"], "block_id")
        _identity(block["sha256"], "block sha256")
        if block["block_id"] in seen:
            raise ValueError("duplicate material block identity")
        seen.add(block["block_id"])
        if not all(isinstance(block[k], str) for k in ("title", "summary")) or type(block["indexed"]) is not bool:
            raise ValueError("invalid block indexing metadata")
        if block["indexed"] and not all(block[k].strip() for k in ("title", "summary")):
            raise ValueError("indexed blocks require a title and summary")
        if not isinstance(block["source_range"], (str, dict, list)):
            raise ValueError("source_range must be a public source descriptor")
    if entry.get("material_digest") != _material_digest(blocks, header["navigation"], header["status"]):
        raise ValueError("material digest does not bind the manifest")
    if entry.get("revision") != documents.revision_of(entry):
        raise ValueError("entry revision does not bind the manifest")
    if body != _navigation_body(header):
        raise ValueError("navigation body differs from bound task metadata")
    # Validate public fields independently of the body, which is checked below.
    documents.validate(dict(entry, content="Material body validated separately."))
    return header


def _parse_block(text, descriptor):
    if _sha(text) != descriptor["sha256"]:
        raise ValueError("material block hash mismatch")
    header, body = _parse(text)
    expected = dict(schema=BLOCK_SCHEMA, block_id=descriptor["block_id"], source_range=descriptor["source_range"])
    if header != expected:
        raise ValueError("material block header differs from task descriptor")
    if not body or len(body.encode("utf-8")) > MAX_BLOCK_BYTES:
        raise ValueError("material block body is empty or exceeds its byte envelope")
    return body


def validate_package_files(files, domain=None):
    """Validate the complete canonical package; never trust paths from a peer."""
    from .file_source import FileContents
    if not isinstance(files, (dict, FileContents)) or "index.md" not in files:
        raise ValueError("material package requires index.md")
    header = _parse_manifest(files["index.md"], domain)
    expected = {"index.md"} | {f"blocks/{b['block_id']}.md" for b in header["blocks"]}
    if set(files) != expected:
        raise ValueError("package files differ from the exact manifest reference set")
    for block in header["blocks"]:
        _parse_block(files[f"blocks/{block['block_id']}.md"], block)
    return dict(schema=PACKAGE_SCHEMA, task_id=header["task_id"],
                revision=header["entry"]["revision"], package_hash=header["entry"]["revision"],
                files=files if isinstance(files, FileContents) else dict(files), entry=dict(header["entry"]),
                ready=all(block["indexed"] for block in header["blocks"]))


package_from_files = validate_package_files


def _split_text(text):
    """Bound a new manual document without dropping a byte or cutting UTF-8."""
    raw = text.encode("utf-8")
    while raw:
        end = min(len(raw), MAX_BLOCK_BYTES)
        if end < len(raw):
            line_end = raw.rfind(b"\n", 0, end)
            if line_end > end // 2:
                end = line_end + 1
            while end and (raw[end] & 0xC0) == 0x80:
                end -= 1
        yield raw[:end].decode("utf-8")
        raw = raw[end:]


