"""Manifest contract: structure, pinned versions and model identity."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from distribution.helpers import GIT_SHA, make_corpus, make_manifest, make_pack

from mindie_knowledge.distribution.errors import CorruptPack, IncompatiblePack
from mindie_knowledge.distribution.manifest import (
    EMBEDDING_MODEL,
    ExpectedContract,
    atomic_write_json,
    content_digest,
    read_json,
    validate_release_manifest,
    version_id_from_sha,
)


def _valid_manifest(tmp_path: Path, **overrides):
    entries = make_corpus()
    pack_path = tmp_path / "corpus.ovpack"
    index = make_pack(pack_path, entries)
    data = make_manifest(pack_path, entries, index, **overrides)
    return data


def test_version_id_requires_git_sha():
    assert version_id_from_sha(GIT_SHA) == "v" + GIT_SHA[:12]
    for bad in ("", "xyz", "A" * 40, "a" * 39, "g" * 40):
        with pytest.raises(CorruptPack):
            version_id_from_sha(bad)


def test_valid_manifest_passes(tmp_path):
    manifest = validate_release_manifest(_valid_manifest(tmp_path), expected=ExpectedContract())
    assert manifest.version_id == "v" + GIT_SHA[:12]
    assert manifest.source_git_sha == GIT_SHA
    assert manifest.embedding["model"] == EMBEDDING_MODEL
    assert len(manifest.content_files) == 2


@pytest.mark.parametrize("asset", ["../escape.ovpack", "C:\\escape.ovpack", "subdir/pack.ovpack"])
def test_release_asset_stays_inside_its_local_version_directory(tmp_path, asset):
    data = _valid_manifest(tmp_path)
    data["pack"]["file"] = asset
    with pytest.raises(CorruptPack, match="single asset filename"):
        validate_release_manifest(data, expected=ExpectedContract())


def test_manifest_requires_dense_index(tmp_path):
    data = _valid_manifest(tmp_path)
    data["pack"]["index"] = {"records": {"count": 1}}
    with pytest.raises(CorruptPack, match="dense"):
        validate_release_manifest(data, expected=ExpectedContract())


def test_manifest_requires_vector_mode_require(tmp_path):
    data = _valid_manifest(tmp_path, vector_mode="auto")
    with pytest.raises(CorruptPack, match="vector_mode"):
        validate_release_manifest(data, expected=ExpectedContract())


def test_manifest_rejects_silent_reembed_pack(tmp_path):
    data = _valid_manifest(tmp_path)
    data["pack"]["index"]["dense"]["count"] = 0
    with pytest.raises(CorruptPack, match="dense"):
        validate_release_manifest(data, expected=ExpectedContract())


def test_openviking_version_mismatch_is_incompatible(tmp_path):
    data = _valid_manifest(tmp_path, openviking="0.4.18")
    with pytest.raises(IncompatiblePack, match="0.4.18"):
        validate_release_manifest(data, expected=ExpectedContract())


def test_model_mismatch_is_incompatible(tmp_path):
    data = _valid_manifest(tmp_path, model="some-other-model")
    with pytest.raises(IncompatiblePack, match="model"):
        validate_release_manifest(data, expected=ExpectedContract())


def test_dimension_mismatch_is_incompatible(tmp_path):
    data = _valid_manifest(tmp_path, dimensions=768)
    with pytest.raises(IncompatiblePack, match="dimension"):
        validate_release_manifest(data, expected=ExpectedContract())


def test_provider_mismatch_is_incompatible(tmp_path):
    data = _valid_manifest(tmp_path, provider="native-gguf")
    with pytest.raises(IncompatiblePack, match="provider"):
        validate_release_manifest(data, expected=ExpectedContract())


def test_version_id_must_derive_from_git_sha(tmp_path):
    data = _valid_manifest(tmp_path)
    data["version_id"] = "vdeadbeef00"
    with pytest.raises(CorruptPack, match="derive"):
        validate_release_manifest(data, expected=ExpectedContract())


def test_git_sha_must_be_full_hex(tmp_path):
    data = _valid_manifest(tmp_path)
    data["source"]["git_sha"] = "main"
    with pytest.raises(CorruptPack, match="git_sha"):
        validate_release_manifest(data, expected=ExpectedContract())


def test_contract_from_live_embedding_info():
    contract = ExpectedContract.from_embedding_info({"model": EMBEDDING_MODEL, "dimension": 384})
    assert contract.embedding_model == EMBEDDING_MODEL
    assert contract.embedding_dimension == 384
    with pytest.raises(IncompatiblePack, match="model"):
        ExpectedContract.from_embedding_info({"dimension": 384})
    with pytest.raises(IncompatiblePack, match="dimension"):
        ExpectedContract.from_embedding_info({"model": EMBEDDING_MODEL})


def test_atomic_write_and_read_json(tmp_path):
    path = tmp_path / "state" / "current.json"
    atomic_write_json(path, {"schema": "x", "n": 1})
    assert read_json(path) == {"schema": "x", "n": 1}
    path.write_text("{not json", encoding="utf-8")
    assert read_json(path) is None
    assert read_json(tmp_path / "absent.json") is None


def test_content_digest_is_order_sensitive_and_stable():
    entries = [{"path": "a.md", "sha256": "0" * 64}, {"path": "b.md", "sha256": "1" * 64}]
    assert content_digest(entries) == content_digest(list(entries))
    assert content_digest(entries) != content_digest(list(reversed(entries)))
