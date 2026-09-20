"""Neutral test double for the adapter-owned transcript parser interface.

Core owns NO native record parser (the real Codex parser is the published
adapter deliverable; the Kimi parser belongs to the Kimi adapter). This
double exists so engine component tests can exercise the adapter contract —
``FileIdentity``, ``identify``, ``read_material`` — over a trivial JSONL
shape: a ``{"type":"session_meta","payload":{"id": ...}}`` header followed by
``response_item`` message records. It implements the same byte-exact
increment contract (status/start/end/digest/more/identity) the engine relies
on; it is not a native format implementation.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ANCHOR_BYTES = 512
RECORD_LIMIT = 1024 * 1024


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

    def anchor_for(self, count):
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


def identify(path):
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


def _session_of(record):
    if record.get("type") != "session_meta" or not isinstance(record.get("payload"), dict):
        return None
    ident = record["payload"].get("id")
    return ident if isinstance(ident, str) and ident else None


def _extract(record):
    if record.get("type") != "response_item" or not isinstance(record.get("payload"), dict):
        return None
    payload = record["payload"]
    if payload.get("type") != "message" or payload.get("role") not in {"user", "assistant"}:
        return None
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    parts = [item["text"] for item in content if isinstance(item, dict)
             and item.get("type") in {"input_text", "output_text"}
             and isinstance(item.get("text"), str)]
    text = "\n".join(parts)
    return text if text.strip() else None


def read_material(path, start, *, session_id=None, not_before=None, expected=None,
                  max_scan_bytes=16777216, max_seconds=2.0, max_text_bytes=49152):
    if type(start) is not int or start < 0:
        raise ValueError("start must be a nonnegative offset")
    result = dict(status="ok", start=start, end=start,
                  digest=hashlib.sha256(b"").hexdigest(), text="", records=0,
                  skipped_records=0, oversize_records=0, partial=False,
                  more=False, timestamps_reliable=True, session_match=None,
                  coverage_note=None, coverage=[])
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
                str(Path(path).resolve()), stat.st_dev, stat.st_ino,
                stat.st_size, stat.st_mtime_ns, len(anchor),
                hashlib.sha256(anchor).hexdigest(),
            )
            result["identity"] = current.serialize()
            result["snapshot_size"] = stat.st_size
            if expected is not None:
                stream.seek(0)
                if (expected.path != current.path or expected.dev != stat.st_dev
                        or expected.ino != stat.st_ino or not expected.anchor_len
                        or hashlib.sha256(stream.read(expected.anchor_len)).hexdigest()
                        != expected.anchor_digest):
                    result.update(status="replaced",
                                  coverage_note="transcript changed before read")
                    return result
            if stat.st_size < start:
                result.update(status="replaced", coverage_note="transcript truncated")
                return result
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
                    result.update(status="wrong-task",
                                  coverage_note="transcript belongs to another task")
                    return result
            elif session_id:
                result.update(status="unknown-format",
                              coverage_note="task metadata unavailable; no public read")
                return result
            stream.seek(max(0, start - 1))
            middle = start > 0 and stream.read(1) != b"\n"
            stream.seek(start)
            end_limit = min(stat.st_size, start + max_scan_bytes)
            while stream.tell() < end_limit and time.monotonic() - begun < max_seconds:
                offset = stream.tell()
                raw = stream.readline(min(RECORD_LIMIT + 1, end_limit - offset))
                if not raw:
                    break
                complete = raw.endswith(b"\n")
                oversize = middle or len(raw) > RECORD_LIMIT
                if not complete and not oversize:
                    result["partial"] = offset + len(raw) == stat.st_size
                    break
                if oversize:
                    consumed.update(raw)
                    result["end"] = stream.tell()
                    result["oversize_records"] += 1
                    result["coverage"].append(dict(
                        start=offset, end=stream.tell(),
                        reason="oversize record skipped"))
                    middle = not complete
                    continue
                try:
                    record = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    record = None
                extracted = None
                if isinstance(record, dict):
                    if record.get("type") in {"session_meta", "response_item"}:
                        recognized += 1
                    text = _extract(record)
                    if text:
                        stamp = _timestamp(record)
                        if not_before is not None and (stamp is None or stamp < not_before):
                            if stamp is None:
                                result["timestamps_reliable"] = False
                        else:
                            size = len(text.encode()) + 2
                            if text_size + size > max_text_bytes:
                                break
                            included.append(text)
                            text_size += size
                            extracted = True
                if not extracted:
                    result["skipped_records"] += 1
                consumed.update(raw)
                result["end"] = stream.tell()
            result["more"] = result["end"] < stat.st_size
    except OSError:
        result.update(status="missing", coverage_note="transcript unreadable")
        return result
    result.update(digest=consumed.hexdigest(), text="\n\n".join(included),
                  records=len(included))
    if result["end"] == start:
        result["status"] = "unchanged"
    elif not recognized:
        result.update(status="unknown-format", text="",
                      coverage_note="no recognized native signature")
    return result
