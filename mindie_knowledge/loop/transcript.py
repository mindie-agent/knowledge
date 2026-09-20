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
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MAX_WINDOW = 256 * 1024          # one read window per increment
MAX_TEXT = 48 * 1024             # extracted increment text cap
MAX_RECORDS = 200                # extracted records per increment

_TEXT_CONTENT = {"input_text", "output_text"}


ANCHOR_BYTES = 512


@dataclass(frozen=True)
class FileIdentity:
    """Replacement/truncation detection that does not trust inode reuse.

    The anchor is the digest of the file's first bytes (at most
    ``ANCHOR_BYTES``), captured together with the stat on the SAME opened
    handle. A later open re-reads exactly that recorded span: an unlinked and
    recreated file that reuses the inode (Linux), or an in-place rewrite that
    changes the prefix, fails the anchor comparison; an ordinary append keeps
    it. mtime is never compared (append changes it) and birthtime is never
    guessed.
    """

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

    def anchor_for(self, count):
        """SHA256 of the first ``count`` bytes read on one fresh handle."""
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except (OSError, ValueError):
            return None
        try:
            return hashlib.sha256(os.read(fd, count)).hexdigest()
        except OSError:
            return None
        finally:
            os.close(fd)

    def serialize(self) -> str:
        return json.dumps(
            dict(dev=self.dev, ino=self.ino, anchor_len=self.anchor_len,
                 anchor_digest=self.anchor_digest),
            sort_keys=True, separators=(",", ":"),
        )

    @staticmethod
    def unserialize(text, path):
        """Rebuild a persisted identity; None when malformed (fail closed)."""
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
    """Stat + anchor read bound to one opened handle; None if unreadable."""
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
        path=str(Path(path).resolve(strict=False)),
        dev=stat.st_dev,
        ino=getattr(stat, "st_ino", 0),
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        anchor_len=len(anchor),
        anchor_digest=hashlib.sha256(anchor).hexdigest(),
    )


def same_file(identity: FileIdentity | None, current: FileIdentity | None) -> bool:
    """Same persisted file? Requires the recorded prefix anchor to match."""
    if identity is None or current is None:
        return False
    if identity.path != current.path:
        return False
    if identity.dev and current.dev and identity.dev != current.dev:
        return False
    if identity.ino and current.ino and identity.ino != current.ino:
        return False
    if not identity.anchor_len or not identity.anchor_digest:
        return False  # a persisted identity without an anchor fails closed
    return current.anchor_for(identity.anchor_len) == identity.anchor_digest


def _clip(value, limit):
    """UTF-8 byte cap preserving both the cause and the last observed outcome."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    marker = "\n…[field truncated; head and tail retained]…\n"
    budget = max(0, limit - len(marker.encode()))
    head = budget * 2 // 3
    return raw[:head].decode("utf-8", "ignore") + marker + raw[-(budget-head):].decode("utf-8", "ignore")


def _timestamp(record):
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


PUBLIC = {None, "final", "final_answer", "commentary"}
KNOWN = {"session_meta", "turn_context", "response_item", "event_msg", "compacted"}
RECORD_LIMIT = 1024 * 1024


def _extract(record):
    """Structural public-field allowlist; never recursively traverse a payload."""
    rtype = record.get("type")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    if rtype == "session_meta":
        return ("meta", None)
    # Native event wrappers duplicate response_item messages. They are recognized
    # as bookkeeping below, but not extracted twice into the model material.
    if rtype != "response_item":
        return None
    if payload.get("channel") not in PUBLIC or payload.get("phase") not in PUBLIC:
        return None
    ptype = payload.get("type")
    if ptype in {"message", "agent_message"}:
        role = payload.get("role", "assistant" if ptype == "agent_message" else None)
        if role not in {"user", "assistant"}:
            return None
        if ptype == "agent_message" and isinstance(payload.get("text"), str):
            return (role, _clip(payload["text"], 12288))
        content = payload.get("content")
        if not isinstance(content, list):
            return None
        parts = [item["text"] for item in content if isinstance(item, dict)
                 and item.get("type") in _TEXT_CONTENT
                 and isinstance(item.get("text"), str)
                 and item.get("channel") in PUBLIC]
        text = "\n".join(parts)
        if role == "user" and text.lstrip().startswith((
            "<recommended_plugins>", "<environment_context>",
            "# AGENTS.md instructions", "<permissions instructions>",
            "<skills_instructions>", "<app-context>",
        )):
            return None
        text = re.sub(r"<oai-mem-citation>.*?</oai-mem-citation>", "", text, flags=re.S)
        return (role, _clip(text, 12288)) if text.strip() else None
    if ptype in {"function_call", "custom_tool_call"}:
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        value = payload.get("input", "") if ptype == "custom_tool_call" else payload.get("arguments", "")
        call = _clip(payload.get("call_id", ""), 256)
        return ("tool", f"{_clip(name, 120)} call_id={call} {_clip(value, 8192)}")
    if ptype in {"function_call_output", "custom_tool_call_output"}:
        call = _clip(payload.get("call_id", ""), 256)
        return ("output", f"call_id={call} {_clip(payload.get('output', ''), 8192)}")
    return None


def _session_of(record):
    if record.get("type") != "session_meta" or not isinstance(record.get("payload"), dict):
        return None
    ident = record["payload"].get("id")
    return ident if isinstance(ident, str) and ident else None


def read_material(path, start, *, session_id=None, not_before=None, expected=None,
                  max_scan_bytes=16777216, max_seconds=2.0, max_text_bytes=49152):
    """Scan noise without model work, stopping BEFORE the next public record
    would exceed the text envelope. Every consumed byte is hashed exactly.
    Large records and field truncations are explicit coverage gaps, never
    interpreted as evidence. All validation and reads use the same descriptor.
    """
    if type(start) is not int or start < 0:
        raise ValueError("start must be a nonnegative offset")
    if not 1024 <= max_scan_bytes <= 64*1024*1024 or not 0 < max_seconds <= 30:
        raise ValueError("invalid scan budget")
    if not 16384 <= max_text_bytes <= MAX_TEXT:
        raise ValueError("invalid text budget")
    result = dict(status="ok", start=start, end=start, digest=hashlib.sha256(b"").hexdigest(),
                  text="", records=0, skipped_records=0, oversize_records=0,
                  partial=False, more=False, timestamps_reliable=True,
                  session_match=None, coverage_note=None, coverage=[])
    consumed = hashlib.sha256()
    recognized = 0
    included = []
    text_size = 0
    turn = None
    begun = time.monotonic()
    try:
        with open(path, "rb") as stream:
            stat = os.fstat(stream.fileno())
            anchor = stream.read(ANCHOR_BYTES)
            current = FileIdentity(str(Path(path).resolve()), stat.st_dev, stat.st_ino,
                                   stat.st_size, stat.st_mtime_ns, len(anchor),
                                   hashlib.sha256(anchor).hexdigest())
            result["identity"] = current.serialize()
            result["snapshot_size"] = stat.st_size
            if expected is not None:
                stream.seek(0)
                if (expected.path != current.path or expected.dev != stat.st_dev
                    or expected.ino != stat.st_ino or not expected.anchor_len
                    or hashlib.sha256(stream.read(expected.anchor_len)).hexdigest() != expected.anchor_digest):
                    result.update(status="replaced", coverage_note="transcript changed before read")
                    return result
            if stat.st_size < start:
                result.update(status="replaced", coverage_note="transcript truncated")
                return result
            # Task identity is checked even at a nonzero cursor. The metadata
            # line alone is bounded; no foreign public material is returned.
            stream.seek(0)
            header = stream.readline(RECORD_LIMIT + 1)
            try:
                meta = json.loads(header) if len(header) <= RECORD_LIMIT else {}
                owner = _session_of(meta) if isinstance(meta, dict) else None
            except ValueError:
                owner = None
            if owner:
                recognized += 1
                result["session_match"] = not session_id or owner == session_id
                if not result["session_match"]:
                    result.update(status="wrong-task", coverage_note="transcript belongs to another task")
                    return result
            elif session_id:
                result.update(status="unknown-format", coverage_note="task metadata unavailable; no public read")
                return result
            parent = meta.get("payload", {}).get("forked_from_id") if isinstance(meta, dict) else None
            fork_time = None
            if parent:
                fork_time = _timestamp({"timestamp": meta["payload"].get("timestamp")})
                if fork_time is None:
                    result.update(status="unknown-format",coverage_note="fork timestamp unavailable; inherited material not read")
                    return result
            stream.seek(max(0, start-1))
            middle = start > 0 and stream.read(1) != b"\n"
            stream.seek(start)
            end_limit = min(stat.st_size, start + max_scan_bytes)
            while stream.tell() < end_limit and time.monotonic()-begun < max_seconds:
                offset = stream.tell()
                room = end_limit - offset
                raw = stream.readline(min(RECORD_LIMIT+1, room))
                if not raw:
                    break
                complete = raw.endswith(b"\n")
                oversize = (middle or len(raw) > RECORD_LIMIT or
                            (not complete and offset == start and len(raw) == max_scan_bytes
                             and end_limit < stat.st_size))
                if not complete and not oversize:
                    # A budget boundary or incomplete append must not consume
                    # a record that fits our record bound on the next call.
                    result["partial"] = offset+len(raw) == stat.st_size
                    break
                if oversize:
                    consumed.update(raw)
                    result["end"] = stream.tell()
                    result["oversize_records"] += 1
                    result["coverage"].append(dict(start=offset,end=stream.tell(),reason="oversize record skipped"))
                    middle = not complete
                    continue
                try:
                    record = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    record = None
                extracted = None
                if isinstance(record, dict):
                    if record.get("type") in KNOWN:
                        recognized += 1
                    foreign = _session_of(record)
                    if foreign and session_id and foreign != session_id:
                        if foreign != parent:
                            result.update(status="wrong-task",text="",session_match=False)
                            return result
                    if record.get("type") == "turn_context":
                        turn = (record.get("payload") or {}).get("turn_id")
                    extracted = _extract(record)
                    if fork_time is not None:
                        stamp = _timestamp(record)
                        if stamp is None or stamp < fork_time:
                            extracted = None  # inherited parent context, never a new experience
                if extracted and extracted[1]:
                    stamp = _timestamp(record)
                    if not_before is not None and (stamp is None or stamp < not_before):
                        if stamp is None:
                            result["timestamps_reliable"] = False
                        extracted = None
                    else:
                        kind, text = extracted
                        label = f"[{kind} timestamp={record.get('timestamp','unknown')} turn={turn or 'unknown'} bytes={offset}:{stream.tell()}]"
                        rendered = label + "\n" + text
                        size = len(rendered.encode()) + 2
                        if text_size + size > max_text_bytes or len(included) >= MAX_RECORDS:
                            break  # cursor remains before this unadmitted record
                        included.append(rendered)
                        text_size += size
                        if "[field truncated;" in text:
                            result["coverage"].append(dict(start=offset,end=stream.tell(),reason="field head/tail truncation"))
                if not extracted or not extracted[1]:
                    result["skipped_records"] += 1
                consumed.update(raw)
                result["end"] = stream.tell()
            result["more"] = result["end"] < stat.st_size
    except OSError:
        result.update(status="missing",coverage_note="transcript unreadable")
        return result
    result.update(digest=consumed.hexdigest(),text="\n\n".join(included),records=len(included))
    if result["end"] == start:
        result["status"] = "unchanged"
    elif not recognized:
        result.update(status="unknown-format",text="",coverage_note="no recognized native signature")
    return result


def read_increment(path, start, *, session_id=None, not_before=None, expected=None):
    """Compatibility entry point for callers explicitly requesting a small scan."""
    return read_material(path, start, session_id=session_id, not_before=not_before,
                         expected=expected, max_scan_bytes=MAX_WINDOW)
