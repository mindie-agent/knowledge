"""Prepared public reference metadata and immutable local source mounts.

The release catalog exporter is not a redaction boundary. This module prepares
the public body and selected metadata first, then derives contexts from those
exact bytes. Raw private sidecars, paths, evidence and source objects never enter
the public asset. Legacy packs can supply verified bodies without enrichment.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import secrets
import shutil
import zipfile
from typing import Iterator

from mindie_knowledge.distribution.errors import BuildError, CorruptPack
from mindie_knowledge.distribution.manifest import atomic_write_json, sha256_file
from mindie_knowledge.markdown import MAX_REFERENCE_BYTES as MAX_BODY_BYTES, MAX_METADATA_BYTES

REFERENCE_SCHEMA = "mindie-knowledge-references/1"
MAX_REFERENCE_BYTES = 64 * 1024 * 1024


def _read_bounded(path, maximum):
    with Path(path).open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise CorruptPack("prepared source exceeds its per-file read budget")
    return data


def _packed_body(archive, manifest, name):
    member = f"{manifest.pack['root_name']}/files/{name}"
    if archive.getinfo(member).file_size > MAX_BODY_BYTES:
        raise CorruptPack(f"released Markdown exceeds the {MAX_BODY_BYTES}-byte per-document budget")
    with archive.open(member) as stream:
        data = stream.read(MAX_BODY_BYTES + 1)
    if len(data) > MAX_BODY_BYTES:
        raise CorruptPack("released Markdown exceeds the per-document read budget")
    return data


def safe_relative(value: str) -> bool:
    return (isinstance(value, str) and bool(value) and not value.startswith(("/", "\\"))
            and not any(char in value for char in "\\:\x00")
            and all(part not in {"", ".", ".."} for part in value.split("/")))


def _public(value):
    from mindie_knowledge.contribution.public import _mask_text
    if isinstance(value, str):
        return _mask_text(value)[0]
    if isinstance(value, list):
        return [_public(item) for item in value]
    if isinstance(value, dict):
        return {_public(str(key)): _public(item) for key, item in value.items()}
    return value


def prepare_reference(path: str, raw: str, metadata: dict | None = None) -> tuple[str, dict]:
    """Prepare one public source before indexing; no original is mutated."""
    from mindie_knowledge import redact
    from mindie_knowledge.contribution.public import prepare_public_copy
    from mindie_knowledge.context import document_context
    from mindie_knowledge.markdown import normalized_sha256, retrieval_metadata
    if not safe_relative(path) or redact.scan_text(path):
        raise BuildError("corpus file path needs a safe public relative name")
    public = prepare_public_copy(raw)
    if public.blocked:
        raise BuildError(f"{path}: package public preparation failed: {public.reason}")
    if len(public.text.encode("utf-8")) > MAX_BODY_BYTES:
        raise BuildError(f"{path}: prepared body exceeds the {MAX_BODY_BYTES}-byte release document budget")
    metadata = metadata if isinstance(metadata, dict) else {}
    retrieval = retrieval_metadata(metadata.get("retrieval"), raw)
    if retrieval.get("ignored"):
        retrieval = {}
    for alias in retrieval.get("aliases", []):
        if isinstance(alias, dict) and isinstance(alias.get("scope"), list):
            alias["scope"] = [item for item in alias["scope"] if isinstance(item, str)]
    retrieval = _public({key: retrieval.get(key, []) for key in ("aliases", "topics")})
    retrieval["source_sha256"] = normalized_sha256(public.text)
    conditions = metadata.get("conditions")
    conditions = _public({str(key): str(value) for key, value in conditions.items()
                          if value is not None and str(value).strip().casefold() not in {"", "unknown", "未记录", "未知"}}) if isinstance(conditions, dict) else {}
    row = {"path": path, "source_sha256": normalized_sha256(public.text), "retrieval": retrieval,
           "conditions": conditions, "contexts": document_context(public.text, conditions)}
    if len(json.dumps(_metadata(row), ensure_ascii=False, indent=2).encode("utf-8")) + 1 > MAX_METADATA_BYTES:
        raise BuildError(f"{path}: prepared metadata exceeds the per-document budget")
    if redact.scan_text(json.dumps(row, ensure_ascii=False)):
        raise BuildError(f"{path}: reference metadata still needs public preparation")
    return public.text, row


def write_references(path: Path, *, source_git_sha: str, documents: list[dict]) -> dict:
    from mindie_knowledge import redact
    data = {"schema": REFERENCE_SCHEMA, "source_git_sha": source_git_sha,
            "redaction_profile": redact.REDACTION_PROFILE, "documents": documents}
    if len(json.dumps(data, ensure_ascii=False).encode()) > MAX_REFERENCE_BYTES:
        raise BuildError("prepared reference metadata exceeds the 64 MiB release budget")
    atomic_write_json(path, data)
    if path.stat().st_size > MAX_REFERENCE_BYTES:
        raise BuildError("prepared reference metadata exceeds the 64 MiB release budget")
    return {"file": path.name, "sha256": sha256_file(path), "size": path.stat().st_size,
            "source_git_sha": source_git_sha, "schema": REFERENCE_SCHEMA, "count": len(documents)}


def verify_references(path: Path, manifest, *, pack_path: Path | None = None) -> dict:
    """Validate asset, final-body hashes and context spans before activation."""
    spec = manifest.data.get("references")
    if not spec:
        return {}
    path = Path(path)
    if not path.is_file() or path.stat().st_size != spec["size"] or sha256_file(path) != spec["sha256"]:
        raise CorruptPack("reference asset integrity differs from the release manifest")
    if path.stat().st_size > MAX_REFERENCE_BYTES:
        raise CorruptPack("reference asset exceeds the supported size limit")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError, OSError) as exc:
        raise CorruptPack("reference asset is not readable JSON") from exc
    from mindie_knowledge import redact
    if not isinstance(data, dict) or set(data) != {"schema", "source_git_sha", "redaction_profile", "documents"}:
        raise CorruptPack("reference asset has unsupported fields")
    if (data["schema"] != REFERENCE_SCHEMA or data["source_git_sha"] != manifest.source_git_sha
            or data["redaction_profile"] != redact.REDACTION_PROFILE):
        raise CorruptPack("reference asset does not match the prepared source contract")
    rows = data["documents"]
    if not isinstance(rows, list) or len(rows) != spec["count"]:
        raise CorruptPack("reference document count differs")
    wanted = {item["path"]: item for item in manifest.content_files}
    by_path = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "source_sha256", "retrieval", "conditions", "contexts"}:
            raise CorruptPack("reference document has unsupported fields")
        name = row["path"]
        if not safe_relative(name) or name in by_path or name not in wanted:
            raise CorruptPack("reference document path is duplicate or outside the released body set")
        if (not isinstance(row["conditions"], dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in row["conditions"].items())
                or not isinstance(row["retrieval"], dict)):
            raise CorruptPack("reference conditions/retrieval is malformed")
        if len(json.dumps(_metadata(row), ensure_ascii=False, indent=2).encode("utf-8")) + 1 > MAX_METADATA_BYTES:
            raise CorruptPack("reference metadata exceeds the per-document budget")
        aliases = row["retrieval"].get("aliases")
        if not isinstance(aliases, list):
            raise CorruptPack("reference aliases must be a list")
        for alias in aliases:
            if isinstance(alias, str):
                continue
            if not isinstance(alias, dict) or set(alias) - {"text", "relation", "scope"}:
                raise CorruptPack("reference alias has unsupported fields")
            scope = alias.get("scope", [])
            if not isinstance(scope, str) and (not isinstance(scope, list) or not all(isinstance(item, str) for item in scope)):
                raise CorruptPack("reference alias topic scope is malformed")
        if redact.scan_text(json.dumps(row, ensure_ascii=False)):
            raise CorruptPack("reference metadata contains data outside the public preparation profile")
        by_path[name] = row
    if set(by_path) != set(wanted):
        raise CorruptPack("reference document set differs from the released bodies")
    if pack_path is not None:
        from mindie_knowledge.context import document_context
        from mindie_knowledge.markdown import normalized_sha256, retrieval_metadata
        with zipfile.ZipFile(pack_path) as archive:
            for name, row in by_path.items():
                try:
                    raw = _packed_body(archive, manifest, name).decode("utf-8")
                except (KeyError, UnicodeError, zipfile.BadZipFile) as exc:
                    raise CorruptPack("reference body is missing or cannot be decoded") from exc
                if row["source_sha256"] != normalized_sha256(raw):
                    raise CorruptPack("reference body hash differs from the packed final public body")
                retrieval = retrieval_metadata(row["retrieval"], raw)
                if retrieval != row["retrieval"]:
                    raise CorruptPack("reference aliases/topics are malformed or not bound to the packed body")
                if document_context(raw, row["conditions"]) != row["contexts"]:
                    raise CorruptPack("reference contexts do not describe the packed final public body")
    return by_path


def prepare_mount(state, manifest, pack_path: Path, *, references_path: Path | None = None, current=None) -> dict:
    """Extract verified bodies and prepared metadata before the pointer switches."""
    spec = manifest.data.get("references")
    rows = verify_references(references_path, manifest, pack_path=pack_path) if spec else {}
    metadata_status = "available" if spec else "unavailable_legacy"
    signature = spec["sha256"] if spec else None
    version = state.version_dir(manifest.version_id)
    if current and current.get("references_sha256") == signature and current.get("prepared_root"):
        root = Path(current["prepared_root"])
        try:
            if root.resolve().is_relative_to((version / "prepared").resolve()):
                valid = all(not (root / entry["path"]).is_symlink()
                            and not (root / entry["path"]).with_suffix(".meta.json").is_symlink()
                            and (root / entry["path"]).resolve().is_relative_to(root.resolve())
                            and (root / entry["path"]).with_suffix(".meta.json").resolve().is_relative_to(root.resolve())
                            and (root / entry["path"]).is_file() and sha256_file(root / entry["path"]) == entry["sha256"]
                            and json.loads((root / entry["path"]).with_suffix(".meta.json").read_text(encoding="utf-8")) == _metadata(rows.get(entry["path"], {}))
                            for entry in manifest.content_files)
                if valid and sha256_file(root / "prepared.json") == current.get("prepared_manifest_sha256"):
                    return {"prepared_root": str(root), "prepared_manifest_sha256": current["prepared_manifest_sha256"],
                            "references_sha256": signature, "metadata_status": metadata_status}
        except (OSError, ValueError):
            pass
    root = version / "prepared" / secrets.token_hex(8)
    root.mkdir(parents=True, exist_ok=False)
    try:
        prepared_files = []
        with zipfile.ZipFile(pack_path) as archive:
            for entry in manifest.content_files:
                name = entry["path"]
                if not safe_relative(name):
                    raise CorruptPack("unsafe released source path")
                raw = _packed_body(archive, manifest, name)
                if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                    raise CorruptPack("packed body changed before source preparation")
                target = root / Path(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
                atomic_write_json(target.with_suffix(".meta.json"), _metadata(rows.get(name, {})))
                prepared_files.append({**entry, "metadata_sha256": sha256_file(target.with_suffix(".meta.json"))})
        atomic_write_json(root / "prepared.json", {"source_git_sha": manifest.source_git_sha,
            "references_sha256": signature, "files": prepared_files})
        return {"prepared_root": str(root), "prepared_manifest_sha256": sha256_file(root / "prepared.json"),
                "references_sha256": signature, "metadata_status": metadata_status}
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _metadata(row):
    return {key: row[key] for key in ("conditions", "retrieval") if key in row}


def prepared_shared_documents(state_root: Path, *, current=None) -> Iterator:
    """Stream one activated generation, with bounded per-file reads.

    Failures can occur during iteration. Catalog owners should consume this
    inside their transaction and preserve the previous snapshot on failure.
    """
    from mindie_knowledge.distribution.sync import DistributionState
    from mindie_knowledge.markdown import document_from_text
    from mindie_knowledge.distribution.manifest import version_id_from_sha
    state = DistributionState(state_root)
    current = current if current is not None else state.read_current()
    if not current or not current.get("prepared_root"):
        return
    root = Path(current["prepared_root"])
    if not root.resolve().is_relative_to((state.version_dir(version_id_from_sha(current["source_git_sha"])) / "prepared").resolve()):
        raise CorruptPack("prepared source mount is outside the activated version")
    try:
        raw_prepared = _read_bounded(root / "prepared.json", MAX_REFERENCE_BYTES)
        if hashlib.sha256(raw_prepared).hexdigest() != current.get("prepared_manifest_sha256"):
            raise CorruptPack("prepared source manifest differs from the active pointer")
        prepared = json.loads(raw_prepared)
    except (OSError, ValueError) as exc:
        raise CorruptPack("activated prepared source mount is unavailable") from exc
    if prepared.get("source_git_sha") != current["source_git_sha"] or prepared.get("references_sha256") != current.get("references_sha256"):
        raise CorruptPack("prepared source mount and active pointer differ")
    for entry in prepared.get("files", []):
        if not safe_relative(entry["path"]):
            raise CorruptPack("unsafe prepared source path")
        path = root / entry["path"]
        if (path.is_symlink() or path.with_suffix(".meta.json").is_symlink()
                or not path.resolve().is_relative_to(root.resolve())
                or not path.with_suffix(".meta.json").resolve().is_relative_to(root.resolve())):
            raise CorruptPack("activated prepared source was replaced by a symlink")
        raw = _read_bounded(path, MAX_BODY_BYTES)
        metadata = _read_bounded(path.with_suffix(".meta.json"), MAX_METADATA_BYTES)
        if hashlib.sha256(raw).hexdigest() != entry["sha256"] or hashlib.sha256(metadata).hexdigest() != entry["metadata_sha256"]:
            raise CorruptPack("activated prepared source body differs from its verified snapshot")
        doc = document_from_text(raw.decode("utf-8"), path=path, layer="shared", root=root, metadata=json.loads(metadata))
        doc.uri = current["root_uri"].rstrip("/") + "/" + entry["path"]
        doc.source = {"git_sha": current["source_git_sha"]}
        yield doc
