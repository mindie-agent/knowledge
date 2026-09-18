"""Loopback FastEmbed/ONNX Runtime CPU embedding server.

OpenViking's built-in local provider is a different GGUF path and must not be
used as a silent substitute. This process only serves ``/v1/embeddings``.
Generation requests are rejected so capture never calls a second summarizer.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import shutil
import sys
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any

EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_DIMENSION = 384
DEFAULT_HOST = "127.0.0.1"


def _load_model(cache_dir: Path, *, local_files_only: bool = True):
    from fastembed import TextEmbedding

    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    return TextEmbedding(
        model_name=EMBEDDING_MODEL,
        cache_dir=str(cache_dir),
        threads=max(1, min(4, os.cpu_count() or 1)),
        providers=["CPUExecutionProvider"],
        local_files_only=local_files_only,
    )


@dataclass(frozen=True)
class PreparedModel:
    cache_dir: Path
    changed: bool
    validation: str


def model_fingerprint(cache_dir: Path) -> str | None:
    """Stable identity of prepared weights/tokenizer, excluding host metadata."""
    from mindie_knowledge.distribution.manifest import read_json

    record = read_json(Path(cache_dir) / "model-ready.json") or {}
    if record.get("model") != EMBEDDING_MODEL or not isinstance(record.get("files"), list):
        return None
    try:
        files = sorted((entry["path"], entry["sha256"]) for entry in record["files"])
        if not files or any(not isinstance(path, str) or not isinstance(digest, str)
                            for path, digest in files):
            return None
    except (KeyError, TypeError):
        return None
    return hashlib.sha256(json.dumps(files, ensure_ascii=False).encode("utf-8")).hexdigest()


def _safe_file(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if (not parts or relative.startswith("/") or "\\" in relative
            or any(part in {".", ".."} or ":" in part for part in parts)):
        raise ValueError("embedding model manifest contains an unsafe path")
    path = root.joinpath(*parts)
    # Hugging Face snapshot symlinks may resolve to a blob inside this cache.
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("embedding model file resolves outside its cache")
    return path


def _file_signature(path: Path) -> list[int]:
    info = path.stat()
    return [info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _pin_fingerprint(manifest: Any | None) -> str:
    embedding = getattr(manifest, "embedding", {}) if manifest is not None else {}
    return hashlib.sha256(json.dumps(embedding, sort_keys=True).encode()).hexdigest()


def _cached_model_ready(cache: Path, manifest: Any | None, *, verify: bool) -> str | None:
    from mindie_knowledge.distribution.manifest import read_json, sha256_file
    from mindie_knowledge.distribution.pack import verify_model_files

    record = read_json(cache / "model-ready.json") or {}
    if (record.get("model") != EMBEDDING_MODEL
            or record.get("dimension") != EMBEDDING_DIMENSION
            or (manifest is not None and record.get("pin") != _pin_fingerprint(manifest))
            or not record.get("files")):
        return None
    try:
        for entry in record["files"]:
            path = _safe_file(cache, entry["path"])
            if _file_signature(path) != entry["signature"]:
                return None
            if verify and sha256_file(path) != entry["sha256"]:
                return None
        if verify and manifest is not None and verify_model_files(cache, manifest):
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return str(record.get("validation") or "local-load")


def _verify_load(cache: Path) -> None:
    model = _load_model(cache, local_files_only=True)
    try:
        vectors = list(model.embed(["Knowledge embedding readiness."]))
        if (len(vectors) != 1 or len(vectors[0]) != EMBEDDING_DIMENSION
                or not all(math.isfinite(float(value)) for value in vectors[0])):
            raise RuntimeError("embedding readiness probe has an invalid vector")
    finally:
        del model
        gc.collect()


def _model_entries(cache: Path, manifest: Any | None) -> list[dict[str, Any]]:
    from mindie_knowledge.distribution.manifest import hash_model_tree, sha256_file

    entries = list((getattr(manifest, "embedding", {}) or {}).get("model_files") or [])
    if not entries:
        entries = hash_model_tree(cache)
    # Ref selection is part of the model, not merely download bookkeeping.
    paths = {str(entry["path"]) for entry in entries}
    paths.update(path.relative_to(cache).as_posix() for path in cache.glob("models--*/refs/main"))
    result = []
    for relative in sorted(paths):
        if relative == "model-ready.json":
            continue
        path = _safe_file(cache, relative)
        result.append({"path": relative, "sha256": sha256_file(path),
                       "signature": _file_signature(path)})
    if not result or not any(entry["path"].endswith(".onnx") for entry in result):
        raise RuntimeError("embedding cache contains no verified ONNX model")
    return result


def _check_pin(cache: Path, manifest: Any | None) -> str:
    from mindie_knowledge.distribution.pack import verify_model_files

    if manifest is not None and manifest.embedding.get("model_files"):
        for entry in manifest.embedding["model_files"]:
            _safe_file(cache, entry["path"])
        problems = verify_model_files(cache, manifest)
        if problems:
            raise RuntimeError("embedding model verification failed: " + "; ".join(problems[:3]))
        return "release-sha256"
    return "local-load"


def _check_previous_integrity(cache: Path, previous: dict[str, Any], manifest: Any | None) -> None:
    from mindie_knowledge.distribution.manifest import sha256_file

    if manifest is not None or previous.get("model") != EMBEDDING_MODEL:
        return
    for entry in previous.get("files") or []:
        path = _safe_file(cache, entry["path"])
        if sha256_file(path) != entry["sha256"]:
            raise RuntimeError("embedding cache differs from its last verified contents")


def _download_model(cache: Path, manifest: Any | None) -> None:
    """Use the official loader, or its exact HF revision when a release pins it."""
    entries = (getattr(manifest, "embedding", {}) or {}).get("model_files") or []
    groups: dict[tuple[str, str], list[str]] = {}
    for entry in entries:
        parts = PurePosixPath(entry["path"]).parts
        if len(parts) < 4 or not parts[0].startswith("models--") or parts[1] != "snapshots":
            raise RuntimeError("released embedding cache cannot be downloaded from its manifest layout")
        groups.setdefault((parts[0], parts[2]), []).append("/".join(parts[3:]))
    if groups:
        from huggingface_hub import snapshot_download

        for (repository, revision), files in groups.items():
            repo_id = repository.removeprefix("models--").replace("--", "/")
            snapshot_download(repo_id=repo_id, revision=revision, cache_dir=str(cache),
                              allow_patterns=files)
            ref = cache / repository / "refs" / "main"
            ref.parent.mkdir(parents=True, exist_ok=True)
            ref.write_text(revision, encoding="utf-8")
    else:
        from fastembed import TextEmbedding

        # The public registry includes pooled implementations as well as the
        # basic ONNX models. This multilingual model belongs to PooledEmbedding.
        description = TextEmbedding._get_model_description(EMBEDDING_MODEL)
        TextEmbedding.download_model(description, str(cache))


def _clear_owned_stage(stage: Path, cache: Path) -> None:
    """Discard only a derived stage next to this instance's cache."""
    if (stage.resolve().parent != cache.parent.resolve()
            or stage.name not in {f".{cache.name}-prepare-seed", f".{cache.name}-failed-download"}):
        raise RuntimeError("embedding cleanup target is outside its owned staging paths")
    if stage.exists():
        shutil.rmtree(stage)


def prepare_embedding_cache(
    cache_dir: Path, *, source_cache: Path | None = None,
    manifest: Any | None = None, verify: bool = False,
) -> PreparedModel:
    """Prepare an owned replacement before a caller stops its live instance.

    External caches are read-only seeds. Partial downloads are resumed in one
    owned stage; failed copied seeds are discarded. An existing cache is never
    overwritten until a replacement passes a local CPU load and optional pin.
    """
    from mindie_knowledge.distribution.manifest import atomic_write_json, read_json

    cache = Path(cache_dir)
    ready = _cached_model_ready(cache, manifest, verify=verify)
    if ready:
        return PreparedModel(cache, False, ready)
    cache.parent.mkdir(parents=True, exist_ok=True)
    previous = read_json(cache / "model-ready.json") or {}
    candidates = [cache]
    if source_cache is not None and Path(source_cache).resolve() != cache.resolve():
        candidates.append(Path(source_cache))
    stage: Path | None = None
    for source in candidates:
        if not source.is_dir():
            continue
        candidate = cache.parent / f".{cache.name}-prepare-seed"
        _clear_owned_stage(candidate, cache)
        candidate.mkdir()
        try:
            shutil.copytree(source, candidate, dirs_exist_ok=True, symlinks=False,
                            ignore=shutil.ignore_patterns("model-ready.json", "*.lock", ".locks"))
            _check_pin(candidate, manifest)
            _check_previous_integrity(candidate, previous, manifest)
            _verify_load(candidate)
            stage = candidate
            break
        except Exception:  # noqa: BLE001 - ONNX failures also reject a seed
            _clear_owned_stage(candidate, cache)
            continue
    if stage is None:
        stage = cache.parent / f".{cache.name}-prepare-download"
        if stage.resolve().parent != cache.parent.resolve():
            raise RuntimeError("embedding download stage resolves outside its instance")
        stage.mkdir(exist_ok=True)
        _download_model(stage, manifest)
        try:
            _check_pin(stage, manifest)
            _check_previous_integrity(stage, previous, manifest)
            _verify_load(stage)
        except Exception:
            failed = cache.parent / f".{cache.name}-failed-download"
            _clear_owned_stage(failed, cache)
            stage.rename(failed)
            raise
    validation = _check_pin(stage, manifest)
    atomic_write_json(stage / "model-ready.json", {
        "model": EMBEDDING_MODEL, "dimension": EMBEDDING_DIMENSION,
        "pin": _pin_fingerprint(manifest), "validation": validation,
        "files": _model_entries(stage, manifest), "verified_at": time.time(),
    })
    return PreparedModel(stage, True, validation)


def serve(
    *,
    host: str = DEFAULT_HOST,
    port: int,
    cache_dir: Path,
    metrics_path: Path | None = None,
) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("embedding server must bind loopback only")
    model = _load_model(cache_dir)
    counts: dict[str, Any] = {
        "requests": 0,
        "texts": 0,
        "characters": 0,
        "compute_s": 0.0,
        "generation_rejected": 0,
        "model": EMBEDDING_MODEL,
        "dimension": EMBEDDING_DIMENSION,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            del format, args

        def _reply(self, payload: Any, status: int = 200) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            if self.path in {"/health", "/v1/health"}:
                self._reply({"ok": True, **counts})
                return
            self._reply(counts)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not self.path.rstrip("/").endswith("/embeddings"):
                counts["generation_rejected"] += 1
                self._reply({"error": "generation disabled"}, 501)
                return
            texts = body.get("input")
            if isinstance(texts, str):
                texts = [texts]
            if not isinstance(texts, list) or not texts:
                self._reply({"error": "input is required"}, 400)
                return
            started = time.monotonic()
            vectors = list(model.embed([str(item) for item in texts]))
            elapsed = time.monotonic() - started
            counts["requests"] += 1
            counts["texts"] += len(texts)
            counts["characters"] += sum(len(str(item)) for item in texts)
            counts["compute_s"] += elapsed
            if metrics_path is not None:
                from mindie_diagnostics import get_recorder
                get_recorder("mindie-knowledge").event(
                    "DEBUG", "embedding.request", count=len(texts),
                    characters=sum(len(str(item)) for item in texts), duration_ms=elapsed * 1000,
                )
            self._reply(
                {
                    "object": "list",
                    "model": body.get("model") or EMBEDDING_MODEL,
                    "usage": {"prompt_tokens": 0, "total_tokens": 0},
                    "data": [
                        {
                            "object": "embedding",
                            "index": index,
                            "embedding": vector.tolist(),
                        }
                        for index, vector in enumerate(vectors)
                    ],
                }
            )

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(
        json.dumps(
            {
                "ready": True,
                "pid": os.getpid(),
                "host": host,
                "port": port,
                "model": EMBEDDING_MODEL,
                "dimension": EMBEDDING_DIMENSION,
            }
        ),
        flush=True,
    )
    httpd.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Loopback CPU embedding server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--metrics", default="")
    args = parser.parse_args(argv)
    serve(
        host=args.host,
        port=args.port,
        cache_dir=Path(args.cache_dir),
        metrics_path=Path(args.metrics) if args.metrics else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
