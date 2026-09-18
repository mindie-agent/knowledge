"""Build one dense OVPack from a fixed Git content version.

Content identity is the Git commit: files are materialized with
``git archive <sha>`` (never the working tree), hashed file by file, written
into a scratch build root of a live native OpenViking instance with
``processing_mode=vectors_only`` (no summarizer), and exported with
``export_ovpack(include_vectors=True)``. The build manifest records the exact
source SHA, builder/OpenViking versions, embedding model/tokenizer file
checksums and the pack index, so a client can verify consistency precisely.
"""

from __future__ import annotations

import hashlib
import io
import platform
import re
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version as installed_version
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from mindie_knowledge.distribution.client import MetricsReader, metrics_delta
from mindie_knowledge.distribution.errors import BuildError
from mindie_knowledge.distribution.manifest import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    OPENVIKING_VERSION,
    PACK_VECTOR_MODE,
    RELEASE_SCHEMA,
    atomic_write_json,
    content_digest,
    hash_model_tree,
    is_git_sha,
    sha256_file,
    utc_now,
    version_id_from_sha,
)
from mindie_knowledge.distribution.pack import inspect_pack

BUILD_ROOT_PREFIX = "viking://resources/build"

_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


@dataclass
class BuildResult:
    pack_path: Path
    manifest_path: Path
    manifest: dict[str, Any]
    documents: int = 0
    details: dict[str, Any] = field(default_factory=dict)


def _git(repo: Path, *args: str, timeout: float = 60.0) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BuildError(f"git is not usable for {repo}: {exc}") from None
    if proc.returncode != 0:
        raise BuildError(f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def resolve_git_sha(repo: Path, expected_sha: str | None = None) -> str:
    """The content identity of a build; refuses uncommitted corpus changes."""

    repo = Path(repo)
    if not (repo / ".git").exists():
        raise BuildError(f"{repo} is not a Git worktree; the build requires fixed Git content")
    head = _git(repo, "rev-parse", "HEAD")
    if not is_git_sha(head):
        raise BuildError(f"git rev-parse HEAD returned an unexpected value: {head!r}")
    if expected_sha is not None:
        if not is_git_sha(expected_sha):
            raise BuildError(f"--sha must be 40 lowercase hex, got {expected_sha!r}")
        if head != expected_sha:
            raise BuildError(
                f"worktree HEAD {head} != requested {expected_sha}; "
                "check out the exact reviewed commit before building"
            )
    dirty = _git(repo, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise BuildError(
            "the corpus worktree has uncommitted changes; commit first so the "
            "built pack matches the Git identity"
        )
    return head


def _materialize(repo: Path, sha: str, subdir: str, target: Path) -> list[dict[str, Any]]:
    """Extract ``*.md`` files of ``subdir`` at ``sha`` via git archive; hash each."""

    proc = subprocess.Popen(
        ["git", "-C", str(repo), "archive", "--format=tar", sha, "--", subdir if subdir != "." else "."],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    files: list[dict[str, Any]] = []
    base = PurePosixPath(subdir) if subdir not in ("", ".") else None
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                name = PurePosixPath(member.name)
                if name.is_absolute() or any(char in member.name for char in "\\:\x00") or any(part in ("", ".", "..") for part in name.parts):
                    raise BuildError(f"unsafe path in git archive: {member.name!r}")
                if base is not None:
                    try:
                        name = name.relative_to(base)
                    except ValueError:
                        continue
                if name.suffix.lower() != ".md" and not name.name.endswith(".meta.json"):
                    continue
                from mindie_knowledge.markdown import MAX_REFERENCE_BYTES, MAX_METADATA_BYTES
                maximum = MAX_REFERENCE_BYTES if name.suffix.lower() == ".md" else MAX_METADATA_BYTES
                if member.size > maximum:
                    raise BuildError(f"{name}: source exceeds the {maximum}-byte document budget")
                raw = archive.extractfile(member).read()  # type: ignore[union-attr]
                destination = target / Path(*name.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
                if name.suffix.lower() != ".md":
                    continue
                files.append(
                    {
                        "path": name.as_posix(),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "size": len(raw),
                    }
                )
    except tarfile.TarError as exc:
        proc.kill()
        proc.communicate(timeout=30)
        raise BuildError(f"cannot read git archive of {sha}: {exc}") from None
    except BaseException:
        proc.kill()
        proc.communicate(timeout=30)
        raise
    _, stderr = proc.communicate(timeout=120)
    if proc.returncode != 0:
        raise BuildError(f"git archive failed: {stderr.decode('utf-8', 'replace').strip()}")
    files.sort(key=lambda entry: entry["path"])
    return files


def check_markdown_contract(relpath: str, text: str) -> None:
    """The only corpus format gate: a title and a non-empty body."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    match = _TITLE_RE.search(normalized)
    if match:
        body = (normalized[: match.start()] + normalized[match.end() :]).strip()
    else:
        lines = normalized.split("\n")
        body = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
    if not body:
        raise BuildError(f"{relpath}: corpus markdown needs a title and a non-empty body")


def _ensure_dir(client: Any, uri: str) -> None:
    try:
        client.mkdir(uri)
    except Exception as exc:  # noqa: BLE001
        text = str(exc).upper()
        if "ALREADY" not in text and "EXISTS" not in text:
            raise


def _check_queues(processing: Mapping[str, Any], *, stage: str) -> None:
    problems: list[str] = []
    for queue, state in processing.items():
        if not isinstance(state, Mapping):
            continue
        errors = int(state.get("error_count") or 0)
        if errors:
            detail = "; ".join(str(item) for item in (state.get("errors") or [])[:3])
            problems.append(f"{queue}: {errors} error(s) {detail}")
    if problems:
        raise BuildError(f"native processing failed during {stage}: " + " | ".join(problems))


def _installed_version(name: str) -> str:
    try:
        return installed_version(name)
    except PackageNotFoundError:
        return "unknown"


def build_pack(
    *,
    repo: Path,
    out_dir: Path,
    client: Any = None,
    client_factory: Callable[[], Any] | None = None,
    expected_sha: str | None = None,
    corpus_subdir: str = ".",
    name: str = "corpus",
    model_cache: Path | None = None,
    metrics_reader: MetricsReader | None = None,
    wait_timeout: float = 1800.0,
) -> BuildResult:
    """Build ``<name>-<version_id>.ovpack`` plus ``<name>-<version_id>.release.json``.

    ``client``/``client_factory`` connect to the *build* instance; it is a
    scratch native instance, never a user-facing one. ``model_cache`` pins the
    actual model/tokenizer file checksums into the manifest when given.
    """

    repo = Path(repo)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sha = resolve_git_sha(repo, expected_sha)
    version_id = version_id_from_sha(sha)

    with tempfile.TemporaryDirectory(prefix="ovpack-build-") as scratch:
        materialized = Path(scratch) / "corpus"
        materialized.mkdir()
        files = _materialize(repo, sha, corpus_subdir, materialized)
        if not files:
            raise BuildError(f"no markdown files under {corpus_subdir!r} at {sha}")
        from mindie_knowledge.distribution.references import prepare_reference, write_references
        import json
        reference_rows = []
        for entry in files:
            path = materialized / Path(*PurePosixPath(entry["path"]).parts)
            text = path.read_text(
                encoding="utf-8"
            )
            check_markdown_contract(entry["path"], text)
            metadata = {}
            if path.with_suffix(".meta.json").is_file():
                try:
                    metadata = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise BuildError(f"{entry['path']}: invalid source metadata") from exc
            public_text, reference = prepare_reference(entry["path"], text, metadata)
            path.write_text(public_text, encoding="utf-8", newline="\n")
            raw = public_text.encode("utf-8")
            entry.update(sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))
            reference_rows.append(reference)
        references_path = out_dir / f"{name}-{version_id}.references.json"
        references = write_references(references_path, source_git_sha=sha, documents=reference_rows)

        if client is None:
            if client_factory is None:
                raise BuildError(
                    "no live OpenViking build instance; pass client= or client_factory= "
                    "(the distribution module does not own server processes)"
                )
            client = client_factory()

        build_root = f"{BUILD_ROOT_PREFIX}/{version_id}"
        before = metrics_reader() if metrics_reader else None
        try:
            _ensure_dir(client, BUILD_ROOT_PREFIX)
            try:
                client.rm(build_root, recursive=True, wait=True, timeout=300)
            except Exception:  # noqa: BLE001 - absent build root is fine
                pass
            _ensure_dir(client, build_root)
            for entry in files:
                client.write(
                    f"{build_root}/{entry['path']}",
                    (materialized / Path(*PurePosixPath(entry['path']).parts)).read_text(
                        encoding="utf-8"
                    ),
                    wait=False,
                    options={"processing_mode": "vectors_only"},
                )
            _check_queues(client.wait_processed(timeout=wait_timeout), stage="document writes")
            consistency = client.check_consistency(uri=build_root)
            if not consistency.get("ok"):
                raise BuildError(f"build instance consistency check failed: {consistency}")
            pack_path = out_dir / f"{name}-{version_id}.ovpack"
            exported = client.export_ovpack(build_root, str(pack_path), include_vectors=True)
            pack_path = Path(exported)
        except BuildError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BuildError(f"native build failed: {type(exc).__name__}: {exc}") from None
        after = metrics_reader() if metrics_reader else None

    info = inspect_pack(pack_path)
    if info.root_name != version_id:
        raise BuildError(
            f"native export rooted the pack at {info.root_name!r}, expected {version_id!r}; "
            "OpenViking export behavior differs from the pinned 0.4.19 contract"
        )
    if not info.dense:
        raise BuildError("exported pack carries no dense vectors; include_vectors must be on")

    manifest: dict[str, Any] = {
        "schema": RELEASE_SCHEMA,
        "version_id": version_id,
        "source": {"git_sha": sha, "corpus_path": corpus_subdir},
        "build": {
            "openviking": _installed_version("openviking"),
            "openviking_sdk": _installed_version("openviking-sdk"),
            "builder": "mindie-knowledge-distribution",
            "builder_version": _installed_version("mindie-knowledge"),
            "platform": f"{platform.system()} {platform.machine()}",
            "device": "cpu",
            "created_at": utc_now(),
        },
        "embedding": {
            "provider": EMBEDDING_PROVIDER,
            "model": EMBEDDING_MODEL,
            "dimensions": EMBEDDING_DIMENSION,
        },
        "pack": {
            "file": pack_path.name,
            "sha256": sha256_file(pack_path),
            "size": pack_path.stat().st_size,
            "format": "openviking-ovpack",
            "vector_mode": PACK_VECTOR_MODE,
            "root_name": info.root_name,
            "index": info.index,
        },
        "content": {
            "files": files,
            "count": len(files),
            "content_sha256": content_digest(files),
        },
        "references": references,
    }
    calls = metrics_delta(before, after)
    if calls:
        manifest["build"]["embedding_calls"] = calls
    if model_cache is not None:
        cache = Path(model_cache)
        if not cache.is_dir():
            raise BuildError(f"model cache {cache} is missing; cannot pin model file checksums")
        manifest["embedding"]["model_files"] = hash_model_tree(cache)

    manifest_path = out_dir / f"{name}-{version_id}.release.json"
    atomic_write_json(manifest_path, manifest)
    return BuildResult(
        pack_path=pack_path,
        manifest_path=manifest_path,
        manifest=manifest,
        documents=len(files),
        details={"embedding_calls": calls, "dense_records": info.dense.get("count")},
    )
