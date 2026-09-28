"""Kimi Code public-record parser (wire.jsonl). Adapter-owned.

Native probe times (`time`, `metadata.created_at`, `state.createdAt`) are
Unix milliseconds. Core capture boundaries are seconds. Convert before
filtering. Fork exclusion uses the fork session's state.createdAt only;
copied metadata.created_at is the source time and must not prove a boundary.
User role material is origin.kind == 'user' only (injection/system excluded).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

MAX_TEXT = 48 * 1024
MAX_RECORDS = 200
ANCHOR_BYTES = 512
RECORD_LIMIT = 1024 * 1024
MS_THRESHOLD = 1e12
KNOWN = {
    "metadata",
    "context.append_message",
    "context.append_loop_event",
    "context.undo",
    "context.apply_compaction",
    "context.clear",
    "turn.prompt",
    "turn.ended",
    "turn.steer",
    "turn.cancel",
}
USER_ORIGINS = {"user"}


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
    if type(raw) not in (int, float) or raw <= 0:
        return None
    value = float(raw)
    if value >= MS_THRESHOLD:
        value = value / 1000.0
    return value


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
    parts = Path(path).resolve().parts
    if "sessions" not in parts:
        return None
    index = parts.index("sessions")
    if index + 2 < len(parts):
        return parts[index + 2]
    return None


def _timestamp(record):
    return to_seconds(record.get("time")) or to_seconds(record.get("created_at"))


def _record_turn_id(record):
    """This record's own native turnId (loop events carry it on the event).
    Body-evidence label only; never carried across records, never guessed."""
    ident = record.get("turnId")
    if type(ident) is int and 0 <= ident < 10**15:
        return ident
    event = record.get("event")
    if isinstance(event, dict):
        ident = event.get("turnId")
        if type(ident) is int and 0 <= ident < 10**15:
            return ident
    return None


def _text_parts(content):
    if not isinstance(content, list):
        return ""
    chunks = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            chunks.append(item["text"])
    return "\n".join(chunks)


def _origin_kind(message):
    origin = message.get("origin") if isinstance(message, dict) else None
    if isinstance(origin, dict):
        kind = origin.get("kind")
        return kind if isinstance(kind, str) else None
    return None


def _extract(record):
    rtype = record.get("type")
    if rtype == "metadata":
        return ("meta", None)
    if rtype == "context.append_message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        if role == "user":
            if _origin_kind(message) not in USER_ORIGINS:
                return None
            text = _text_parts(message.get("content"))
            return ("user", _clip(text, 12288)) if text.strip() else None
        if role == "assistant":
            text = _text_parts(message.get("content"))
            return ("assistant", _clip(text, 12288)) if text.strip() else None
        return None
    if rtype == "context.append_loop_event":
        event = record.get("event")
        if not isinstance(event, dict):
            return None
        etype = event.get("type")
        if etype == "content.part":
            part = event.get("part")
            if not isinstance(part, dict):
                return None
            if part.get("type") == "think":
                return None
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                text = part["text"].strip()
                return ("assistant", _clip(text, 12288)) if text else None
            return None
        if etype == "tool.call":
            name = event.get("name")
            if not isinstance(name, str) or not name.strip():
                return None
            call = _clip(event.get("toolCallId") or event.get("tool_call_id") or "", 256)
            args = event.get("args")
            return ("tool", f"{_clip(name, 120)} call_id={call} {_clip(args, 8192)}")
        if etype == "tool.result":
            call = _clip(event.get("toolCallId") or event.get("tool_call_id") or "", 256)
            result = event.get("result")
            output = ""
            if isinstance(result, dict):
                output = result.get("output")
            elif isinstance(result, str):
                output = result
            return ("output", f"call_id={call} {_clip(output, 8192)}")
        return None
    return None


def _fork_boundary(path) -> float | None:
    """Inherited material ends at the fork session's createdAt (seconds)."""
    try:
        state_path = Path(path).resolve().parents[2] / "state.json"
        data = json.loads(state_path.read_text())
    except (OSError, ValueError, IndexError):
        return None
    if not isinstance(data, dict):
        return None
    parent = data.get("forkedFrom") or data.get("forked_from")
    if not parent:
        return None
    created = to_seconds(data.get("createdAt") or data.get("created_at"))
    if created is None:
        raise ValueError("forked session lacks state.createdAt; inherited material not read")
    return created


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
        fork_time = _fork_boundary(path)
    except ValueError as exc:
        result.update(status="unknown-format", coverage_note=str(exc), text="")
        return result
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
                    if record.get("type") in KNOWN:
                        recognized += 1
                    stamp = _timestamp(record)
                    extracted = _extract(record)
                    if fork_time is not None and (stamp is None or stamp < fork_time):
                        extracted = None
                if extracted and extracted[1]:
                    if boundary is not None and (stamp is None or stamp < boundary):
                        if stamp is None:
                            result["timestamps_reliable"] = False
                        extracted = None
                    else:
                        kind, text = extracted
                        turn_id = _record_turn_id(record)
                        label = (
                            f"[{kind} timestamp={stamp if stamp is not None else 'unknown'} "
                            f"turn={turn_id if turn_id is not None else 'unknown'} "
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
    if result["end"] == start:
        # A trailing half-written record is pending material, not proof of
        # no-new-material: report partial/more and leave the cursor unmoved
        # for a finite deferred read. Only a truly empty page is unchanged.
        if not result["partial"]:
            result["status"] = "unchanged"
    elif not recognized and start == 0 and not result["more"]:
        # Whole-file unknown-format only with whole-file evidence: a first
        # page scanned to EOF without one recognized record. A positive
        # offset is not proof of recognition either, so continuation pages
        # never judge; the cumulative verdict belongs to the core cursor.
        result.update(
            status="unknown-format",
            text="",
            coverage_note="no recognized native Kimi wire signature",
        )
    elif not recognized:
        result["coverage_note"] = (
            "page held no recognized records while bytes remain; "
            "format verdict deferred to the owning cursor"
        )
    return result
