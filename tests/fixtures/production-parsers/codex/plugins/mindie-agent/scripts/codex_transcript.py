"""Bounded incremental reader for native Codex JSONL task transcripts.

This is the per-Harness version adapter the design requires: it recognizes
only a structural signature whitelist of the Codex rollout JSONL format and
extracts user messages and public assistant progress/final messages. Tools,
hidden reasoning, injected system/developer instructions and other tasks'
history are excluded. An unrecognized format is reported as ``unknown-format``;
the public-transcript path never substitutes a model-written summary for it.
There is no filesystem scanning: the caller names one file and byte range.

Byte accounting is precise: every call reports the consumed range
``[start, end)`` and its SHA256 so the engine can durably reserve
``(file identity, start, end, digest)`` with the saved body. Public messages are
never clipped to a model envelope. Replacement and unsupported oversized input
stop visibly instead of silently consuming or rereading history.
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
MAX_TEXT = 4 * 1024 * 1024       # page target, never clips a public message
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
INJECTED_PREFIXES = (
    "<recommended_plugins>", "<environment_context>",
    "# AGENTS.md instructions", "<permissions instructions>",
    "<skills_instructions>", "<app-context>",
)
ATTACHMENTS = {"input_image": "Image", "image": "Image", "input_audio": "Audio", "input_file": "File"}


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
            parts = [payload['text']]
        else:
            content = payload.get("content")
            if not isinstance(content, list):
                return None
            parts = []
            for item in content:
                if not isinstance(item, dict) or item.get('channel') not in PUBLIC:
                    continue
                if item.get('type') in _TEXT_CONTENT and isinstance(item.get('text'), str):
                    parts.append(item['text'])
                elif item.get('type') in ATTACHMENTS:
                    parts.append('[' + ATTACHMENTS[item['type']] + ' attachment omitted]')
        if role == 'user':
            parts = [part for part in parts if not part.lstrip().startswith(INJECTED_PREFIXES)]
        text = "\n".join(parts)
        text = re.sub(r"<oai-mem-citation>.*?</oai-mem-citation>", "", text, flags=re.S)
        phase = payload.get("phase") or payload.get("channel")
        label = role + (":" + phase if role == "assistant" and phase else "")
        return (label, text) if text.strip() else None
    return None


def _session_of(record):
    if record.get("type") != "session_meta" or not isinstance(record.get("payload"), dict):
        return None
    ident = record["payload"].get("id")
    return ident if isinstance(ident, str) and ident else None


def read_material(path, start, *, session_id=None, not_before=None, expected=None,
                  max_scan_bytes=16777216, max_seconds=2.0, max_text_bytes=MAX_TEXT,
                  scan_until=None):
    """Scan noise without model work, stopping BEFORE the next public record
    would exceed the text envelope. Every consumed byte is hashed exactly.
    Large records and field truncations are explicit coverage gaps, never
    interpreted as evidence. All validation and reads use the same descriptor.
    """
    if type(start) is not int or start < 0:
        raise ValueError("start must be a nonnegative offset")
    if max_scan_bytes <= 0 or max_seconds <= 0:
        raise ValueError("invalid scan budget")
    if scan_until is not None and (
        type(scan_until) is not int or scan_until < start
    ):
        raise ValueError("scan_until must be an exact byte boundary at or after start")
    if max_text_bytes <= 0:
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
            # identifies ownership; no foreign public material is returned.
            stream.seek(0)
            header = stream.readline()
            try:
                meta = json.loads(header)
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
            if middle:
                result.update(status="invalid-boundary", coverage_note="cursor is not on a whole-record boundary")
                return result
            stream.seek(start)
            end_limit = min(stat.st_size, start + max_scan_bytes)
            if scan_until is not None:
                end_limit = min(end_limit, scan_until)
            while stream.tell() < end_limit and time.monotonic()-begun < max_seconds:
                offset = stream.tell()
                # A page target limits work between records. One complete
                # public message may exceed it; never clip or consume half.
                room = stat.st_size - offset
                if scan_until is not None:
                    room = min(room, scan_until - offset)
                raw = stream.readline(room)
                if not raw:
                    break
                complete = raw.endswith(b"\n")
                if not complete:
                    # A budget boundary or incomplete append must not consume
                    # a record that can be completed on the next call.
                    result["partial"] = offset+len(raw) == stat.st_size
                    break
                try:
                    record = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    record = None
                if not isinstance(record, dict):
                    result.update(status="invalid-record", coverage_note="invalid complete JSONL record; not consumed")
                    result["coverage"].append(dict(start=offset, end=stream.tell(), reason="invalid record"))
                    break
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
                    if not_before is not None and stamp is None:
                        result.update(status='invalid-record', timestamps_reliable=False,
                                      coverage_note='public message timestamp unavailable; not consumed')
                        result['coverage'].append(dict(start=offset, end=stream.tell(), reason='missing public timestamp'))
                        break
                    if not_before is not None and stamp < not_before:
                        extracted = None
                    else:
                        kind, text = extracted
                        text = text.replace("\r\n", "\n").replace("\r", "\n")
                        label = f"### {kind}"
                        rendered = label + "\n" + text
                        size = len(rendered.encode()) + 2
                        if included and (text_size + size > max_text_bytes or len(included) >= MAX_RECORDS):
                            break  # cursor remains before this unadmitted record
                        included.append(rendered)
                        text_size += size
                if not extracted or not extracted[1]:
                    result["skipped_records"] += 1
                consumed.update(raw)
                result["end"] = stream.tell()
            result["more"] = result["end"] < stat.st_size
    except OSError:
        result.update(status="missing",coverage_note="transcript unreadable")
        return result
    result.update(digest=consumed.hexdigest(),text="\n\n".join(included),records=len(included))
    established = expected is not None or result.get("session_match") is True
    if result["status"] != "ok":
        return result
    if result["end"] == start:
        result["status"] = "unchanged"
    elif not recognized and not (
        established and result["more"] and result["end"] > result["start"]
    ):
        result.update(status="unknown-format",text="",coverage_note="no recognized native signature")
    return result


def read_increment(path, start, *, session_id=None, not_before=None, expected=None):
    """Compatibility entry point for callers explicitly requesting a small scan."""
    return read_material(path, start, session_id=session_id, not_before=not_before,
                         expected=expected, max_scan_bytes=MAX_WINDOW)
