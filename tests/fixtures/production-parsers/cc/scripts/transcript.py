"""Claude Code public-record parser (session JSONL). Adapter-owned.

Public records are top-level type user/assistant with camel-case sessionId
and ISO timestamps. Thinking, redacted_thinking, isMeta, sidechains,
attachments, system, queue-operation, atis-latch and last-prompt are
excluded. The caller names exactly one file supplied by a trusted hook.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MAX_WINDOW = 256 * 1024
MAX_TEXT = 48 * 1024
MAX_RECORDS = 200
ANCHOR_BYTES = 512
RECORD_LIMIT = 1024 * 1024
MS_THRESHOLD = 1e12
PUBLIC_TYPES = {"user", "assistant"}
SKIP_TYPES = {
    "attachment",
    "system",
    "queue-operation",
    "atis-latch",
    "last-prompt",
}
SKIP_BLOCKS = {"thinking", "redacted_thinking"}
COMMAND_BLOCK = re.compile(
    r"<command-[a-zA-Z0-9_-]+>.*?</command-[a-zA-Z0-9_-]+>",
    re.DOTALL,
)
SESSION_FILE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl\Z"
)


@dataclass(frozen=True)
class FileIdentity:
    path: str
    dev: int
    ino: int
    size: int
    mtime_ns: int
    anchor_len: int = 0
    anchor_digest: str = ""

    @property
    def key(self) -> str:
        return f"{self.path}|{self.dev}:{self.ino}"

    def serialize(self) -> str:
        return json.dumps(
            dict(
                dev=self.dev,
                ino=self.ino,
                anchor_len=self.anchor_len,
                anchor_digest=self.anchor_digest,
            ),
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def unserialize(text, path):
        try:
            data = json.loads(text)
            anchor_len = data["anchor_len"]
            anchor_digest = data["anchor_digest"]
            if (
                type(anchor_len) is not int
                or not 0 < anchor_len <= ANCHOR_BYTES
                or not isinstance(anchor_digest, str)
                or len(anchor_digest) != 64
            ):
                return None
            return FileIdentity(
                path, int(data["dev"]), int(data["ino"]), 0, 0,
                anchor_len, anchor_digest,
            )
        except (ValueError, KeyError, TypeError):
            return None


def identify(path) -> FileIdentity | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except (OSError, ValueError):
        return None
    try:
        stat = os.fstat(fd)
        anchor = os.read(fd, ANCHOR_BYTES)
    except OSError:
        return None
    finally:
        os.close(fd)
    return FileIdentity(
        str(Path(path).resolve()),
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        len(anchor),
        hashlib.sha256(anchor).hexdigest(),
    )


def to_seconds(raw):
    if type(raw) in (int, float) and raw > 0:
        value = float(raw)
        if value >= MS_THRESHOLD:
            value = value / 1000.0
        return value
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def _clip(text, limit):
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False) if text is not None else ""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    head = raw[: limit // 2].decode("utf-8", "ignore")
    tail = raw[-limit // 2 :].decode("utf-8", "ignore")
    return f"{head}\n[field truncated; {len(raw)} bytes]\n{tail}"


def _session_in_path(path) -> str | None:
    match = SESSION_FILE.search(Path(path).name)
    return match.group(1) if match else None


def _block_text(block):
    if not isinstance(block, dict):
        return None
    btype = block.get("type")
    if btype in SKIP_BLOCKS:
        return None
    if btype == "text" and isinstance(block.get("text"), str):
        return ("text", block["text"])
    if btype == "tool_use":
        name = block.get("name") if isinstance(block.get("name"), str) else ""
        ident = block.get("id") if isinstance(block.get("id"), str) else ""
        args = block.get("input")
        return (
            "tool",
            f"{_clip(name, 120)} call_id={_clip(ident, 256)} {_clip(args, 8192)}",
        )
    if btype == "tool_result":
        ident = block.get("tool_use_id") if isinstance(block.get("tool_use_id"), str) else ""
        content = block.get("content")
        output = ""
        if isinstance(content, str):
            output = content
        elif isinstance(content, list):
            chunks = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
            output = "\n".join(chunks)
        return ("output", f"call_id={_clip(ident, 256)} {_clip(output, 8192)}")
    return None


def _message_content(record):
    message = record.get("message")
    if isinstance(message, dict):
        return message.get("content")
    return record.get("content")


def _is_command_only(text: str) -> bool:
    leftover = COMMAND_BLOCK.sub("", text).strip()
    return not leftover


def _extract(record):
    rtype = record.get("type")
    if rtype not in PUBLIC_TYPES:
        return None
    if record.get("isSidechain") is True:
        return None
    if record.get("isMeta") is True:
        return None
    content = _message_content(record)
    if isinstance(content, str):
        text = content.strip()
        if not text or _is_command_only(text):
            return None
        kind = "user" if rtype == "user" else "assistant"
        return (kind, _clip(text, 12288))
    if not isinstance(content, list):
        return None
    chunks = []
    kind = "user" if rtype == "user" else "assistant"
    for block in content:
        extracted = _block_text(block)
        if extracted is None:
            continue
        bkind, text = extracted
        if not isinstance(text, str) or not text.strip():
            continue
        if bkind == "tool":
            chunks.append(("tool", text))
        elif bkind == "output":
            chunks.append(("output", text))
        else:
            chunks.append((kind, text))
    if not chunks:
        return None
    if len(chunks) == 1:
        return chunks[0]
    joined = "\n".join(item[1] for item in chunks)
    return (chunks[0][0], _clip(joined, 12288))


def read_material(
    path,
    start,
    *,
    session_id=None,
    not_before=None,
    expected=None,
    max_scan_bytes=16777216,
    max_seconds=2.0,
    max_text_bytes=49152,
    scan_until=None,
):
    if type(start) is not int or start < 0:
        raise ValueError("start must be a nonnegative offset")
    if not 1024 <= max_scan_bytes <= 64 * 1024 * 1024 or not 0 < max_seconds <= 30:
        raise ValueError("invalid scan budget")
    if scan_until is not None and (
        type(scan_until) is not int or scan_until < start
    ):
        raise ValueError("scan_until must be an exact byte boundary at or after start")
    if not 16384 <= max_text_bytes <= MAX_TEXT:
        raise ValueError("invalid text budget")
    boundary = to_seconds(not_before) if not_before is not None else None
    result = dict(
        status="ok",
        start=start,
        end=start,
        digest=hashlib.sha256(b"").hexdigest(),
        text="",
        records=0,
        skipped_records=0,
        oversize_records=0,
        partial=False,
        more=False,
        timestamps_reliable=True,
        session_match=None,
        coverage_note=None,
        coverage=[],
    )
    consumed = hashlib.sha256()
    recognized = 0
    included = []
    text_size = 0
    begun = time.monotonic()
    try:
        with open(path, "rb") as stream:
            stat = os.fstat(stream.fileno())
            anchor = stream.read(ANCHOR_BYTES)
            current = FileIdentity(
                str(Path(path).resolve()),
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                len(anchor),
                hashlib.sha256(anchor).hexdigest(),
            )
            result["identity"] = current.serialize()
            result["snapshot_size"] = stat.st_size
            if expected is not None:
                stream.seek(0)
                if (
                    expected.path != current.path
                    or expected.dev != stat.st_dev
                    or expected.ino != stat.st_ino
                    or not expected.anchor_len
                    or hashlib.sha256(stream.read(expected.anchor_len)).hexdigest()
                    != expected.anchor_digest
                ):
                    result.update(
                        status="replaced",
                        coverage_note="transcript changed before read",
                    )
                    return result
            if stat.st_size < start:
                result.update(status="replaced", coverage_note="transcript truncated")
                return result
            owner = _session_in_path(path)
            if owner:
                result["session_match"] = not session_id or owner == session_id
                if session_id and owner != session_id:
                    result.update(
                        status="wrong-task",
                        coverage_note="transcript belongs to another task",
                    )
                    return result
            elif session_id:
                result.update(
                    status="unknown-format",
                    coverage_note="task path identity unavailable; no public read",
                )
                return result
            stream.seek(max(0, start - 1))
            middle = start > 0 and stream.read(1) != b"\n"
            stream.seek(start)
            end_limit = min(stat.st_size, start + max_scan_bytes)
            if scan_until is not None:
                end_limit = min(end_limit, scan_until)
            while stream.tell() < end_limit and time.monotonic() - begun < max_seconds:
                offset = stream.tell()
                room = end_limit - offset
                raw = stream.readline(min(RECORD_LIMIT + 1, room))
                if not raw:
                    break
                complete = raw.endswith(b"\n")
                oversize = middle or len(raw) > RECORD_LIMIT or (
                    not complete
                    and offset == start
                    and len(raw) == max_scan_bytes
                    and end_limit < stat.st_size
                )
                if not complete and not oversize:
                    result["partial"] = offset + len(raw) == stat.st_size
                    break
                if oversize:
                    consumed.update(raw)
                    result["end"] = stream.tell()
                    result["oversize_records"] += 1
                    result["coverage"].append(
                        dict(start=offset, end=stream.tell(), reason="oversize record skipped")
                    )
                    middle = not complete
                    continue
                try:
                    record = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    record = None
                extracted = None
                stamp = None
                if isinstance(record, dict):
                    rtype = record.get("type")
                    if rtype in PUBLIC_TYPES or rtype in SKIP_TYPES:
                        recognized += 1
                    stamp = to_seconds(record.get("timestamp"))
                    rec_session = record.get("sessionId") or record.get("session_id")
                    if session_id and rec_session and rec_session != session_id:
                        extracted = None
                    else:
                        extracted = _extract(record)
                if extracted and extracted[1]:
                    if boundary is not None and (stamp is None or stamp < boundary):
                        if stamp is None:
                            result["timestamps_reliable"] = False
                        extracted = None
                    else:
                        kind, text = extracted
                        label = (
                            f"[{kind} timestamp={stamp if stamp is not None else 'unknown'} "
                            f"bytes={offset}:{stream.tell()}]"
                        )
                        rendered = label + "\n" + text
                        size = len(rendered.encode()) + 2
                        if text_size + size > max_text_bytes or len(included) >= MAX_RECORDS:
                            break
                        included.append(rendered)
                        text_size += size
                        if "[field truncated;" in text:
                            result["coverage"].append(
                                dict(
                                    start=offset,
                                    end=stream.tell(),
                                    reason="field head/tail truncation",
                                )
                            )
                if not extracted or not extracted[1]:
                    result["skipped_records"] += 1
                consumed.update(raw)
                result["end"] = stream.tell()
            result["more"] = result["end"] < stat.st_size
    except OSError:
        result.update(status="missing", coverage_note="transcript unreadable")
        return result
    result.update(
        digest=consumed.hexdigest(),
        text="\n\n".join(included),
        records=len(included),
    )
    established = expected is not None or result.get("session_match") is True
    if result["end"] == start:
        result["status"] = "unchanged"
    elif not recognized and not (
        established and result["more"] and result["end"] > result["start"]
    ):
        result.update(
            status="unknown-format",
            text="",
            coverage_note="no recognized native Claude transcript signature",
        )
    return result
