"""A local worklist for an independent knowledge maintainer.

Only mounted Markdown and its private sidecars are read. Findings are review
hints, never applicability verdicts or instructions to ordinary task agents.
The rebuildable cache is the only output written by this module.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from mindie_knowledge.distribution.errors import SwitchInProgress
from mindie_knowledge.distribution.manifest import atomic_write_json, read_json
from mindie_knowledge.distribution.sync import SwitchLock
from mindie_knowledge.markdown import LAYERS, MAX_REFERENCE_BYTES, MAX_METADATA_BYTES, meta_path, parse_markdown, read_bounded, uri_for
from mindie_knowledge.server.layers import ServiceConfig

CACHE_NAME = "knowledge-health.json"
SCHEMA = 1
_LINK = re.compile(r"(?<!!)\[[^\]\n]*\]\(<?([^\s<>]+)>?(?:\s+\"[^\"]*\")?\)")


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None  # Do not invent a timezone for a historical observation.
        return parsed.timestamp()
    except (ValueError, OverflowError):
        return None


def _date(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")


def _valid_report(report: Any, snapshot: Any) -> bool:
    if not isinstance(report, dict) or report.get("status") not in ("ok", "partial"):
        return False
    if not all(isinstance(report.get(key), str) for key in ("checked_at", "snapshot", "scope")):
        return False
    if report["snapshot"] != snapshot or not isinstance(report.get("reused"), bool):
        return False
    if not all(type(report.get(key)) is int and report[key] >= 0 for key in ("documents", "parsed")):
        return False
    findings = report.get("findings")
    return isinstance(findings, list) and all(
        isinstance(item, dict) and isinstance(item.get("kind"), str)
        and item.get("status") in ("unknown", "review_hint")
        for item in findings
    )


def _cache(path: Path | None) -> dict[str, Any]:
    try:
        cached = read_json(path) if path else None
    except (OSError, ValueError, UnicodeError):
        return {}
    if not isinstance(cached, dict) or cached.get("schema") != SCHEMA:
        return {}
    records = cached.get("records")
    if not isinstance(records, dict):
        return {}
    # Runtime state is an optimization. A malformed entry must not prevent
    # reading its authoritative Markdown or turn absent fields into facts.
    valid = {}
    for key, record in records.items():
        if not isinstance(record, dict):
            continue
        parsed, baseline = record.get("parsed"), record.get("baseline")
        if not isinstance(parsed, dict) or not isinstance(baseline, dict):
            continue
        if not all(isinstance(record.get(name), str) for name in ("path", "ref", "fingerprint", "raw_digest")):
            continue
        if not all(isinstance(parsed.get(name), str) for name in ("title", "age_basis")):
            continue
        recorded = parsed.get("recorded")
        if not isinstance(recorded, (int, float)) or not math.isfinite(recorded):
            continue
        try:
            _date(recorded)
        except (OSError, ValueError, OverflowError):
            continue
        if not isinstance(parsed.get("links"), list) or not all(isinstance(link, str) for link in parsed["links"]):
            continue
        if parsed.get("duplicate_key") is not None and not isinstance(parsed["duplicate_key"], str):
            continue
        if not all(isinstance(link, str) and isinstance(digest, str) for link, digest in baseline.items()):
            continue
        valid[key] = record
    # Do not reuse a saved report after dropping a corrupt observation.
    if len(valid) != len(records) or not _valid_report(cached.get("report"), cached.get("snapshot")):
        cached.pop("report", None)
    return {**cached, "records": valid}


def _parse(raw: bytes, metadata: bytes, modified: float) -> dict[str, Any]:
    text = raw.decode("utf-8")
    title, body = parse_markdown(text)
    meta = json.loads(metadata.decode("utf-8")) if metadata else {}
    if not isinstance(meta, dict):
        raise ValueError("metadata is not an object")
    recorded = _timestamp(meta.get("captured_at"))
    # Full normalized Markdown plus recorded conditions/status/evidence must
    # match. Similar prose under different conditions is not an exact duplicate.
    context = {key: meta.get(key) for key in ("conditions", "status", "evidence")}
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return {
        "title": str(meta.get("title") or title),
        "duplicate_key": _digest([normalized, context]) if body else None,
        "recorded": recorded if recorded is not None else modified,
        "age_basis": "captured_at" if recorded is not None else "file_modified_at",
        "links": sorted(set(_LINK.findall(body))),
    }


def _linked_path(note: Path, target: str, roots: list[Path]) -> Path | None:
    """Recognize only relative local links inside current mounted roots."""
    try:
        parsed = urlsplit(target)
        name = unquote(parsed.path)
        if parsed.scheme or parsed.netloc or not name or "\\" in name:
            return None
        relative = Path(name)
        if relative.is_absolute() or relative.drive:
            return None
        resolved = (note.parent / relative).resolve()
        return resolved if any(_inside(resolved, root) for root in roots) else None
    except (OSError, ValueError):
        return None


def inspect_knowledge(
    config: ServiceConfig, *, max_age_days: int = 180,
    now: float | None = None, refresh: bool = False,
) -> dict[str, Any]:
    """Inspect notes without retrieval, models, network calls or note edits.

    Unchanged snapshots reuse parsed records and the saved report. An age
    boundary still refreshes review hints. Relative links can reveal changes
    since first observation, not whether the linked claim was ever correct.
    ``refresh`` recomputes hints; it does not acknowledge or dismiss findings.
    """
    if isinstance(max_age_days, bool) or not isinstance(max_age_days, int) or max_age_days < 1:
        raise ValueError("max_age_days must be a positive integer")
    instant = time.time() if now is None else float(now)
    if not math.isfinite(instant):
        raise ValueError("now must be a finite timestamp")
    state_root = config.state_root
    cache_path = Path(state_root) / CACHE_NAME if state_root is not None else None
    lock = SwitchLock(cache_path.with_suffix(".lock")) if cache_path else None
    cache_error = None
    if lock:
        try:
            lock.acquire()
        except SwitchInProgress:
            cached = _cache(cache_path)
            report = cached.get("report")
            return {**(report if isinstance(report, dict) else {}), "status": "busy", "reused": True}
        except OSError as exc:
            # Read-only or unavailable state does not prevent a source review.
            # Do not write the cache without owning its lock.
            cache_error, cache_path, lock = str(exc), None, None
    try:
        result = _inspect(config, cache_path, max_age_days, instant, refresh)
        if cache_error:
            result["cache_error"] = cache_error
        return result
    finally:
        if lock:
            lock.release()


def _inspect(config: ServiceConfig, cache_path: Path | None, age: int, now: float, refresh: bool) -> dict[str, Any]:
    previous = _cache(cache_path)
    old_records = previous.get("records", {})
    records: dict[str, Any] = {}
    current: dict[str, Any] = {}
    findings: list[dict[str, Any]] = []
    snapshot: list[Any] = []
    roots = [Path(root) for layer in LAYERS for root in config.mount(layer).roots]
    readable: list[Path] = []
    parse_count = 0

    def unknown(kind: str, path: Path, reason: str, **extra: Any) -> None:
        findings.append({"kind": kind, "status": "unknown", "path": str(path), "reason": reason, **extra})

    for layer in LAYERS:
        mount = config.mount(layer)
        if mount.absent_reason and mount.roots and mount.present:
            unknown("source_unavailable", mount.roots[0], mount.absent_reason)
        for root in mount.roots:
            root = Path(root)
            snapshot.append([layer, str(root.resolve())])
            try:
                if not root.is_dir():
                    if (layer == "candidate" and mount.present and not mount.absent_reason
                            and not root.exists()
                            and not any(_inside(Path(record["path"]), root) for record in old_records.values())):
                        # Candidate storage is created by its first capture.
                        # A never-observed destination is an ordinary empty
                        # layer; disappearance after an observation is unknown.
                        continue
                    raise OSError("mounted directory is unavailable; its contents are unknown")
                # os.walk's onerror would be more involved; Path.walk is not
                # available on supported Python 3.11. Explicit scandir keeps
                # unreadable subdirectories visible instead of silently empty.
                paths = _markdown_paths(root)
                readable.append(root)
            except OSError as exc:
                unknown("source_unavailable", root, str(exc))
                continue
            for path in paths:
                key = f"{layer}:{path.absolute()}"
                ref = uri_for(layer, path.relative_to(root).as_posix())
                try:
                    if not _inside(path, root) or not _inside(meta_path(path), root):
                        raise ValueError("note or sidecar resolves outside its mounted directory")
                    raw = read_bounded(path, MAX_REFERENCE_BYTES)
                    sidecar = meta_path(path)
                    metadata = read_bounded(sidecar, MAX_METADATA_BYTES) if sidecar.is_file() else b""
                    modified = path.stat().st_mtime
                    fingerprint = _digest([hashlib.sha256(raw).hexdigest(), hashlib.sha256(metadata).hexdigest(), modified])
                    old = old_records.get(key, {})
                    if not isinstance(old, dict):
                        old = {}
                    if old.get("fingerprint") == fingerprint and isinstance(old.get("parsed"), dict):
                        parsed = old["parsed"]
                    else:
                        parsed = _parse(raw, metadata, modified)
                        parse_count += 1
                    raw_digest = hashlib.sha256(raw).hexdigest()
                    record = {"fingerprint": fingerprint, "raw_digest": raw_digest,
                              "path": str(path.resolve()), "ref": ref, "parsed": parsed,
                              "baseline": old.get("baseline", {}) if old.get("raw_digest") == raw_digest else {}}
                    records[key] = current[key] = record
                    snapshot.append([key, fingerprint])
                except (OSError, UnicodeError, ValueError) as exc:
                    unknown("note_unreadable", path, str(exc), ref=ref)

    # Preserve observations across missing/unreadable files. Absence is never
    # a deletion decision; no source or index record is removed here.
    for key, old in old_records.items():
        if key in current or not isinstance(old, dict) or not isinstance(old.get("path"), str):
            continue
        path = Path(old["path"])
        if any(_inside(path, root) for root in roots):
            records.setdefault(key, old)
            if any(_inside(path, root) for root in readable) and not path.exists():
                unknown("note_unavailable", path, "Previously observed note is unavailable; cause is unknown.", ref=old.get("ref"))

    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    source_digests: dict[Path, str] = {}
    for record in current.values():
        parsed, path = record["parsed"], Path(record["path"])
        ref = record["ref"]
        if parsed.get("duplicate_key"):
            groups[parsed["duplicate_key"]].append({"ref": ref, "path": str(path), "title": parsed["title"]})
        due = parsed["recorded"] + age * 86400
        if due <= now:
            findings.append({"kind": "age_review", "status": "review_hint", "ref": ref, "path": str(path),
                             "recorded_at": _date(parsed["recorded"]), "age_basis": parsed["age_basis"],
                             "reason": f"Recorded age exceeds {age} days; this does not establish that the note is outdated."})
        baseline = record["baseline"]
        for link in parsed["links"]:
            target = _linked_path(path, link, roots)
            if target is None:
                continue
            try:
                # Shared links need one byte read per pass. Always re-read in
                # the next pass: stat-only reuse would miss preserved-mtime edits.
                if target not in source_digests:
                    source_digests[target] = _file_digest(target)
                source_digest = source_digests[target]
                snapshot.append([str(target), source_digest])
            except (OSError, ValueError) as exc:
                unknown("linked_source_unavailable", target, str(exc), ref=ref, link=link)
                continue
            observed = baseline.setdefault(link, source_digest)
            if observed != source_digest:
                findings.append({"kind": "linked_source_changed", "status": "review_hint", "ref": ref,
                                 "path": str(path), "link": link, "source_path": str(target),
                                 "reason": "Linked local bytes changed since this note was first observed; relevance needs review."})
    for duplicates in groups.values():
        # Overlapping mounts of the same file are not duplicate notes.
        members = {item["path"]: item for item in duplicates}
        if len(members) > 1:
            findings.append({"kind": "exact_duplicate", "status": "review_hint", "documents": list(members.values()),
                             "reason": "Markdown and recorded conditions, status and evidence match; no merge or deletion was performed."})
    snapshot_id = _digest([snapshot, findings, age])
    cached_report = previous.get("report")
    if not refresh and previous.get("snapshot") == snapshot_id and isinstance(cached_report, dict):
        return {**cached_report, "reused": True, "parsed": parse_count}
    report = {"status": "partial" if any(item["status"] == "unknown" for item in findings) else "ok",
              "checked_at": _date(now), "snapshot": snapshot_id, "documents": len(current),
              "findings": findings, "reused": False, "parsed": parse_count,
              "scope": "mounted Markdown and relative links within mounted roots; external and unrecorded sources are not checked"}
    if cache_path:
        try:
            atomic_write_json(cache_path, {"schema": SCHEMA, "snapshot": snapshot_id, "records": records,
                                           "report": report})
        except OSError as exc:
            report["cache_error"] = str(exc)
    return report


def _file_digest(path: Path) -> str:
    return hashlib.sha256(read_bounded(path, MAX_REFERENCE_BYTES)).hexdigest()


def _markdown_paths(root: Path) -> list[Path]:
    import os

    paths: list[Path] = []

    def failed(error: OSError) -> None:
        raise error

    for directory, _names, filenames in os.walk(root, followlinks=False, onerror=failed):
        paths.extend(Path(directory) / name for name in filenames if name.endswith(".md"))
    return sorted(paths)
