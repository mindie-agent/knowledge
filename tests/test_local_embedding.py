"""Model recovery stays in owned storage and precedes service replacement."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mindie_knowledge.distribution.manifest import ReleaseManifest
from mindie_knowledge.local import embedding
from mindie_knowledge.local.embedding import PreparedModel, prepare_embedding_cache
from mindie_knowledge.local.instance import LocalInstance


def model_files(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "model.onnx").write_bytes(b"known-good-model")
    (root / "tokenizer.json").write_text('{"valid": true}', encoding="utf-8")


@pytest.fixture
def loaders(monkeypatch):
    def probe(root):
        assert (root / "model.onnx").read_bytes() == b"known-good-model"
        json.loads((root / "tokenizer.json").read_text(encoding="utf-8"))

    load = Mock(side_effect=probe)
    download = Mock(side_effect=lambda root, manifest: model_files(root))
    monkeypatch.setattr(embedding, "_verify_load", load)
    monkeypatch.setattr(embedding, "_download_model", download)
    return load, download


def test_external_seed_is_read_only_and_owned_cache_is_warm(tmp_path, monkeypatch, loaders):
    source = tmp_path / "external"
    model_files(source)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in source.iterdir()}
    monkeypatch.setenv("MINDIE_KNOWLEDGE_EMBEDDING_CACHE", str(source))
    instance = LocalInstance(tmp_path / "instance")
    assert instance.cache_dir == tmp_path / "instance" / "embedding-cache"
    prepared = instance.prepare_model()
    assert prepared.changed and prepared.validation == "local-load"
    instance._activate_model(prepared)
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in source.iterdir()}
    loaders[0].reset_mock()
    assert not instance.prepare_model().changed
    assert not instance.prepare_model(verify=True).changed
    loaders[0].assert_not_called()
    loaders[1].assert_not_called()


@pytest.mark.parametrize("damage", ["missing", "truncated", "wrong-bytes", "tokenizer"])
def test_owned_damage_repaired_from_seed_without_overwriting_old(tmp_path, loaders, damage):
    source = tmp_path / "external"
    model_files(source)
    instance = LocalInstance(tmp_path / "instance")
    instance.source_cache = source
    instance._activate_model(instance.prepare_model())
    model = instance.cache_dir / "model.onnx"
    if damage == "missing":
        model.unlink()
    elif damage == "truncated":
        model.write_bytes(b"short")
    elif damage == "wrong-bytes":
        model.write_bytes(b"wrong-good-model")
    else:
        (instance.cache_dir / "tokenizer.json").write_text("invalid", encoding="utf-8")
    damaged = model.read_bytes() if model.exists() else None
    repaired = instance.prepare_model(verify=True)
    assert repaired.changed
    assert (model.read_bytes() if model.exists() else None) == damaged
    instance._activate_model(repaired)
    assert model.read_bytes() == b"known-good-model"
    assert not instance.prepare_model(verify=True).changed
    assert list(instance.state_root.glob(".embedding-cache-previous-*"))
    loaders[1].assert_not_called()


def test_cold_cache_downloads_and_probes_before_activation(tmp_path, loaders):
    cache = tmp_path / "instance" / "embedding-cache"
    prepared = prepare_embedding_cache(cache)
    assert prepared.changed
    assert not cache.exists()
    loaders[1].assert_called_once()
    loaders[0].assert_called_once()
    record = json.loads((prepared.cache_dir / "model-ready.json").read_text())
    assert record["validation"] == "local-load"
    assert all(entry["sha256"] for entry in record["files"])


def test_cold_download_resolves_pooled_model_from_full_fastembed_registry(tmp_path, monkeypatch):
    from fastembed import TextEmbedding

    download = Mock()
    monkeypatch.setattr(TextEmbedding, "download_model", download)
    embedding._download_model(tmp_path, None)
    description, cache = download.call_args.args
    assert description.model == embedding.EMBEDDING_MODEL
    assert description.dim == embedding.EMBEDDING_DIMENSION
    assert description.sources.hf == "qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
    assert cache == str(tmp_path)


def test_release_checksum_rejects_loadable_bad_seed(tmp_path, loaders):
    source = tmp_path / "external"
    model_files(source)
    original = (source / "model.onnx").read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    (source / "model.onnx").write_bytes(b"wrong-good-model")
    manifest = ReleaseManifest({"embedding": {
        "model": embedding.EMBEDDING_MODEL, "dimensions": embedding.EMBEDDING_DIMENSION,
        "model_files": [{"path": "model.onnx", "sha256": digest, "size": len(original)}],
    }})
    prepared = prepare_embedding_cache(tmp_path / "owned", source_cache=source, manifest=manifest)
    assert prepared.validation == "release-sha256"
    loaders[1].assert_called_once()
    assert (source / "model.onnx").read_bytes() == b"wrong-good-model"


def test_failed_download_preserves_live_instance_and_cache(tmp_path, monkeypatch):
    instance = LocalInstance(tmp_path)
    instance.cache_dir.mkdir()
    sentinel = instance.cache_dir / "old.onnx"
    sentinel.write_bytes(b"old")
    monkeypatch.setattr(instance, "describe", lambda: {"live": True, "pid": {"embedding_pid": 10}})
    monkeypatch.setattr(instance, "_credentials", lambda: {"data_key": "private"})
    monkeypatch.setattr(instance, "prepare_model", Mock(side_effect=RuntimeError("download unavailable")))
    stop = Mock()
    monkeypatch.setattr(instance, "_stop_owned", stop)
    with pytest.raises(RuntimeError, match="download unavailable"):
        instance.ensure(verify_model=True)
    stop.assert_not_called()
    assert sentinel.read_bytes() == b"old"


def test_manifest_cannot_resolve_outside_cache(tmp_path, loaders):
    manifest = ReleaseManifest({"embedding": {"model_files": [
        {"path": "../outside.onnx", "sha256": "a" * 64},
    ]}})
    with pytest.raises(ValueError, match="unsafe path"):
        prepare_embedding_cache(tmp_path / "owned", manifest=manifest)
    assert not (tmp_path / "outside.onnx").exists()


def test_starting_pid_is_saved_before_each_health_wait(tmp_path, monkeypatch):
    from mindie_knowledge.local import instance as module

    instance = LocalInstance(tmp_path)
    monkeypatch.setattr(instance, "describe", lambda: {"live": False, "pid": {}})
    monkeypatch.setattr(instance, "prepare_model", lambda *args, **kwargs: PreparedModel(instance.cache_dir, False, "local-load"))
    monkeypatch.setattr(instance, "_credentials", lambda: {"data_key": "private", "root_key": "private"})
    monkeypatch.setattr(module, "which", lambda name: "openviking-server")
    processes = [SimpleNamespace(pid=101), SimpleNamespace(pid=102)]
    monkeypatch.setattr(module.subprocess, "Popen", Mock(side_effect=processes))
    stopped = Mock(return_value=True)
    monkeypatch.setattr(module, "stop_owned_pid", stopped)
    waits = []

    def wait(proc, url, **kwargs):
        record = json.loads(instance.pid_path.read_text())
        assert record["status"] == "starting"
        assert record["embedding_pid"] == 101
        assert record["embedding_marker"] == str(instance.metrics_path)
        waits.append(proc.pid)
        if proc.pid == 102:
            assert record["openviking_pid"] == 102
            raise SystemExit("interrupted during start")
        assert "openviking_pid" not in record

    monkeypatch.setattr(instance, "_wait_proc_health", wait)
    with pytest.raises(SystemExit, match="interrupted"):
        instance.ensure()
    assert waits == [101, 102]
    assert {call.args[0] for call in stopped.call_args_list} == {101, 102}
    assert instance._read_pid()["openviking_pid"] == 102


def test_successful_start_marks_pid_running(tmp_path, monkeypatch):
    from mindie_knowledge.local import instance as module

    instance = LocalInstance(tmp_path)
    # Legacy log storage failure cannot gate the new process-owned diagnostic sink.
    instance.log_dir.write_text("unavailable legacy log directory")
    monkeypatch.setattr(instance, "describe", lambda: {"live": False, "pid": {}})
    monkeypatch.setattr(instance, "prepare_model", lambda *args, **kwargs: PreparedModel(instance.cache_dir, False, "local-load"))
    monkeypatch.setattr(instance, "_credentials", lambda: {"data_key": "private", "root_key": "private"})
    monkeypatch.setattr(module, "which", lambda name: "openviking-server")
    monkeypatch.setattr(module.subprocess, "Popen", Mock(side_effect=[SimpleNamespace(pid=101), SimpleNamespace(pid=102)]))
    monkeypatch.setattr(instance, "_wait_proc_health", Mock())
    validate_key = Mock()
    monkeypatch.setattr(instance, "_ensure_data_key", validate_key)
    instance.ensure()
    assert instance._read_pid()["status"] == "running"
    validate_key.assert_called_once()
    assert len(module.subprocess.Popen.call_args_list) == 2
    for launch in module.subprocess.Popen.call_args_list:
        assert launch.kwargs["stdin"] == module.subprocess.DEVNULL
        assert launch.kwargs["stdout"] == module.subprocess.DEVNULL
        assert launch.kwargs["stderr"] == module.subprocess.DEVNULL
        assert launch.args[0][1:3] == ["-m", "mindie_knowledge.local.daemon"]
        if module.os.name == "nt":
            assert launch.kwargs["creationflags"] & module.subprocess.CREATE_NO_WINDOW


def test_partial_download_reuses_one_stage(tmp_path, monkeypatch):
    seen = []

    def interrupted(cache, manifest):
        seen.append(cache)
        partial = cache / "model.incomplete"
        partial.write_bytes(partial.read_bytes() + b"chunk" if partial.exists() else b"chunk")
        raise OSError("offline")

    monkeypatch.setattr(embedding, "_download_model", interrupted)
    for _ in range(3):
        with pytest.raises(OSError, match="offline"):
            prepare_embedding_cache(tmp_path / "embedding-cache")
    assert len(set(seen)) == 1
    assert (seen[0] / "model.incomplete").read_bytes() == b"chunkchunkchunk"
    assert len(list(tmp_path.glob(".embedding-cache-prepare-*"))) == 1


def test_repeated_invalid_seed_does_not_accumulate_copies(tmp_path, monkeypatch, loaders):
    source = tmp_path / "external"
    model_files(source)
    (source / "model.onnx").write_bytes(b"invalid")
    monkeypatch.setattr(embedding, "_download_model", Mock(side_effect=OSError("offline")))
    for _ in range(3):
        with pytest.raises(OSError, match="offline"):
            prepare_embedding_cache(tmp_path / "embedding-cache", source_cache=source)
    assert not (tmp_path / ".embedding-cache-prepare-seed").exists()
    assert len(list(tmp_path.glob(".embedding-cache-prepare-*"))) == 1
    assert (source / "model.onnx").read_bytes() == b"invalid"


def test_release_pin_rejects_wrong_active_snapshot(tmp_path, monkeypatch):
    repository = "models--example--model"
    wanted, wrong = "a" * 40, "b" * 40
    source = tmp_path / "external"
    for revision in (wanted, wrong):
        model_files(source / repository / "snapshots" / revision)
    ref = source / repository / "refs" / "main"
    ref.parent.mkdir()
    ref.write_text(wrong)
    path = f"{repository}/snapshots/{wanted}/model.onnx"
    manifest = ReleaseManifest({"embedding": {"model_files": [
        {"path": path, "sha256": hashlib.sha256(b"known-good-model").hexdigest()},
    ]}})
    download = Mock(side_effect=OSError("offline"))
    probe = Mock()
    monkeypatch.setattr(embedding, "_download_model", download)
    monkeypatch.setattr(embedding, "_verify_load", probe)
    with pytest.raises(OSError, match="offline"):
        prepare_embedding_cache(tmp_path / "owned", source_cache=source, manifest=manifest)
    probe.assert_not_called()
    assert ref.read_text() == wrong


def test_active_manifest_uses_distribution_pointer(tmp_path):
    from tests.distribution.helpers import GIT_SHA, make_release_dir
    from mindie_knowledge.distribution.manifest import shared_root_uri, version_id_from_sha
    from mindie_knowledge.distribution.sync import CURRENT_SCHEMA, DistributionState

    instance = LocalInstance(tmp_path)
    state = DistributionState(tmp_path)
    version = version_id_from_sha(GIT_SHA)
    release = make_release_dir(state.version_dir(version))
    state.write_current({"schema": CURRENT_SCHEMA, "version_id": version,
                         "source_git_sha": GIT_SHA, "root_uri": shared_root_uri(version),
                         "manifest_path": str(release / "release.json")})
    assert instance._model_manifest().source_git_sha == GIT_SHA
    outside = make_release_dir(tmp_path / "outside")
    current = state.read_current()
    state.write_current({**current, "manifest_path": str(outside / "release.json")})
    assert instance._model_manifest() is None


def test_missing_release_metadata_keeps_verified_cache_warm(tmp_path, loaders):
    source = tmp_path / "external"
    model_files(source)
    manifest = ReleaseManifest({"embedding": {"model_files": [
        {"path": "model.onnx", "sha256": hashlib.sha256(b"known-good-model").hexdigest()},
    ]}})
    instance = LocalInstance(tmp_path / "instance")
    instance.source_cache = source
    instance._activate_model(instance.prepare_model(manifest))
    loaders[0].reset_mock()
    assert not instance.prepare_model(verify=True).changed
    loaders[0].assert_not_called()


def test_model_fingerprint_ignores_metadata_and_detects_weights(tmp_path):
    from mindie_knowledge.local.openviking import OpenVikingBackend

    instance = LocalInstance(tmp_path)
    instance.cache_dir.mkdir()
    path = instance.cache_dir / "model-ready.json"
    record = {"model": embedding.EMBEDDING_MODEL, "files": [
        {"path": "model.onnx", "sha256": "a" * 64, "signature": [100, 1, 1]},
        {"path": "tokenizer.json", "sha256": "b" * 64, "signature": [5, 2, 2]},
    ], "verified_at": 1}
    path.write_text(json.dumps(record))
    backend = OpenVikingBackend(SimpleNamespace(state_root=tmp_path))
    before = backend.index_fingerprint()
    record["verified_at"] = 2
    record["files"].reverse()
    record["files"][0]["signature"] = [5, 3, 4]
    path.write_text(json.dumps(record))
    assert backend.index_fingerprint() == before
    record["files"][0]["sha256"] = "c" * 64
    path.write_text(json.dumps(record))
    assert backend.index_fingerprint() != before
