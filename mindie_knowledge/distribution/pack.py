"""OVPack integrity and contract verification.

An OVPack is a zip whose single top-level directory mirrors the exported
resource root, with ``<root>/_ovpack/manifest.json`` carrying the dense index
(dimensions, dtype, byte order, per-part sha256) and per-entry content hashes.
All checks here stream from the zip: no member is ever written to disk, so
unsafe paths (absolute, ``..``, drive letters, backslashes) are rejected
before they could matter, on any platform.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from mindie_knowledge.distribution.errors import CorruptPack, IncompatiblePack
from mindie_knowledge.distribution.manifest import (
    ExpectedContract,
    ReleaseManifest,
    sha256_file,
)

_OVPACK_MANIFEST_SUFFIX = "/_ovpack/manifest.json"


def _unsafe_member_reason(name: str) -> str | None:
    """Return why a zip member name is unsafe to unpack, else ``None``."""

    if "\x00" in name:
        return "NUL in member name"
    if not name or name.startswith("/") or name.startswith("\\"):
        return "absolute path"
    if "\\" in name:
        return "backslash separator"
    parts = PurePosixPath(name).parts
    if any(part in ("", ".", "..") for part in parts):
        return "dot or dot-dot component"
    first = parts[0]
    if len(first) >= 2 and first[1] == ":":
        return "drive-letter path"
    return None


def _sha256_member(pack: zipfile.ZipFile, name: str) -> str:
    digest = hashlib.sha256()
    with pack.open(name) as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class PackInfo:
    """Parsed OVPack: root directory name and the embedded manifest."""

    root_name: str
    manifest: dict[str, Any]

    @property
    def index(self) -> dict[str, Any]:
        return self.manifest.get("index") or {}

    @property
    def dense(self) -> dict[str, Any]:
        return self.index.get("dense") or {}

    @property
    def entries(self) -> list[dict[str, Any]]:
        entries = self.manifest.get("entries")
        return entries if isinstance(entries, list) else []


def inspect_pack(pack_path: Path) -> PackInfo:
    """Open the pack and parse ``<root>/_ovpack/manifest.json``.

    Raises :class:`CorruptPack` for any structural problem with an actionable
    reason (not a zip, unsafe member, missing/invalid embedded manifest).
    """

    pack_path = Path(pack_path)
    try:
        archive = zipfile.ZipFile(pack_path)
    except zipfile.BadZipFile:
        raise CorruptPack(
            f"{pack_path.name} is not a readable zip; re-download the release asset"
        ) from None
    except OSError as exc:
        raise CorruptPack(f"cannot read pack {pack_path}: {exc}") from None
    with archive:
        names = archive.namelist()
        if not names:
            raise CorruptPack(f"{pack_path.name} is an empty archive")
        # ZipInfo.filename normalizes backslashes on Windows and truncates
        # NULs. Validate the raw archive spelling before using normalized names.
        for info in archive.infolist():
            name = info.orig_filename
            reason = _unsafe_member_reason(name)
            if reason:
                raise CorruptPack(f"pack member {name!r} is unsafe to unpack: {reason}")
        roots = {name.split("/", 1)[0] for name in names}
        if len(roots) != 1:
            raise CorruptPack(
                f"pack must contain exactly one top-level resource root, found {sorted(roots)}"
            )
        root_name = roots.pop()
        manifest_name = f"{root_name}{_OVPACK_MANIFEST_SUFFIX}"
        if manifest_name not in names:
            raise CorruptPack(
                f"pack lacks {manifest_name}; export with vectors from OpenViking "
                "produces this manifest, a plain zip of markdown does not"
            )
        try:
            manifest = json.loads(archive.read(manifest_name).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CorruptPack(f"embedded {manifest_name} is not valid JSON") from None
        if not isinstance(manifest, dict):
            raise CorruptPack(f"embedded {manifest_name} is not a JSON object")
        kind = manifest.get("kind")
        if kind is not None and kind != "openviking.ovpack":
            raise CorruptPack(f"embedded manifest kind {kind!r} != 'openviking.ovpack'")
    return PackInfo(root_name=root_name, manifest=manifest)


def verify_pack(
    pack_path: Path,
    manifest: ReleaseManifest,
    *,
    expected: ExpectedContract,
) -> PackInfo:
    """Full pre-import verification of a downloaded pack against its release manifest.

    Order: asset integrity -> archive structure -> dense index contract ->
    embedded part checksums -> per-file content checksums. Any failure raises
    :class:`CorruptPack`/:class:`IncompatiblePack`; the caller keeps the old
    version and cleans staging.
    """

    pack_path = Path(pack_path)
    expected_pack = manifest.pack
    if not pack_path.is_file():
        raise CorruptPack(f"pack file {pack_path} is missing after download")
    size = pack_path.stat().st_size
    if size != expected_pack["size"]:
        raise CorruptPack(
            f"pack size {size} != release manifest {expected_pack['size']}; download is incomplete"
        )
    digest = sha256_file(pack_path)
    if digest != expected_pack["sha256"]:
        raise CorruptPack(
            f"pack sha256 {digest} != release manifest {expected_pack['sha256']}; "
            "the asset is corrupted or was replaced"
        )

    info = inspect_pack(pack_path)
    dense = info.dense
    if not dense:
        raise CorruptPack(
            "pack index has no dense block; this release format requires "
            "export --include-vectors (prebuilt dense snapshot)"
        )
    try:
        dimensions = int(dense.get("dimensions"))
    except (TypeError, ValueError):
        dimensions = -1
    if dimensions != expected.embedding_dimension:
        raise IncompatiblePack(
            f"pack dense dimension {dimensions} != local {expected.embedding_dimension}"
        )
    if str(dense.get("dtype") or "") != "float32":
        raise IncompatiblePack(f"pack dense dtype {dense.get('dtype')!r} != 'float32'")
    if str(dense.get("byte_order") or "") != "little":
        raise IncompatiblePack(f"pack dense byte_order {dense.get('byte_order')!r} != 'little'")
    embedding = dense.get("embedding") if isinstance(dense.get("embedding"), dict) else {}
    pack_model = str(embedding.get("model") or manifest.embedding.get("model") or "")
    if pack_model != expected.embedding_model:
        raise IncompatiblePack(
            f"pack was embedded with model {pack_model or 'unknown'} != local "
            f"{expected.embedding_model}; build a pack for the pinned model"
        )
    pack_provider = str(embedding.get("provider") or manifest.embedding.get("provider") or "")
    if pack_provider != expected.embedding_provider:
        raise IncompatiblePack(
            f"pack embedding provider {pack_provider or 'unknown'} != local "
            f"{expected.embedding_provider}"
        )

    records = info.index.get("records") if isinstance(info.index.get("records"), dict) else {}
    with zipfile.ZipFile(pack_path) as archive:
        for part, block in ((dense, "dense vectors"), (records, "index records")):
            part_path = part.get("path")
            part_sha = part.get("sha256")
            if not part_path or not part_sha:
                raise CorruptPack(f"pack index {block} entry lacks path/sha256")
            member = f"{info.root_name}/{part_path}"
            if member not in archive.namelist():
                raise CorruptPack(f"pack is missing indexed part {member}")
            actual = _sha256_member(archive, member)
            if actual != part_sha:
                raise CorruptPack(
                    f"pack {block} part {part_path} sha256 mismatch ({actual} != {part_sha}); "
                    "the archive is corrupted"
                )

        entries = {
            entry.get("path"): entry
            for entry in info.entries
            if isinstance(entry, dict) and entry.get("kind") == "file"
        }
        content_checked = 0
        for wanted in manifest.content_files:
            path = wanted["path"]
            entry = entries.get(path)
            if entry is None:
                raise CorruptPack(
                    f"corpus file {path} from the release manifest is not in the pack entries"
                )
            if entry.get("sha256") != wanted["sha256"]:
                raise CorruptPack(
                    f"pack content of {path} does not match the release manifest sha256; "
                    "pack and manifest do not describe the same Git content"
                )
            # 0.4.19 stores content under <root>/files/, index parts under <root>/_ovpack/.
            member = f"{info.root_name}/files/{path}"
            if member not in archive.namelist():
                raise CorruptPack(f"pack entry {member} is listed but absent from the archive")
            actual = _sha256_member(archive, member)
            if actual != wanted["sha256"]:
                raise CorruptPack(f"pack member {member} content sha256 mismatch")
            content_checked += 1
        if content_checked != manifest.data["content"]["count"]:
            raise CorruptPack("content count mismatch after verification")
    return info


def _portable_snapshot(pack_path: Path) -> tuple[dict[str, str], dict[tuple[str, str, int], str]]:
    """Content and dense values, independent of export order and destination URI.

    OpenViking 0.4.19 exports JSONL records with path/kind/level and a dense
    offset measured in float32 values. Record ids and offsets can change on a
    second export, so compare values by their portable identity instead.
    """
    info = inspect_pack(pack_path)
    dense, records = info.dense, info.index.get("records") or {}
    if dense.get("dtype") != "float32" or dense.get("byte_order") != "little":
        raise CorruptPack("runtime export has no compatible float32 dense snapshot")
    dimension = dense.get("dimensions")
    if not isinstance(dimension, int) or dimension <= 0:
        raise CorruptPack("runtime export has an invalid dense dimension")
    files: dict[str, str] = {}
    vectors: dict[tuple[str, str, int], str] = {}
    with zipfile.ZipFile(pack_path) as archive:
        parts = []
        for part in (records, dense):
            path = part.get("path")
            if not isinstance(path, str) or _unsafe_member_reason(path):
                raise CorruptPack("runtime export has an invalid index part path")
            raw = archive.read(f"{info.root_name}/{path}")
            if hashlib.sha256(raw).hexdigest() != part.get("sha256"):
                raise CorruptPack("runtime export index part checksum differs")
            parts.append(raw)
        records_raw, dense_raw = parts
        try:
            rows = [json.loads(line) for line in records_raw.splitlines() if line.strip()]
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CorruptPack("runtime export has invalid index records") from exc
        if len(rows) != records.get("count") or len(rows) != dense.get("count"):
            raise CorruptPack("runtime export dense and record counts differ")
        occupied: set[int] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise CorruptPack("runtime export has a non-object index record")
            path, kind, level = row.get("path"), row.get("kind"), row.get("level")
            ref = (row.get("vector") or {}).get("dense") or {}
            offset = ref.get("offset")
            if (not isinstance(path, str) or (path and _unsafe_member_reason(path))
                    or kind not in {"file", "directory"} or not isinstance(level, int)
                    or not isinstance(offset, int) or offset < 0
                    or ref.get("dimensions") != dimension or offset % dimension):
                raise CorruptPack("runtime export has an invalid dense record")
            key = (path, kind, level)
            if key in vectors or offset in occupied:
                raise CorruptPack("runtime export has duplicate dense records")
            occupied.add(offset)
            raw = dense_raw[offset * 4:(offset + dimension) * 4]
            if len(raw) != dimension * 4:
                raise CorruptPack("runtime export dense record is truncated")
            vectors[key] = hashlib.sha256(raw).hexdigest()
        if len(dense_raw) != len(rows) * dimension * 4:
            raise CorruptPack("runtime export dense data length differs from its records")
        for entry in info.entries:
            if entry.get("kind") != "file":
                continue
            path = entry.get("path")
            if not isinstance(path, str) or _unsafe_member_reason(path) or path in files:
                raise CorruptPack("runtime export has an invalid or duplicate file entry")
            digest = _sha256_member(archive, f"{info.root_name}/files/{path}")
            if digest != entry.get("sha256"):
                raise CorruptPack("runtime export file checksum differs")
            files[path] = digest
    return files, vectors


def verify_imported_pack(exported: Path, verified_source: Path) -> dict[str, int]:
    """Audit materialized Markdown and vectors against the verified release.

    This uses the native vector-inclusive export, not check_consistency alone:
    a record can exist while its source text or dense vector has been damaged.
    No document embedding is performed by this audit.
    """
    expected_files, expected_vectors = _portable_snapshot(verified_source)
    actual_files, actual_vectors = _portable_snapshot(exported)
    if actual_files != expected_files:
        raise CorruptPack("imported shared files differ from the verified release")
    if actual_vectors != expected_vectors:
        raise CorruptPack("imported shared dense vectors differ from the verified release")
    return {"verified_files": len(actual_files), "verified_vectors": len(actual_vectors)}


def verify_model_files(model_cache: Path, manifest: ReleaseManifest) -> list[str]:
    """Check the local read-only model cache against pinned model/tokenizer checksums.

    Returns a list of human-readable mismatches (empty when the local model
    files are exactly what the build used). A missing cache directory is a
    mismatch entry, not an exception, so the caller can phrase one actionable
    reason.
    """

    problems: list[str] = []
    model_files = manifest.embedding.get("model_files") or []
    if not model_files:
        return problems
    root = Path(model_cache)
    if not root.is_dir():
        return [f"model cache {root} is missing; install the pinned embedding model first"]
    checked_revisions: set[str] = set()
    for entry in model_files:
        path = root / entry["path"]
        parts = Path(entry["path"]).parts
        if len(parts) >= 4 and parts[0].startswith("models--") and parts[1] == "snapshots":
            repository, revision = parts[0], parts[2]
            if repository not in checked_revisions:
                checked_revisions.add(repository)
                ref = root / repository / "refs" / "main"
                if not ref.is_file() or ref.read_text(encoding="utf-8").strip() != revision:
                    problems.append("active embedding model revision differs from the released snapshot")
        if not path.is_file():
            problems.append(f"model file {entry['path']} is missing from the local cache")
            continue
        actual = sha256_file(path)
        if actual != entry["sha256"]:
            problems.append(
                f"model file {entry['path']} sha256 differs from the released pin; "
                "the local embedding model is not the build-time model"
            )
    return problems
