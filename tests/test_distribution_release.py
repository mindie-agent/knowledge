"""Release adaptation and public GitHub source validation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from distribution.helpers import make_corpus, make_manifest, make_pack

from mindie_knowledge.distribution.errors import CorruptPack, ReleaseError, SourceUnavailable
from mindie_knowledge.distribution.release import (
    LocalReleaseSource,
    make_release,
    publish_release,
    source_from_location,
)


def _build_outputs(tmp_path: Path):
    entries = make_corpus()
    pack_path = tmp_path / "corpus-build.ovpack"
    index = make_pack(pack_path, entries)
    manifest = make_manifest(pack_path, entries, index)
    return pack_path, manifest


def test_make_release_layout(tmp_path):
    pack_path, manifest = _build_outputs(tmp_path)
    out = make_release(pack_path=pack_path, build_manifest=manifest, out_dir=tmp_path / "rel")
    assert (out / "release.json").is_file()
    assert (out / manifest["pack"]["file"]).is_file()
    snapshot = LocalReleaseSource(out).fetch()
    assert snapshot.manifest.source_git_sha == manifest["source"]["git_sha"]
    assert snapshot.pack_path.name == manifest["pack"]["file"]


def test_make_release_rejects_pack_manifest_mismatch(tmp_path):
    pack_path, manifest = _build_outputs(tmp_path)
    other = tmp_path / "other.ovpack"
    other.write_bytes(b"different bytes")
    with pytest.raises(ReleaseError, match="rebuild"):
        make_release(pack_path=other, build_manifest=manifest, out_dir=tmp_path / "rel")


def test_make_release_rejects_invalid_manifest(tmp_path):
    pack_path, manifest = _build_outputs(tmp_path)
    manifest["pack"]["vector_mode"] = "auto"
    with pytest.raises(CorruptPack):
        make_release(pack_path=pack_path, build_manifest=manifest, out_dir=tmp_path / "rel")


def test_local_source_errors_are_actionable(tmp_path):
    with pytest.raises(SourceUnavailable, match="not reachable"):
        LocalReleaseSource(tmp_path / "absent").fetch()
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SourceUnavailable, match="incomplete"):
        LocalReleaseSource(empty).fetch()
    pack_path, manifest = _build_outputs(tmp_path)
    out = make_release(pack_path=pack_path, build_manifest=manifest, out_dir=tmp_path / "rel")
    (out / manifest["pack"]["file"]).unlink()
    with pytest.raises(SourceUnavailable, match="missing"):
        LocalReleaseSource(out).fetch()


def test_network_sources_are_disabled(tmp_path):
    with pytest.raises(SourceUnavailable, match="disabled"):
        source_from_location("https://example.invalid/releases/latest")
    local = source_from_location(tmp_path)
    assert isinstance(local, LocalReleaseSource)


def test_publish_requires_a_valid_complete_directory(tmp_path):
    with pytest.raises(SourceUnavailable):
        publish_release(tmp_path, repository="example/corpus")
