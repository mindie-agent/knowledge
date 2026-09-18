"""Release/build manifest: pinned contract, hashing and atomic JSON writes.

The release manifest binds a fixed Git content version to one dense OVPack:
source Git SHA, builder/OpenViking versions, embedding model/tokenizer file
checksums and dimensions, plus pack integrity values. Checksums prove
integrity and compatibility; they never replace the Git content identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

OPENVIKING_VERSION = "0.4.19"
OPENVIKING_SDK_VERSION = "0.1.10"
EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_DIMENSION = 384
EMBEDDING_PROVIDER = "openai"

RELEASE_SCHEMA = "mindie-knowledge-release/1"
SHARED_PARENT_URI = "viking://resources/shared"
PACK_VECTOR_MODE = "require"

from mindie_knowledge.distribution.errors import CorruptPack, IncompatiblePack

_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def is_git_sha(text: Any) -> bool:
    return isinstance(text, str) and bool(_GIT_SHA_RE.match(text))


def version_id_from_sha(sha: str) -> str:
    """URI-safe shared version root name for one fixed Git content version."""

    if not is_git_sha(sha):
        raise CorruptPack(f"source git sha must be 40 lowercase hex characters, got: {sha!r}")
    return f"v{sha[:12]}"


def shared_root_uri(version_id: str) -> str:
    return f"{SHARED_PARENT_URI}/{version_id}"


def sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_data(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_tree(root: Path) -> list[dict[str, Any]]:
    """sha256 + size for every regular file under ``root``, sorted by relative path."""

    entries: list[dict[str, Any]] = []
    for path in sorted(p for p in Path(root).rglob("*") if p.is_file()):
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    return entries


def hash_model_tree(root: Path) -> list[dict[str, Any]]:
    """Pin model snapshots, excluding machine-specific download cache state.

    Hugging Face stores the actual model/tokenizer under snapshots; blobs are
    duplicate storage and may be absent when Windows cannot create symlinks.
    refs/main selects the downloaded revision, not every historical snapshot.
    A flat model directory is also accepted by the build API.
    """
    root = Path(root)
    repositories = sorted(root.glob("models--*"))
    if not repositories:
        return hash_tree(root)
    entries: list[dict[str, Any]] = []
    for repository in repositories:
        ref = repository / "refs" / "main"
        revision = ref.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise CorruptPack("model cache main ref is not a Git revision")
        snapshot = repository / "snapshots" / revision
        if not snapshot.is_dir():
            raise CorruptPack("model cache active snapshot is missing")
        prefix = snapshot.relative_to(root).as_posix()
        entries.extend({**item, "path": f"{prefix}/{item['path']}"} for item in hash_tree(snapshot))
    if not entries:
        raise CorruptPack("model cache contains no model files")
    return entries


def content_digest(entries: Iterable[Mapping[str, Any]]) -> str:
    """One digest over ``path + sha256`` lines; a content fingerprint, not an identity."""

    digest = hashlib.sha256()
    for entry in entries:
        digest.update(f"{entry['path']}\0{entry['sha256']}\n".encode("utf-8"))
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """fsync + os.replace so a crash never leaves a half-written pointer file."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


@dataclass(frozen=True)
class ExpectedContract:
    """What the local build/sync side requires a release to match."""

    openviking_version: str = OPENVIKING_VERSION
    embedding_model: str = EMBEDDING_MODEL
    embedding_dimension: int = EMBEDDING_DIMENSION
    embedding_provider: str = EMBEDDING_PROVIDER

    @classmethod
    def from_embedding_info(
        cls, info: Mapping[str, Any], *, openviking_version: str = OPENVIKING_VERSION
    ) -> "ExpectedContract":
        """Build the contract from the live embedding endpoint identity.

        The local lifecycle reports the actual model/dimension it serves
        (e.g. from the loopback embedding ``/health``); sync then fails with a
        clear reason instead of importing a pack built for another model.
        """

        model = str(info.get("model") or "").strip()
        if not model:
            raise IncompatiblePack(
                "local embedding endpoint did not report a model name; "
                "cannot verify pack compatibility"
            )
        try:
            dimension = int(info.get("dimension"))
        except (TypeError, ValueError):
            raise IncompatiblePack(
                "local embedding endpoint did not report a vector dimension; "
                "cannot verify pack compatibility"
            ) from None
        provider = str(info.get("provider") or EMBEDDING_PROVIDER)
        return cls(
            openviking_version=openviking_version,
            embedding_model=model,
            embedding_dimension=dimension,
            embedding_provider=provider,
        )


@dataclass
class ReleaseManifest:
    """Validated ``release.json`` payload."""

    data: dict[str, Any] = field(default_factory=dict)

    @property
    def version_id(self) -> str:
        return self.data["version_id"]

    @property
    def source_git_sha(self) -> str:
        return self.data["source"]["git_sha"]

    @property
    def pack(self) -> dict[str, Any]:
        return self.data["pack"]

    @property
    def embedding(self) -> dict[str, Any]:
        return self.data["embedding"]

    @property
    def content_files(self) -> list[dict[str, Any]]:
        return self.data["content"]["files"]


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise CorruptPack(reason)


def validate_release_manifest(data: Any, *, expected: ExpectedContract) -> ReleaseManifest:
    """Validate structure, then check the pinned version/model contract.

    Structural problems raise :class:`CorruptPack`; a well-formed release that
    targets another OpenViking version or embedding model raises
    :class:`IncompatiblePack` with the differing field named.
    """

    _require(isinstance(data, dict), "release manifest is not a JSON object")
    _require(
        data.get("schema") == RELEASE_SCHEMA,
        f"release manifest schema must be {RELEASE_SCHEMA!r}, got {data.get('schema')!r}",
    )
    version_id = data.get("version_id")
    _require(isinstance(version_id, str) and version_id, "release manifest lacks version_id")
    source = data.get("source")
    _require(isinstance(source, dict), "release manifest lacks a source object")
    sha = source.get("git_sha")
    _require(is_git_sha(sha), f"source.git_sha must be 40 lowercase hex, got {sha!r}")
    _require(
        version_id == version_id_from_sha(sha),
        f"version_id {version_id!r} does not derive from source git sha",
    )
    embedding = data.get("embedding")
    _require(isinstance(embedding, dict), "release manifest lacks an embedding object")
    pack = data.get("pack")
    _require(isinstance(pack, dict), "release manifest lacks a pack object")
    _require(isinstance(pack.get("file"), str) and pack["file"], "pack.file missing")
    _require(
        pack["file"] not in {".", ".."} and not any(ch in pack["file"] for ch in "/\\:\x00"),
        "pack.file must be a single asset filename",
    )
    _require(
        re.match(r"^[0-9a-f]{64}$", str(pack.get("sha256") or "")) is not None,
        "pack.sha256 must be 64 lowercase hex",
    )
    _require(isinstance(pack.get("size"), int) and pack["size"] > 0, "pack.size missing")
    _require(
        pack.get("vector_mode") == PACK_VECTOR_MODE,
        f"pack.vector_mode must be {PACK_VECTOR_MODE!r}; prebuilt clients never re-embed silently",
    )
    index = pack.get("index")
    _require(isinstance(index, dict) and isinstance(index.get("dense"), dict), "pack.index.dense missing")
    dense = index["dense"]
    _require(
        isinstance(dense.get("count"), int) and dense["count"] > 0,
        "pack.index.dense.count missing; export must include dense vectors",
    )
    _require(isinstance(index.get("records"), dict), "pack.index.records missing")
    content = data.get("content")
    _require(isinstance(content, dict), "release manifest lacks a content object")
    files = content.get("files")
    _require(isinstance(files, list) and len(files) > 0, "content.files must be a non-empty list")
    for entry in files:
        _require(
            isinstance(entry, dict)
            and isinstance(entry.get("path"), str)
            and re.match(r"^[0-9a-f]{64}$", str(entry.get("sha256") or ""))
            and isinstance(entry.get("size"), int),
            f"content.files entry is malformed: {entry!r}",
        )
        _require(not entry["path"].startswith(("/", "\\")) and not any(c in entry["path"] for c in "\\:\x00")
                 and all(part not in {"", ".", ".."} for part in entry["path"].split("/")), "content file path is unsafe")
    _require(len({entry["path"] for entry in files}) == len(files), "content file paths are not unique")
    _require(
        content.get("count") == len(files),
        f"content.count {content.get('count')!r} != number of content.files {len(files)}",
    )
    _require(
        re.match(r"^[0-9a-f]{64}$", str(content.get("content_sha256") or "")) is not None,
        "content.content_sha256 must be 64 lowercase hex",
    )
    references = data.get("references")
    if references is not None:
        _require(isinstance(references, dict), "references must be an object")
        name = references.get("file")
        _require(isinstance(name, str) and bool(name) and name not in {".", "..", pack["file"], "release.json"}
                 and not any(char in name for char in "/\\:\x00"), "references.file must be a distinct single asset filename")
        _require(re.fullmatch(r"[0-9a-f]{64}", str(references.get("sha256") or "")) is not None,
                 "references.sha256 must be 64 lowercase hex")
        _require(isinstance(references.get("size"), int) and not isinstance(references["size"], bool)
                 and 0 < references["size"] <= 64 * 1024 * 1024, "references.size must fit the 64 MiB budget")
        _require(references.get("schema") == "mindie-knowledge-references/1", "unsupported references schema")
        _require(references.get("source_git_sha") == sha, "references must bind the same source Git SHA")
        _require(references.get("count") == len(files), "references.count must match the released body count")

    build = data.get("build") if isinstance(data.get("build"), dict) else {}
    openviking = str(build.get("openviking") or "")
    if openviking != expected.openviking_version:
        raise IncompatiblePack(
            f"release was built with OpenViking {openviking or 'unknown'}, "
            f"local pin is {expected.openviking_version}; upgrade the local package first"
        )
    model = str(embedding.get("model") or "")
    if model != expected.embedding_model:
        raise IncompatiblePack(
            f"release embedding model {model or 'unknown'} != local model "
            f"{expected.embedding_model}; do not import: vectors would not match queries"
        )
    try:
        dimension = int(embedding.get("dimensions"))
    except (TypeError, ValueError):
        dimension = -1
    if dimension != expected.embedding_dimension:
        raise IncompatiblePack(
            f"release embedding dimension {dimension} != local {expected.embedding_dimension}"
        )
    provider = str(embedding.get("provider") or "")
    if provider != expected.embedding_provider:
        raise IncompatiblePack(
            f"release embedding provider {provider or 'unknown'} != local {expected.embedding_provider}"
        )
    model_files = embedding.get("model_files")
    if model_files is not None:
        _require(isinstance(model_files, list), "embedding.model_files must be a list")
        for entry in model_files:
            _require(
                isinstance(entry, dict)
                and isinstance(entry.get("path"), str)
                and re.match(r"^[0-9a-f]{64}$", str(entry.get("sha256") or "")),
                f"embedding.model_files entry is malformed: {entry!r}",
            )
    return ReleaseManifest(data=dict(data))
