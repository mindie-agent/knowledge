"""Prepare a bounded public feed from independent-agent Markdown outputs.

Only the returned, verified generation is publishable. The output container
also holds private lock diagnostics and must never be recursively published.
No Git, network, scheduler or model execution belongs to this module.
Preparation uses the existing pattern-based public profile; it does not decide
whether arbitrary prose is private. Automated inputs must be public-only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

from mindie_knowledge import redact
from mindie_knowledge.curation import _aliases
from mindie_knowledge.distribution.errors import DistributionError, SwitchInProgress
from mindie_knowledge.distribution.manifest import atomic_write_json
from mindie_knowledge.distribution.references import prepare_reference, safe_relative
from mindie_knowledge.distribution.sync import SwitchLock
from mindie_knowledge.markdown import MAX_METADATA_BYTES, MAX_REFERENCE_BYTES, meta_path, normalized_sha256, read_bounded, retrieval_metadata

SCHEMA = "vaws-curation-export/1"
MAX_FILES = 1024
MAX_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = MAX_FILES * 8
_SHA = re.compile(r"[0-9a-f]{64}")
_ROW_KEYS = {"path", "size", "sha256", "source_sha256", "input_sha256", "metadata_size", "metadata_sha256"}


class ExportError(ValueError):
    """The selected source or a prepared generation is incomplete or changed."""


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _inside(root: Path, relative: str) -> Path:
    if not safe_relative(relative):
        raise ExportError("export paths must be ordinary relative names")
    path = root / relative
    path.resolve().relative_to(root.resolve())
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink():
            raise ExportError("selected sources and prepared files cannot traverse symlinks")
    return path


def _walk(root: Path, *, deadline: float, maximum: int = MAX_ENTRIES):
    pending, visited = [root], 0
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                visited += 1
                if visited > maximum or time.monotonic() > deadline:
                    raise ExportError("export exceeded its directory-entry or time budget")
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                else:
                    yield path


def _selected(source: Path, includes: Sequence[str], deadline: float) -> list[Path]:
    if not source.is_dir():
        raise ExportError("selected source root is unavailable")
    paths: dict[str, Path] = {}
    for relative in includes:
        directory = _inside(source, relative)
        if not directory.exists():
            continue
        if not directory.is_dir():
            raise ExportError("each selected export path must be a Markdown subdirectory")
        for path in _walk(directory, deadline=deadline):
            if path.is_symlink():
                raise ExportError("selected Markdown directories cannot contain symlinks")
            if path.suffix.casefold() != ".md":
                continue
            name = path.relative_to(source).as_posix()
            key = name.casefold()
            if key in paths:
                raise ExportError("selected source paths overlap or collide across platforms")
            paths[key] = _inside(source, name)
            if len(paths) > MAX_FILES:
                raise ExportError(f"export exceeds the {MAX_FILES}-document budget")
    return sorted(paths.values(), key=lambda path: path.relative_to(source).as_posix())


def _snapshot(rows: list[dict], includes: list[str], profile: str) -> str:
    return _hash(_json({"files": rows, "includes": includes, "redaction_profile": profile}))


def verify_export(prepared_root: Path, *, manifest_sha256: str) -> dict[str, Any]:
    """Verify the exact prepared file set against an externally retained hash.

    A self-modified manifest cannot authorize changed output. Callers retain
    the returned manifest hash or obtain it through ``current_export``.
    """
    root = Path(prepared_root)
    if root.is_symlink() or not isinstance(manifest_sha256, str) or not _SHA.fullmatch(manifest_sha256):
        raise ExportError("prepared export needs a safe root and its retained manifest hash")
    raw_manifest = read_bounded(_inside(root, "prepared.json"), MAX_MANIFEST_BYTES)
    if _hash(raw_manifest) != manifest_sha256:
        raise ExportError("prepared manifest changed after export")
    manifest = json.loads(raw_manifest)
    if not isinstance(manifest, dict) or set(manifest) != {"schema", "redaction_profile", "includes", "snapshot", "previous_snapshot", "files", "changes"}:
        raise ExportError("prepared manifest has unsupported fields")
    if manifest["schema"] != SCHEMA or manifest["redaction_profile"] != redact.REDACTION_PROFILE:
        raise ExportError("prepared export requires the current public preparation profile")
    includes = manifest["includes"]
    if (not isinstance(includes, list) or not 1 <= len(includes) <= 16
            or any(not safe_relative(name) for name in includes)
            or len(set(includes)) != len(includes)
            or not isinstance(manifest["snapshot"], str) or not _SHA.fullmatch(manifest["snapshot"])
            or (manifest["previous_snapshot"] is not None and (not isinstance(manifest["previous_snapshot"], str)
                or not _SHA.fullmatch(manifest["previous_snapshot"])))):
        raise ExportError("prepared selection or snapshot is malformed")
    changes = manifest["changes"]
    if not isinstance(changes, dict) or set(changes) != {"added", "removed", "updated", "renamed"}:
        raise ExportError("prepared change observations are malformed")
    for kind in ("added", "removed", "updated"):
        if not isinstance(changes[kind], list) or len(changes[kind]) > MAX_FILES or any(not safe_relative(name) for name in changes[kind]):
            raise ExportError("prepared change paths are malformed")
    if not isinstance(changes["renamed"], list) or len(changes["renamed"]) > MAX_FILES or any(
            not isinstance(row, dict) or set(row) != {"from", "to", "basis"}
            or not safe_relative(row["from"]) or not safe_relative(row["to"]) or row["basis"] != "identical_input_sha256"
            for row in changes["renamed"]):
        raise ExportError("prepared rename observations are malformed")
    rows = manifest["files"]
    if not isinstance(rows, list) or len(rows) > MAX_FILES:
        raise ExportError("prepared export has an invalid file count")
    expected = {"prepared.json"}
    total = len(raw_manifest)
    for row in rows:
        if not isinstance(row, dict) or set(row) != _ROW_KEYS:
            raise ExportError("prepared file entry has unsupported fields")
        name = row["path"]
        if not isinstance(name, str) or not name.casefold().endswith(".md") or name.casefold() in {value.casefold() for value in expected}:
            raise ExportError("prepared source paths are invalid or duplicate")
        if not any(name.startswith(directory + "/") for directory in includes):
            raise ExportError("prepared source is outside the selected subdirectories")
        if any(type(row[key]) is not int or not 0 <= row[key] <= maximum for key, maximum in
               (("size", MAX_REFERENCE_BYTES), ("metadata_size", MAX_METADATA_BYTES))):
            raise ExportError("prepared file sizes are malformed")
        if any(not isinstance(row[key], str) or not _SHA.fullmatch(row[key]) for key in ("sha256", "source_sha256", "input_sha256", "metadata_sha256")):
            raise ExportError("prepared source hashes are malformed")
        path = _inside(root, name)
        side = meta_path(path)
        _inside(root, side.relative_to(root).as_posix())
        raw = read_bounded(path, MAX_REFERENCE_BYTES)
        metadata_raw = read_bounded(side, MAX_METADATA_BYTES)
        if len(raw) != row["size"] or _hash(raw) != row["sha256"] or len(metadata_raw) != row["metadata_size"] or _hash(metadata_raw) != row["metadata_sha256"]:
            raise ExportError("prepared Markdown or metadata changed after export")
        text, metadata = raw.decode("utf-8"), json.loads(metadata_raw)
        if not isinstance(metadata, dict) or set(metadata) != {"conditions", "retrieval"}:
            raise ExportError("only prepared conditions and retrieval metadata may leave the source")
        public, reference = prepare_reference(name, text, metadata)
        if public != text or {key: reference[key] for key in metadata} != metadata or normalized_sha256(text) != row["source_sha256"]:
            raise ExportError("prepared source no longer matches public preparation or source binding")
        total += len(raw) + len(metadata_raw)
        expected.update((name, side.relative_to(root).as_posix()))
    if total > MAX_BYTES or _snapshot(rows, manifest["includes"], manifest["redaction_profile"]) != manifest["snapshot"]:
        raise ExportError("prepared export exceeds its byte budget or has an inconsistent snapshot")
    if redact.scan_text(raw_manifest.decode("utf-8")):
        raise ExportError("prepared manifest is outside the public preparation profile")
    observed = set()
    for path in _walk(root, deadline=time.monotonic() + 60):
        if path.is_symlink():
            raise ExportError("prepared output contains a symlink")
        observed.add(path.relative_to(root).as_posix())
    if observed != expected:
        raise ExportError("prepared output has missing or unmanaged files")
    return manifest


def current_export(output_root: Path) -> dict[str, Any] | None:
    """Return only a currently verified generation; never expose its container."""
    root = Path(output_root)
    pointer = root / "current.json"
    if not pointer.exists():
        return None
    payload = json.loads(read_bounded(_inside(root, "current.json"), 4096))
    if not isinstance(payload, dict) or set(payload) != {"schema", "generation", "manifest_sha256", "snapshot"} or payload["schema"] != SCHEMA:
        raise ExportError("prepared export pointer is invalid")
    generation = payload["generation"]
    if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation):
        raise ExportError("prepared export generation is invalid")
    prepared = _inside(root, f"generations/{generation}")
    manifest = verify_export(prepared, manifest_sha256=payload["manifest_sha256"])
    if manifest["snapshot"] != payload["snapshot"]:
        raise ExportError("prepared pointer and manifest snapshots differ")
    return {"prepared_root": str(prepared), "manifest_sha256": payload["manifest_sha256"], "manifest": manifest,
            "snapshot": manifest["snapshot"]}


def _changes(before: dict[str, Any] | None, rows: list[dict]) -> dict[str, Any]:
    old = {row["path"]: row for row in before["files"]} if before else {}
    new = {row["path"]: row for row in rows}
    added, removed = sorted(new.keys() - old.keys()), sorted(old.keys() - new.keys())
    renamed = []
    for previous in removed:
        candidates = [name for name in added if new[name]["input_sha256"] == old[previous]["input_sha256"]]
        origins = [name for name in removed if old[name]["input_sha256"] == old[previous]["input_sha256"]]
        if len(candidates) == len(origins) == 1:
            renamed.append({"from": previous, "to": candidates[0], "basis": "identical_input_sha256"})
    return {"added": added, "removed": removed, "updated": sorted(name for name in old.keys() & new.keys() if old[name] != new[name]),
            "renamed": renamed}


def export_notes(source_root: Path, output_root: Path, includes: Sequence[str] = ("topics", "cases", "maintenance"), *,
                 max_files: int = MAX_FILES, max_bytes: int = MAX_BYTES,
                 max_file_bytes: int = MAX_REFERENCE_BYTES, max_seconds: float = 60) -> dict[str, Any]:
    """Prepare all selected notes before switching one generated pointer.

    Redaction failures, concurrent input edits, invalid old output and budgets
    keep the previous pointer intact. Removed/renamed sources appear in the
    manifest; old generations and unrelated output files are never deleted.
    """
    if (any(type(value) is not int or not 1 <= value <= ceiling for value, ceiling in
            ((max_files, MAX_FILES), (max_bytes, MAX_BYTES), (max_file_bytes, MAX_REFERENCE_BYTES)))
            or isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float)) or not 0 < max_seconds <= 300):
        raise ExportError("export limits must remain within 1024 files, 32 MiB total, 4 MiB per note and 300 seconds")
    if isinstance(includes, str):
        includes = [includes]
    if not isinstance(includes, (list, tuple)) or any(not isinstance(name, str) for name in includes):
        raise ExportError("selected Markdown subdirectories must be a list of relative names")
    includes = sorted(set(includes))
    if not includes or len(includes) > 16 or any(not safe_relative(name) or redact.scan_text(name) for name in includes):
        raise ExportError("select one to sixteen public relative Markdown subdirectories")
    source, output = Path(source_root).resolve(), Path(output_root).resolve()
    if source == output or source.is_relative_to(output) or output.is_relative_to(source):
        raise ExportError("source and export containers must not overlap")
    deadline = time.monotonic() + max_seconds
    lock = SwitchLock(output / ".export.lock")
    try:
        lock.acquire()
    except SwitchInProgress:
        return {"status": "busy", "switched": False}
    try:
        previous = current_export(output)
        paths = _selected(source, includes, deadline)
        if len(paths) > max_files:
            raise ExportError("export exceeded its selected document budget")
        rows, files, observed = [], {}, {}
        total = 0
        for path in paths:
            name = path.relative_to(source).as_posix()
            raw = read_bounded(path, min(max_file_bytes, max_bytes - total))
            total += len(raw)
            side = _inside(source, meta_path(path).relative_to(source).as_posix())
            metadata_raw = read_bounded(side, min(MAX_METADATA_BYTES, max_bytes - total)) if side.exists() else b""
            total += len(metadata_raw)
            metadata = json.loads(metadata_raw) if metadata_raw else {}
            if not isinstance(metadata, dict):
                raise ExportError("selected source metadata must be a JSON object")
            text = raw.decode("utf-8")
            retrieval = retrieval_metadata(metadata.get("retrieval"), text)
            aliases = list(retrieval.get("aliases", []))
            aliases.extend(alias for alias in _aliases(text) if alias not in aliases)
            allowed = {"conditions": metadata.get("conditions", {}),
                       "retrieval": {"source_sha256": normalized_sha256(text), "aliases": aliases, "topics": retrieval.get("topics", [])}}
            public, reference = prepare_reference(name, text, allowed)
            body = public.encode("utf-8")
            if len(body) > max_file_bytes:
                raise ExportError("prepared Markdown exceeds the per-file byte budget")
            prepared_metadata = _json({key: reference[key] for key in ("conditions", "retrieval")})
            side_name = side.relative_to(source).as_posix()
            files.update({name: body, side_name: prepared_metadata})
            rows.append({"path": name, "size": len(body), "sha256": _hash(body), "source_sha256": normalized_sha256(public),
                         "input_sha256": _hash(raw), "metadata_size": len(prepared_metadata), "metadata_sha256": _hash(prepared_metadata)})
            observed[name] = (_hash(raw), _hash(metadata_raw))
            if time.monotonic() > deadline:
                raise ExportError("export exceeded its preparation time budget")
        snapshot = _snapshot(rows, includes, redact.REDACTION_PROFILE)
        before = previous["manifest"] if previous else None
        changes = _changes(before, rows)
        manifest = {"schema": SCHEMA, "redaction_profile": redact.REDACTION_PROFILE, "includes": includes,
                    "snapshot": snapshot, "previous_snapshot": previous["snapshot"] if previous else None,
                    "files": rows, "changes": changes}
        encoded = _json(manifest)
        if len(encoded) > MAX_MANIFEST_BYTES or sum(map(len, files.values())) + len(encoded) > max_bytes:
            raise ExportError("prepared output exceeds its total byte budget")
        # Reobserve the complete selected set before either reuse or activation.
        def unchanged_sources() -> None:
            if time.monotonic() > deadline:
                raise ExportError("export exceeded its source-verification time budget")
            if [path.relative_to(source).as_posix() for path in _selected(source, includes, deadline)] != list(observed):
                raise ExportError("selected sources were added, removed or renamed during export")
            for name, (body_hash, metadata_hash) in observed.items():
                path = _inside(source, name)
                side = _inside(source, meta_path(path).relative_to(source).as_posix())
                if _hash(read_bounded(path, max_file_bytes)) != body_hash or _hash(read_bounded(side, MAX_METADATA_BYTES) if side.exists() else b"") != metadata_hash:
                    raise ExportError("selected source changed during public preparation")
                if time.monotonic() > deadline:
                    raise ExportError("export exceeded its source-verification time budget")
        if previous and snapshot == previous["snapshot"]:
            unchanged_sources()
            return {key: previous[key] for key in ("prepared_root", "manifest_sha256", "snapshot")} | {
                "status": "unchanged", "switched": False, "files": len(rows), "changes": changes}
        generation = uuid.uuid4().hex
        prepared = _inside(output, f"generations/{generation}")
        prepared.mkdir(parents=True, exist_ok=False)
        for name, data in files.items():
            path = _inside(prepared, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (prepared / "prepared.json").write_bytes(encoded)
        signature = _hash(encoded)
        verify_export(prepared, manifest_sha256=signature)
        unchanged_sources()
        atomic_write_json(output / "current.json", {"schema": SCHEMA, "generation": generation,
                          "manifest_sha256": signature, "snapshot": snapshot})
        return {"status": "prepared", "switched": True, "prepared_root": str(prepared), "manifest_sha256": signature,
                "snapshot": snapshot, "files": len(rows), "changes": changes}
    except (OSError, ValueError, DistributionError) as exc:
        return {"status": "incomplete", "switched": False, "reason": str(exc)}
    finally:
        lock.release()


def main(argv: list[str] | None = None) -> int:
    """Explicit public-only preparation or verification, with bounded output."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--include", action="append", help="Selected Markdown subdirectory; repeat as needed")
    parser.add_argument("--max-files", type=int, default=MAX_FILES)
    parser.add_argument("--max-bytes", type=int, default=MAX_BYTES)
    parser.add_argument("--max-file-bytes", type=int, default=MAX_REFERENCE_BYTES)
    parser.add_argument("--max-seconds", type=float, default=60)
    parser.add_argument("--verify-root", type=Path)
    parser.add_argument("--manifest-sha256")
    args = parser.parse_args(argv)
    try:
        if args.verify_root:
            if not args.manifest_sha256 or args.source_root or args.output_root or args.include:
                parser.error("--verify-root requires --manifest-sha256 and cannot be combined with export paths")
            manifest = verify_export(args.verify_root, manifest_sha256=args.manifest_sha256)
            result = {"status": "verified", "prepared_root": str(args.verify_root), "manifest_sha256": args.manifest_sha256,
                      "snapshot": manifest["snapshot"], "files": len(manifest["files"]), "changes": manifest["changes"]}
        else:
            if not args.source_root or not args.output_root or args.manifest_sha256:
                parser.error("export requires --source-root and --output-root")
            result = export_notes(args.source_root, args.output_root, includes=args.include or ("topics", "cases", "maintenance"),
                                  max_files=args.max_files, max_bytes=args.max_bytes,
                                  max_file_bytes=args.max_file_bytes, max_seconds=args.max_seconds)
    except (OSError, ValueError, DistributionError) as exc:
        result = {"status": "incomplete", "reason": str(exc)[:1000]}
    if "changes" in result:
        result = {**result, "changes": {kind: len(items) for kind, items in result["changes"].items()}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"prepared", "unchanged", "verified"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
