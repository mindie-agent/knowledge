"""Shared helpers for distribution tests: synthetic packs, releases, fake clients.

The fake client mirrors only the native OpenViking 0.4.19 surface that
``mindie_knowledge.distribution`` uses, so logic tests never start a server.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

from mindie_knowledge.distribution.manifest import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    OPENVIKING_VERSION,
    RELEASE_SCHEMA,
    SHARED_PARENT_URI,
    content_digest,
    utc_now,
    version_id_from_sha,
)

GIT_SHA = "a" * 40
GIT_SHA_2 = "b" * 40
GIT_SHA_3 = "c" * 40

DOC_A = "# Alpha note\n\nThe alpha body explains the fix.\n"
DOC_B = "# Beta note\n\nThe beta body records the measurement.\n"


def make_corpus(files: dict[str, str] | None = None) -> list[dict[str, Any]]:
    docs = files if files is not None else {"alpha.md": DOC_A, "notes/beta.md": DOC_B}
    entries = []
    for path in sorted(docs):
        raw = docs[path].encode("utf-8")
        entries.append({"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw), "text": docs[path]})
    return entries


def make_pack(
    pack_path: Path,
    entries: list[dict[str, Any]],
    *,
    root_name: str | None = None,
    dimensions: int = EMBEDDING_DIMENSION,
    dtype: str = "float32",
    byte_order: str = "little",
    model: str = EMBEDDING_MODEL,
    provider: str = EMBEDDING_PROVIDER,
    corrupt_dense: bool = False,
    omit_entry: str | None = None,
    extra_members: dict[str, bytes] | None = None,
) -> dict[str, Any]:
    """Write a synthetic OVPack zip; returns the embedded index block."""

    root = root_name or version_id_from_sha(GIT_SHA)
    count = len(entries) + 1
    dense_raw = b"\x00\x00\x80?" * dimensions * count
    dense_sha = hashlib.sha256(dense_raw).hexdigest()
    if corrupt_dense:
        dense_raw = dense_raw + b"corrupt"  # content no longer matches the index sha
    rows = []
    for position, (path, kind, level) in enumerate(
        [("", "directory", 0)] + [(entry["path"], "file", 2) for entry in entries]
    ):
        rows.append({"record_id": f"r{position + 1:06d}", "path": path, "kind": kind,
                     "level": level, "vector": {"dense": {"offset": position * dimensions,
                                                             "dimensions": dimensions}}})
    records_raw = ("\n".join(json.dumps(row) for row in rows) + "\n").encode("utf-8")
    index = {
        "dense": {
            "byte_order": byte_order,
            "count": len(entries) + 1,
            "dimensions": dimensions,
            "dtype": dtype,
            "embedding": {
                "dimensions": dimensions,
                "input": "multimodal",
                "model": model,
                "provider": provider,
            },
            "path": "_ovpack/dense.f32",
            "sha256": dense_sha,
        },
        "records": {
            "count": len(entries) + 1,
            "path": "_ovpack/index_records.jsonl",
            "sha256": hashlib.sha256(records_raw).hexdigest(),
        },
    }
    manifest_entries = [{"kind": "directory", "path": ""}]
    with zipfile.ZipFile(pack_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{root}/_ovpack/dense.f32", dense_raw)
        archive.writestr(f"{root}/_ovpack/index_records.jsonl", records_raw)
        for entry in entries:
            if entry["path"] == omit_entry:
                continue
            # Real 0.4.19 packs store content under <root>/files/.
            archive.writestr(f"{root}/files/{entry['path']}", entry["text"].encode("utf-8"))
            manifest_entries.append(
                {"kind": "file", "path": entry["path"], "sha256": entry["sha256"], "size": entry["size"]}
            )
        for name, data in (extra_members or {}).items():
            info = zipfile.ZipInfo(name)
            # Preserve intentionally malformed raw spellings on Windows too.
            info.filename = name
            archive.writestr(info, data)
        embedded = {
            "content_sha256": "0" * 64,
            "entries": manifest_entries,
            "index": index,
            "format_version": 3,
            "kind": "openviking.ovpack",
            "root": {"name": root, "scope": "resources", "uri": f"viking://resources/build/{root}"},
        }
        archive.writestr(f"{root}/_ovpack/manifest.json", json.dumps(embedded).encode("utf-8"))
    return index


def make_manifest(
    pack_path: Path,
    entries: list[dict[str, Any]],
    index: dict[str, Any],
    *,
    sha: str = GIT_SHA,
    model: str = EMBEDDING_MODEL,
    dimensions: int = EMBEDDING_DIMENSION,
    provider: str = EMBEDDING_PROVIDER,
    openviking: str = OPENVIKING_VERSION,
    vector_mode: str = "require",
    root_name: str | None = None,
    model_files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    version_id = version_id_from_sha(sha)
    content_files = [
        {"path": e["path"], "sha256": e["sha256"], "size": e["size"]} for e in entries
    ]
    embedding: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "dimensions": dimensions,
    }
    if model_files is not None:
        embedding["model_files"] = model_files
    return {
        "schema": RELEASE_SCHEMA,
        "version_id": version_id,
        "source": {"git_sha": sha, "corpus_path": "."},
        "build": {
            "openviking": openviking,
            "openviking_sdk": "0.1.10",
            "builder": "mindie-knowledge-distribution",
            "builder_version": "0.2.0",
            "platform": "macOS arm64",
            "device": "cpu",
            "created_at": utc_now(),
        },
        "embedding": embedding,
        "pack": {
            "file": pack_path.name,
            "sha256": hashlib.sha256(pack_path.read_bytes()).hexdigest(),
            "size": pack_path.stat().st_size,
            "format": "openviking-ovpack",
            "vector_mode": vector_mode,
            "root_name": root_name or version_id,
            "index": index,
        },
        "content": {
            "files": content_files,
            "count": len(content_files),
            "content_sha256": content_digest(content_files),
        },
    }


def make_release_dir(
    base: Path,
    *,
    sha: str = GIT_SHA,
    files: dict[str, str] | None = None,
    **pack_kwargs: Any,
) -> Path:
    """A local release directory: release.json + one pack asset."""

    base.mkdir(parents=True, exist_ok=True)
    entries = make_corpus(files)
    version_id = version_id_from_sha(sha)
    pack_path = base / f"corpus-{version_id}.ovpack"
    index = make_pack(pack_path, entries, root_name=version_id, **pack_kwargs)
    manifest = make_manifest(pack_path, entries, index, sha=sha, root_name=version_id)
    (base / "release.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return base


class FakeClient:
    """In-memory stand-in for the native SyncHTTPClient surface used here."""

    def __init__(self, *, fail_import: bool = False, consistency_ok: bool = True):
        self.trees: dict[str, dict[str, str]] = {}
        self.expected_counts: dict[str, int] = {}
        self.calls: list[tuple[str, Any]] = []
        self.fail_import = fail_import
        self.consistency_ok = consistency_ok
        self.import_kwargs: dict[str, Any] = {}
        self.on_wait_processed: Any = None
        self.corrupt_vectors: set[str] = set()

    def stat(self, uri: str) -> dict[str, Any]:
        self.calls.append(("stat", uri))
        if any(tree == uri or tree.startswith(uri + "/") for tree in self.trees):
            return {"uri": uri}
        raise RuntimeError(f"NOT_FOUND: {uri}")

    def mkdir(self, uri: str, description: str | None = None) -> None:
        self.calls.append(("mkdir", uri))
        if uri in self.trees:
            raise RuntimeError("ALREADY_EXISTS")
        self.trees[uri] = {}

    def write(self, uri: str, content: str, wait: bool = False, options: dict | None = None, **kw: Any) -> dict:
        self.calls.append(("write", {"uri": uri, "options": options}))
        root = uri.rsplit("/", 1)[0]
        self.trees.setdefault(root, {})[uri] = content
        return {"uri": uri}

    def wait_processed(self, timeout: float | None = None) -> dict[str, Any]:
        self.calls.append(("wait_processed", timeout))
        if callable(self.on_wait_processed):
            hook = self.on_wait_processed
            self.on_wait_processed = None
            hook()
        return {"Embedding": {"processed": 1, "requeue_count": 0, "error_count": 0, "errors": []}}

    def check_consistency(self, uri: str) -> dict[str, Any]:
        self.calls.append(("check_consistency", uri))
        count = self.expected_counts.get(uri, len(self.trees.get(uri, {})))
        return {"ok": self.consistency_ok, "expected_count": count, "missing_record_count": 0}

    def export_ovpack(self, uri: str, to: str, include_vectors: bool = False) -> str:
        self.calls.append(("export_ovpack", {"uri": uri, "include_vectors": include_vectors}))
        if uri not in self.trees:
            raise RuntimeError(f"NOT_FOUND: {uri}")
        root = uri.rsplit("/", 1)[-1]
        entries = make_corpus(self.trees[uri])
        make_pack(Path(to), entries, root_name=root)
        if uri in self.corrupt_vectors:
            with zipfile.ZipFile(to) as archive:
                members = {name: archive.read(name) for name in archive.namelist()}
            dense_path = f"{root}/_ovpack/dense.f32"
            members[dense_path] = b"\x00\x00\x00@" + members[dense_path][4:]
            manifest_path = f"{root}/_ovpack/manifest.json"
            embedded = json.loads(members[manifest_path])
            embedded["index"]["dense"]["sha256"] = hashlib.sha256(members[dense_path]).hexdigest()
            members[manifest_path] = json.dumps(embedded).encode()
            with zipfile.ZipFile(to, "w") as archive:
                for name, raw in members.items():
                    archive.writestr(name, raw)
        return to

    def import_ovpack(
        self,
        file_path: str,
        parent: str,
        on_conflict: str | None = None,
        vector_mode: str | None = None,
    ) -> str:
        self.calls.append(("import_ovpack", {"file_path": file_path, "parent": parent}))
        self.import_kwargs = {"on_conflict": on_conflict, "vector_mode": vector_mode}
        if self.fail_import:
            raise RuntimeError("native import exploded")
        version_id = Path(file_path).stem.removeprefix("corpus-")
        root_uri = f"{parent.rstrip('/')}/{version_id}"
        with zipfile.ZipFile(file_path) as archive:
            prefix = f"{version_id}/files/"
            docs = {
                name[len(prefix):]: archive.read(name).decode("utf-8")
                for name in archive.namelist()
                if name.startswith(prefix) and name.endswith(".md")
            }
            embedded = json.loads(archive.read(f"{version_id}/_ovpack/manifest.json"))
        if root_uri in self.trees and on_conflict == "fail":
            raise RuntimeError("ALREADY_EXISTS: target exists")
        self.trees[root_uri] = docs
        dense = (embedded.get("index") or {}).get("dense") or {}
        if isinstance(dense.get("count"), int):
            self.expected_counts[root_uri] = dense["count"]
        return root_uri

    def find(self, query: str, target_uri: str = "", limit: int = 10, options: dict | None = None, **kw: Any) -> dict[str, Any]:
        self.calls.append(("find", {"query": query, "target_uri": target_uri}))
        docs = self.trees.get(target_uri, {})
        return {
            "resources": [
                {"uri": uri, "score": 1.0, "content": content}
                for uri, content in sorted(docs.items())[:limit]
            ]
        }

    def read(self, uri: str) -> str:
        for tree in self.trees.values():
            if uri in tree:
                return tree[uri]
        raise RuntimeError(f"NOT_FOUND: {uri}")

    def rm(self, uri: str, recursive: bool = False, wait: bool = False, timeout: float | None = None) -> None:
        self.calls.append(("rm", uri))
        doomed = [tree for tree in self.trees if tree == uri or tree.startswith(uri + "/")]
        if not doomed:
            raise RuntimeError(f"NOT_FOUND: {uri}")
        for tree in doomed:
            del self.trees[tree]

    def close(self) -> None:
        self.calls.append(("close", None))


def embedding_info(model: str = EMBEDDING_MODEL, dimension: int = EMBEDDING_DIMENSION) -> dict[str, Any]:
    return {"model": model, "dimension": dimension, "provider": EMBEDDING_PROVIDER}


def metrics_reader_from(counter: dict[str, int]):
    def read() -> dict[str, Any]:
        return dict(counter)

    return read


SHARED_PARENT = SHARED_PARENT_URI
