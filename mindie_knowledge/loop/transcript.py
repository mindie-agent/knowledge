"""Bounded incremental reader for native Codex JSONL task transcripts.

This is the per-Harness version adapter the design requires: it recognizes
only a structural signature whitelist of the Codex rollout JSONL format and
extracts only public task content — user messages, assistant public messages,
and bounded tool call input/output that explains the problem. Hidden
reasoning, system/developer instructions, credential fields and other tasks'
history are never extracted. An unrecognized format is reported honestly as
``unknown-format`` so the caller degrades to the already-present bounded
summary in the same attempt; there is no format guessing and no filesystem
scanning — the caller names exactly one file and one byte range.

Byte accounting is precise: every call reports the consumed range
``[start, end)`` and its SHA256 so the engine can durably reserve
``(file identity, start, end, digest)`` before any model call. File
replacement, truncation and oversized records stop or skip visibly instead of
silently rereading old history.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MAX_WINDOW = 256 * 1024          # one read window per increment
MAX_RECORD_BYTES = 64 * 1024     # one JSONL line; larger lines are skipped visibly
MAX_FIELD = 4096                 # characters per extracted text field
MAX_TOOL_FIELD = 1024            # characters per tool argument/output field
MAX_TEXT = 48 * 1024             # extracted increment text cap
MAX_RECORDS = 200                # extracted records per increment

_META_TYPES = {"session_meta"}
_ITEM_TYPE = "response_item"
_MESSAGE_ROLES = {"user": "user", "assistant": "assistant"}
_TEXT_CONTENT = {"input_text", "output_text"}


@dataclass(frozen=True)
class FileIdentity:
    path: str
    dev: int
    ino: int
    size: int
    mtime_ns: int

    @property
    def key(self) -> str:
        return f"{self.path}|{self.dev}:{self.ino}"


def identify(path) -> FileIdentity | None:
    """File identity for replacement/truncation detection; None if unreadable."""
    try:
        stat = os.stat(path)
    except (OSError, ValueError):
        return None
    return FileIdentity(
        path=str(Path(path).resolve(strict=False)),
        dev=stat.st_dev,
        ino=getattr(stat, "st_ino", 0),
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )


def same_file(identity: FileIdentity | None, current: FileIdentity | None) -> bool:
    """Best-effort same-file check; Windows inodes may be zero."""
    if identity is None or current is None:
        return False
    if identity.path != current.path:
        return False
    if identity.dev and current.dev and identity.dev != current.dev:
        return False
    if identity.ino and current.ino and identity.ino != current.ino:
        return False
    return True


def _clip(value, limit):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 12].rstrip() + "…[truncated]"
    return text


def _timestamp(record):
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _extract(record):
    """Whitelisted extraction from one parsed record.

    Returns ``(kind, text)`` for content that may leave the local filter, or
    None for records that are structurally recognized but not public content
    (reasoning, system/developer material, bookkeeping).
    """
    rtype = record.get("type")
    if rtype in _META_TYPES:
        return ("meta", None)
    if rtype != _ITEM_TYPE or not isinstance(record.get("payload"), dict):
        return None
    payload = record["payload"]
    ptype = payload.get("type")
    if ptype == "message":
        role = _MESSAGE_ROLES.get(payload.get("role"))
        if role is None:
            return None  # system/developer messages are never public content
        # A role alone never makes content public: assistant messages carry an
        # explicit channel and only the known public `final` channel may be
        # extracted. `analysis` (private reasoning) and any unknown channel are
        # excluded. User messages are the task's own public input.
        if role == "assistant":
            channel = payload.get("channel")
            if channel is not None and channel != "final":
                return None
        parts = []
        content = payload.get("content")
        if isinstance(content, list):
            for item in content:
                if (
                    isinstance(item, dict)
                    and item.get("type") in _TEXT_CONTENT
                    and isinstance(item.get("text"), str)
                    and (role == "user" or item.get("channel") in (None, "final"))
                ):
                    parts.append(item["text"])
        if not parts:
            return None
        return (role, _clip("\n".join(parts), MAX_FIELD))
    if ptype == "function_call":
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        return ("tool", f"{_clip(name, 120)} {_clip(payload.get('arguments', ''), MAX_TOOL_FIELD)}")
    if ptype == "function_call_output":
        return ("output", _clip(payload.get("output", ""), MAX_TOOL_FIELD))
    return None


def _session_of(record):
    if record.get("type") != "session_meta" or not isinstance(record.get("payload"), dict):
        return None
    ident = record["payload"].get("id")
    return ident if isinstance(ident, str) and ident else None


def read_increment(path, start, *, session_id=None, not_before=None,
                   expected: FileIdentity | None = None):
    """Read and filter the new byte region of one transcript.

    ``start`` is the previously consumed offset. ``not_before`` (Unix seconds)
    is the capture-authorization boundary: older records are consumed but not
    included, and if records without a usable timestamp had to be admitted the
    result is flagged ``timestamps_reliable=False`` so the caller can degrade
    to summary-only instead of backfilling unbounded history.
    """
    result = dict(
        status="ok", start=start, end=start, digest=hashlib.sha256(b"").hexdigest(),
        text="", records=0, skipped_records=0, oversize_records=0, partial=False,
        more=False, timestamps_reliable=True, session_match=None, coverage_note=None,
    )
    current = identify(path)
    if current is None:
        result.update(status="missing", coverage_note="transcript is unreadable")
        return result
    if type(start) is not int or start < 0:
        raise ValueError("start must be a nonnegative offset")
    if expected is not None and not same_file(expected, current):
        result.update(status="replaced", coverage_note="transcript was replaced")
        return result
    if current.size < start:
        result.update(status="replaced", coverage_note="transcript was truncated")
        return result
    if current.size == start:
        result["status"] = "unchanged"
        return result

    consumed = hashlib.sha256()
    lines = []           # (line_bytes, offset, length_with_newline)
    try:
        with open(current.path, "rb", buffering=0) as stream:
            stream.seek(start)
            window = stream.read(MAX_WINDOW + 1)
    except OSError:
        result.update(status="missing", coverage_note="transcript is unreadable")
        return result
    if not window:
        result["status"] = "unchanged"
        return result
    more = len(window) > MAX_WINDOW
    if more:
        window = window[:MAX_WINDOW]
    offset, cursor = start, start
    while cursor < start + len(window):
        newline = window.find(b"\n", cursor - start)
        if newline == -1:
            # Trailing partial record: never parsed, never consumed.
            if cursor == start and (more or current.size > start + len(window)):
                # One record larger than the whole window: consume it blind as
                # an oversize skip so the cursor cannot stall forever.
                blind = len(window)
                result["oversize_records"] += 1
                lines.append((None, cursor, blind))
                cursor += blind
            else:
                result["partial"] = True
            break
        # `newline` is window-relative; `cursor` is absolute. Keep all
        # accounting absolute so a nonzero start cannot loop or go negative.
        line_end = start + newline + 1
        raw_line = window[cursor - start : line_end - start]
        lines.append((raw_line, cursor, line_end - cursor))
        cursor = line_end
    if lines and lines[-1][0] is None:
        result["more"] = True
    elif more or current.size > cursor:
        result["more"] = True

    included, skipped = [], 0
    recognized = 0
    json_objects = 0
    end = start
    for raw_line, line_start, length in lines:
        chunk = (
            raw_line
            if raw_line is not None
            else window[line_start - start : line_start - start + length]
        )
        if raw_line is not None and len(raw_line) <= MAX_RECORD_BYTES:
            owner = None
            record = None
            stripped = raw_line.strip()
            if stripped:
                try:
                    record = json.loads(stripped.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    record = None
                if isinstance(record, dict) and isinstance(record.get("type"), str):
                    owner = _session_of(record)
            if owner is not None and session_id and owner != session_id:
                # Another task's file: stop before consuming the foreign record.
                result.update(
                    status="wrong-task",
                    session_match=False,
                    coverage_note="transcript belongs to another task",
                    end=end,
                    digest=consumed.hexdigest(),
                )
                return result
        end = line_start + length
        consumed.update(chunk)
        if raw_line is None:
            continue  # blind oversize skip accounted above
        if len(raw_line) > MAX_RECORD_BYTES:
            result["oversize_records"] += 1
            skipped += 1
            continue
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            skipped += 1
            continue
        if not isinstance(record, dict) or not isinstance(record.get("type"), str):
            skipped += 1
            continue
        json_objects += 1
        owner = _session_of(record)
        if owner is not None:
            recognized += 1
            result["session_match"] = True
            continue
        extracted = _extract(record)
        if extracted is None:
            skipped += 1
            continue
        recognized += 1
        kind, text = extracted
        if kind == "meta":
            continue
        if not_before is not None:
            stamp = _timestamp(record)
            if stamp is not None and stamp < not_before:
                skipped += 1
                continue
            if stamp is None:
                # No usable timestamp: never admit text that might predate the
                # authorization boundary; flag the whole increment unreliable.
                result["timestamps_reliable"] = False
                skipped += 1
                continue
        if len(included) >= MAX_RECORDS:
            skipped += 1
            continue
        included.append(f"[{kind}] {text}")

    text = "\n".join(included)
    if len(text.encode("utf-8")) > MAX_TEXT:
        raw_text = text.encode("utf-8")[: MAX_TEXT - 32]
        text = raw_text.decode("utf-8", "ignore") + "\n…[increment truncated]"
    result.update(
        end=end,
        digest=consumed.hexdigest(),
        text=text,
        records=len(included),
        skipped_records=skipped,
    )
    if end > start and recognized == 0 and json_objects > 0:
        result.update(
            status="unknown-format",
            text="",
            coverage_note="no recognized transcript signature; summary-only",
        )
    elif end > start and recognized == 0 and json_objects == 0 and skipped:
        result.update(
            status="unknown-format",
            text="",
            coverage_note="no parseable transcript records; summary-only",
        )
    elif end == start:
        result["status"] = "unchanged"
    return result
