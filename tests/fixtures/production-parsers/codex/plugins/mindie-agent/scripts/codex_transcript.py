"""Bounded incremental reader for native Codex JSONL task transcripts.

[Role]
Per-Harness transcript adapter (`history_source`, `capture_source`, `read_material`, `read_increment`, `identify`, `same_file`) for Codex rollout JSONL files.

[Invariants]
- Extracts only whitelisted public user/assistant messages; tools, hidden reasoning, injected system instructions, and foreign task records are excluded.
- Enforces strict profile containment (`sessions` / `archived_sessions` under `CODEX_HOME`) and exact `[start, end)` SHA-256 byte accounting with a 512-byte prefix anchor (`FileIdentity`).

[Timeouts & Locks]
- `read_material` enforces `max_scan_bytes=16 MiB` and `max_seconds=2.0s` per incremental scan window without holding locks.

[Side-Effect Policy]
- Purely read-only over the caller-specified transcript path; never scans directories or mutates files.
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


def _resolved_path(path, *, strict=False):
    # Parser modules are loaded directly by core, outside the adapter sys.path.
    from mindie_knowledge.filesystem import resolved_path
    return resolved_path(path, strict=strict)


def _path_identity(path):
    from mindie_knowledge.filesystem import path_identity
    return path_identity(path)


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
            fd = os.open(_resolved_path(self.path), os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except TimeoutError:
            raise
        except (OSError, ValueError):
            return None
        try:
            return hashlib.sha256(os.read(fd, count)).hexdigest()
        except TimeoutError:
            raise
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
        fd = os.open(_resolved_path(path), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return None
    try:
        stat = os.fstat(fd)
        anchor = os.read(fd, ANCHOR_BYTES)
    except TimeoutError:
        raise
    except OSError:
        return None
    finally:
        os.close(fd)
    return FileIdentity(
        path=str(_resolved_path(path)),
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
    if _path_identity(identity.path) != _path_identity(current.path):
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


def history_source(path):
    """Describe exactly one explicitly selected historical source, never scan.

    The caller must check current task admission and sharing before this read.
    Native session metadata supplies the historical owner and project scope;
    neither is inferred from a filename or the importing task's cwd.
    """
    path = _resolved_path(path)
    if not path.is_file():
        raise ValueError('history source must be a regular transcript file')
    with path.open('rb') as stream:
        info = os.fstat(stream.fileno())
        anchor = stream.read(ANCHOR_BYTES)
        stream.seek(0)
        raw = stream.readline(info.st_size)
    try:
        meta = json.loads(raw)
        if not isinstance(meta, dict):
            raise ValueError('invalid metadata')
        owner = _session_of(meta)
        scope = meta['payload']['cwd']
    except (ValueError, TypeError, KeyError):
        raise ValueError('history source requires native session metadata and project scope') from None
    if (not raw.endswith(b'\n') or not owner or not isinstance(scope, str)
            or not Path(scope).is_absolute()):
        raise ValueError('history source requires native session metadata and project scope')
    return dict(session_id=owner, project_root=str(_resolved_path(scope)),
                identity=FileIdentity(str(path), info.st_dev, info.st_ino,
                                      info.st_size, info.st_mtime_ns, len(anchor),
                                      hashlib.sha256(anchor).hexdigest()))


def capture_source(path, session_id):
    """Verify only the current event's native profile artifact and its header."""
    from capture_errors import TranscriptSourceError
    try:
        profile = _resolved_path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        selected = _resolved_path(path, strict=True)
    except TimeoutError:
        raise
    except (OSError, ValueError) as exc:
        raise TranscriptSourceError('transcript_unavailable',
                                    'native transcript path is unavailable') from exc
    profile_key, selected_key = Path(_path_identity(profile)), Path(_path_identity(selected))
    if not any(selected_key.is_relative_to(profile_key / name) for name in ('sessions', 'archived_sessions')):
        raise TranscriptSourceError('transcript_outside_profile',
                                    'capture transcript is outside the active native profile')
    try:
        if not selected.is_file():
            raise TranscriptSourceError('transcript_not_regular',
                                        'capture source must be a regular transcript file')
        with selected.open('rb') as stream:
            header = stream.readline(1024 * 1024 + 1)
    except TimeoutError:
        raise
    except OSError as exc:
        raise TranscriptSourceError('transcript_unavailable',
                                    'native transcript read is unavailable') from exc
    if len(header) > 1024 * 1024 or not header.endswith(b'\n'):
        raise TranscriptSourceError('transcript_header_invalid',
                                    'native task header is missing or exceeds its byte bound')
    try:
        meta = json.loads(header)
    except ValueError as exc:
        raise TranscriptSourceError('transcript_header_invalid',
                                    'native task header is invalid') from exc
    if not isinstance(meta, dict) or _session_of(meta) != session_id:
        raise TranscriptSourceError('transcript_identity_mismatch',
                                    'capture transcript belongs to another task')
    payload = meta['payload']
    scope = payload.get('cwd')
    created = _timestamp({'timestamp': payload.get('timestamp')}) or _timestamp(meta)
    if not isinstance(scope, str) or not Path(scope).is_absolute() or created is None:
        raise TranscriptSourceError('transcript_metadata_invalid',
                                    'native task scope or creation boundary is unavailable')
    return dict(session_id=session_id, project_root=str(_resolved_path(scope)),
                created_at=created, transcript_path=str(selected))


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
        with open(_resolved_path(path), "rb") as stream:
            stat = os.fstat(stream.fileno())
            anchor = stream.read(ANCHOR_BYTES)
            current = FileIdentity(str(_resolved_path(path)), stat.st_dev, stat.st_ino,
                                   stat.st_size, stat.st_mtime_ns, len(anchor),
                                   hashlib.sha256(anchor).hexdigest())
            result["identity"] = current.serialize()
            result["snapshot_size"] = stat.st_size
            if expected is not None:
                stream.seek(0)
                if (_path_identity(expected.path) != _path_identity(current.path) or expected.dev != stat.st_dev
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
                    # A corrupt complete record makes this page incomplete.
                    # Do not consume it or label later text as complete.
                    result.setdefault("discarded_records", []).append(
                        dict(start=offset, end=stream.tell(), reason="invalid record"))
                    result.update(status="invalid-record", end=start, text="",
                                  coverage_note="invalid complete transcript record")
                    return result
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
                        # An unknown authorization boundary is an error, not
                        # permission to skip an unknown amount of experience.
                        result.setdefault("discarded_records", []).append(
                            dict(start=offset, end=stream.tell(), reason="missing public timestamp"))
                        result.update(status="invalid-record", end=start, text="",
                                      coverage_note="missing public timestamp")
                        return result
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
    except TimeoutError:
        raise
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
