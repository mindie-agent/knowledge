"""Kimi Code public-record transcript parser (`wire.jsonl`).

[Role]
Reads bounded pages of Kimi's native `wire.jsonl` session transcript for the
shared knowledge capture pipeline, verifying file identity, session ownership,
fork timestamp boundaries (`state.createdAt`), and activation time boundaries.

[Invariants]
- Native probe timestamps (`time`, `metadata.created_at`, `state.createdAt`) are
  Unix milliseconds and are converted to seconds before boundary comparison.
- Fork exclusion uses the fork session's `state.createdAt` from `state.json`.
- Only `origin.kind == "user"` user messages and non-thinking assistant text
  parts are exported; tool inputs/results and thinking traces are excluded.

[Timeouts & Locks]
- Bounded by `max_scan_bytes` (default 16 MiB), `max_seconds` (default 2.0s),
  `max_text_bytes` (default 48 KiB), and `MAX_RECORDS` (200).

[Side-Effect Policy]
- Purely read-only over `wire.jsonl` and parent `state.json`.
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
            dict(dev=self.dev, ino=self.ino, anchor_len=self.anchor_len, anchor_digest=self.anchor_digest),
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
            return FileIdentity(path, int(data["dev"]), int(data["ino"]), 0, 0, anchor_len, anchor_digest)
        except (ValueError, KeyError, TypeError):
            return None


def identify(path) -> FileIdentity | None:
    try:
        with open(path, "rb") as stream:
            st = os.fstat(stream.fileno())
            anchor = stream.read(ANCHOR_BYTES)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return None
    return FileIdentity(
        str(Path(path).resolve()),
        st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
        len(anchor), hashlib.sha256(anchor).hexdigest(),
    )


def to_seconds(raw):
    if type(raw) not in (int, float) or raw <= 0:
        return None
    value = float(raw)
    return value / 1000.0 if value >= MS_THRESHOLD else value


def _session_in_path(path) -> str | None:
    parts = Path(path).resolve().parts
    if "sessions" not in parts:
        return None
    index = parts.index("sessions")
    return parts[index + 2] if index + 2 < len(parts) else None


def _timestamp(record):
    return to_seconds(record.get("time")) or to_seconds(record.get("created_at"))


def _text_parts(content):
    if not isinstance(content, list):
        return ""
    return "\n".join(
        item["text"] for item in content
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
    )


def _origin_kind(message):
    origin = message.get("origin") if isinstance(message, dict) else None
    kind = origin.get("kind") if isinstance(origin, dict) else None
    return kind if isinstance(kind, str) else None


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
            return ("user", text) if text.strip() else None
        if role == "assistant":
            text = _text_parts(message.get("content"))
            return ("assistant", text) if text.strip() else None
        return None
    if rtype == "context.append_loop_event":
        event = record.get("event")
        if isinstance(event, dict) and event.get("type") == "content.part":
            part = event.get("part")
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                text = part["text"].strip()
                return ("assistant", text) if text else None
    return None


def _fork_boundary(path) -> float | None:
    try:
        state_path = Path(path).resolve().parents[2] / "state.json"
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, IndexError):
        raise OSError("native session state unavailable; inherited material not read") from None
    if not isinstance(data, dict):
        raise OSError("native session state invalid; inherited material not read")
    if not (data.get("forkedFrom") or data.get("forked_from")):
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
    if max_scan_bytes <= 0 or max_seconds <= 0:
        raise ValueError("invalid scan budget")
    if scan_until is not None and (type(scan_until) is not int or scan_until < start):
        raise ValueError("scan_until must be an exact byte boundary at or after start")
    if max_text_bytes <= 0:
        raise ValueError("invalid text budget")
    boundary = to_seconds(not_before) if not_before is not None else None
    result = dict(
        status="ok", start=start, end=start,
        digest=hashlib.sha256(b"").hexdigest(), text="",
        records=0, skipped_records=0, oversize_records=0,
        partial=False, more=False, timestamps_reliable=True,
        session_match=None, coverage_note=None, coverage=[],
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
            st = os.fstat(stream.fileno())
            anchor = stream.read(ANCHOR_BYTES)
            current = FileIdentity(
                str(Path(path).resolve()),
                st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                len(anchor), hashlib.sha256(anchor).hexdigest(),
            )
            result["identity"] = current.serialize()
            result["snapshot_size"] = st.st_size
            if expected is not None:
                stream.seek(0)
                if (
                    expected.path != current.path
                    or expected.dev != st.st_dev
                    or expected.ino != st.st_ino
                    or not expected.anchor_len
                    or hashlib.sha256(stream.read(expected.anchor_len)).hexdigest() != expected.anchor_digest
                ):
                    result.update(status="replaced", coverage_note="transcript changed before read")
                    return result
            if st.st_size < start:
                result.update(status="replaced", coverage_note="transcript truncated")
                return result
            owner = _session_in_path(path)
            if owner:
                result["session_match"] = not session_id or owner == session_id
                if session_id and owner != session_id:
                    result.update(status="wrong-task", coverage_note="transcript belongs to another task")
                    return result
            elif session_id:
                result.update(status="unknown-format", coverage_note="task path identity unavailable; no public read")
                return result
            stream.seek(max(0, start - 1))
            if start > 0 and stream.read(1) != b"\n":
                result.update(status="invalid-boundary", coverage_note="cursor is not a whole-record boundary")
                return result
            stream.seek(start)
            end_limit = min(st.st_size, start + max_scan_bytes)
            if scan_until is not None:
                end_limit = min(end_limit, scan_until)
            while stream.tell() < end_limit and time.monotonic() - begun < max_seconds:
                offset = stream.tell()
                room = st.st_size - offset
                if scan_until is not None:
                    room = min(room, scan_until - offset)
                raw = stream.readline(room)
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    result["partial"] = offset + len(raw) == st.st_size
                    break
                try:
                    record = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    record = None
                if not isinstance(record, dict):
                    result.setdefault("discarded_records", []).append(
                        dict(start=offset, end=stream.tell(), reason="invalid record")
                    )
                    consumed.update(raw)
                    result["end"] = stream.tell()
                    result["skipped_records"] += 1
                    continue
                if record.get("type") in KNOWN:
                    recognized += 1
                stamp = _timestamp(record)
                extracted = _extract(record)
                if fork_time is not None and (stamp is None or stamp < fork_time):
                    extracted = None
                if extracted and extracted[1]:
                    if boundary is not None and stamp is None:
                        result.setdefault("discarded_records", []).append(
                            dict(start=offset, end=stream.tell(), reason="missing public timestamp")
                        )
                        consumed.update(raw)
                        result["end"] = stream.tell()
                        result["skipped_records"] += 1
                        continue
                    if boundary is not None and stamp < boundary:
                        extracted = None
                    else:
                        kind, text = extracted
                        text = text.replace("\r\n", "\n").replace("\r", "\n")
                        rendered = f"### {kind}\n{text}"
                        size = len(rendered.encode()) + 2
                        if included and (text_size + size > max_text_bytes or len(included) >= MAX_RECORDS):
                            break
                        included.append(rendered)
                        text_size += size
                if not extracted or not extracted[1]:
                    result["skipped_records"] += 1
                consumed.update(raw)
                result["end"] = stream.tell()
            result["more"] = result["end"] < st.st_size
    except TimeoutError:
        raise
    except OSError:
        result.update(status="missing", coverage_note="transcript unreadable")
        return result
    result.update(digest=consumed.hexdigest(), text="\n\n".join(included), records=len(included))
    if result["status"] != "ok":
        return result
    if result["end"] == start:
        if not result["partial"]:
            result["status"] = "unchanged"
    elif not recognized and start == 0 and not result["more"]:
        result.update(status="unknown-format", text="", coverage_note="no recognized native Kimi wire signature")
    elif not recognized:
        result["coverage_note"] = "page held no recognized records while bytes remain; format verdict deferred to the owning cursor"
    return result
