"""OVPack verification: integrity, archive safety, dense contract, content."""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from distribution.helpers import GIT_SHA, make_corpus, make_manifest, make_pack

from mindie_knowledge.distribution.errors import CorruptPack, IncompatiblePack
from mindie_knowledge.distribution.manifest import ExpectedContract, validate_release_manifest
from mindie_knowledge.distribution.pack import inspect_pack, verify_imported_pack, verify_model_files, verify_pack


def _verified_inputs(tmp_path: Path, **pack_kwargs):
    entries = make_corpus()
    pack_path = tmp_path / "corpus.ovpack"
    index = make_pack(pack_path, entries, **pack_kwargs)
    manifest = validate_release_manifest(
        make_manifest(pack_path, entries, index), expected=ExpectedContract()
    )
    return pack_path, manifest


def test_good_pack_verifies(tmp_path):
    pack_path, manifest = _verified_inputs(tmp_path)
    info = verify_pack(pack_path, manifest, expected=ExpectedContract())
    assert info.root_name == "v" + GIT_SHA[:12]
    assert info.dense["dimensions"] == 384


def test_runtime_export_record_order_and_ids_do_not_change_integrity(tmp_path):
    import hashlib
    import struct

    source, _manifest = _verified_inputs(tmp_path)
    info = inspect_pack(source)
    root = info.root_name
    with zipfile.ZipFile(source) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    manifest_name = f"{root}/_ovpack/manifest.json"
    records_name = f"{root}/_ovpack/index_records.jsonl"
    dense_name = f"{root}/_ovpack/dense.f32"
    rows = [json.loads(line) for line in members[records_name].splitlines()]
    dimension = info.dense["dimensions"]
    values = [struct.pack("<f", float(index + 1)) * dimension for index in range(len(rows))]

    def write_pack(path, reordered):
        exported = dict(members)
        ordered = list(reversed(list(zip(rows, values)))) if reordered else list(zip(rows, values))
        copied_rows = []
        for index, (row, _raw) in enumerate(ordered):
            copied = json.loads(json.dumps(row))
            copied["record_id"] = f"changed-{index}"
            copied["vector"]["dense"]["offset"] = index * dimension
            copied_rows.append(copied)
        exported[records_name] = ("\n".join(json.dumps(row) for row in copied_rows) + "\n").encode()
        exported[dense_name] = b"".join(raw for _row, raw in ordered)
        embedded = json.loads(exported[manifest_name])
        embedded["root"]["uri"] = "viking://resources/shared/different-location"
        embedded["index"]["records"]["sha256"] = hashlib.sha256(exported[records_name]).hexdigest()
        embedded["index"]["dense"]["sha256"] = hashlib.sha256(exported[dense_name]).hexdigest()
        exported[manifest_name] = json.dumps(embedded).encode()
        with zipfile.ZipFile(path, "w") as archive:
            for name, raw in exported.items():
                archive.writestr(name, raw)

    original, reordered = tmp_path / "original.ovpack", tmp_path / "reordered.ovpack"
    write_pack(original, False)
    write_pack(reordered, True)
    result = verify_imported_pack(reordered, original)
    assert result == {"verified_files": 2, "verified_vectors": 3}


def test_size_mismatch_rejected(tmp_path):
    pack_path, manifest = _verified_inputs(tmp_path)
    manifest.data["pack"]["size"] += 1
    with pytest.raises(CorruptPack, match="size"):
        verify_pack(pack_path, manifest, expected=ExpectedContract())


def test_sha_mismatch_rejected(tmp_path):
    pack_path, manifest = _verified_inputs(tmp_path)
    manifest.data["pack"]["sha256"] = "0" * 64
    with pytest.raises(CorruptPack, match="sha256"):
        verify_pack(pack_path, manifest, expected=ExpectedContract())


def test_non_zip_rejected(tmp_path):
    pack_path, manifest = _verified_inputs(tmp_path)
    broken = tmp_path / "broken.ovpack"
    broken.write_bytes(b"not a zip at all")
    manifest.data["pack"]["size"] = broken.stat().st_size
    import hashlib

    manifest.data["pack"]["sha256"] = hashlib.sha256(broken.read_bytes()).hexdigest()
    with pytest.raises(CorruptPack, match="zip"):
        verify_pack(broken, manifest, expected=ExpectedContract())


@pytest.mark.parametrize(
    "member",
    ["../escape.md", "/abs/escape.md", "C:/win/escape.md", "root/../escape.md", "root\\escape.md"],
)
def test_unsafe_members_rejected(tmp_path, member):
    pack_path, manifest = _verified_inputs(tmp_path, extra_members={member: b"x"})
    with pytest.raises(CorruptPack, match="unsafe"):
        verify_pack(pack_path, manifest, expected=ExpectedContract())


def test_multiple_roots_rejected(tmp_path):
    pack_path, manifest = _verified_inputs(tmp_path, extra_members={"other/x.md": b"x"})
    with pytest.raises(CorruptPack, match="one top-level"):
        verify_pack(pack_path, manifest, expected=ExpectedContract())


def test_missing_embedded_manifest_rejected(tmp_path):
    pack_path = tmp_path / "bare.ovpack"
    with zipfile.ZipFile(pack_path, "w") as archive:
        archive.writestr("root/a.md", b"# A\n\nbody\n")
    with pytest.raises(CorruptPack, match="_ovpack/manifest.json"):
        inspect_pack(pack_path)


@pytest.mark.parametrize(
    "field,value",
    [("dimensions", 768), ("dtype", "float16"), ("byte_order", "big")],
)
def test_dense_contract_mismatch_rejected(tmp_path, field, value):
    kwargs = {field: value}
    pack_path, manifest = _verified_inputs(tmp_path, **kwargs)
    with pytest.raises(IncompatiblePack):
        verify_pack(pack_path, manifest, expected=ExpectedContract())


def test_wrong_embedding_model_rejected(tmp_path):
    entries = make_corpus()
    pack_path = tmp_path / "corpus.ovpack"
    index = make_pack(pack_path, entries, model="other-model")
    # The release manifest claims the pinned model; the pack disagrees.
    manifest = validate_release_manifest(
        make_manifest(pack_path, entries, index), expected=ExpectedContract()
    )
    with pytest.raises(IncompatiblePack, match="other-model"):
        verify_pack(pack_path, manifest, expected=ExpectedContract())


def test_corrupt_dense_part_rejected(tmp_path):
    pack_path, manifest = _verified_inputs(tmp_path, corrupt_dense=True)
    with pytest.raises(CorruptPack, match="dense vectors"):
        verify_pack(pack_path, manifest, expected=ExpectedContract())


def test_missing_content_file_rejected(tmp_path):
    pack_path, manifest = _verified_inputs(tmp_path, omit_entry="notes/beta.md")
    with pytest.raises(CorruptPack, match="notes/beta.md"):
        verify_pack(pack_path, manifest, expected=ExpectedContract())


def test_content_tamper_rejected(tmp_path):
    entries = make_corpus()
    pack_path = tmp_path / "corpus.ovpack"
    index = make_pack(pack_path, entries)
    manifest = validate_release_manifest(
        make_manifest(pack_path, entries, index), expected=ExpectedContract()
    )
    manifest.data["content"]["files"][0]["sha256"] = "f" * 64
    with pytest.raises(CorruptPack, match="same Git content"):
        verify_pack(pack_path, manifest, expected=ExpectedContract())


def test_verify_model_files(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    model = cache / "model.onnx"
    model.write_bytes(b"onnx-bytes")
    import hashlib

    entries = make_corpus()
    pack_path = tmp_path / "corpus.ovpack"
    index = make_pack(pack_path, entries)
    manifest = validate_release_manifest(
        make_manifest(
            pack_path,
            entries,
            index,
            model_files=[{"path": "model.onnx", "sha256": hashlib.sha256(b"onnx-bytes").hexdigest(), "size": 10}],
        ),
        expected=ExpectedContract(),
    )
    assert verify_model_files(cache, manifest) == []
    model.write_bytes(b"different")
    problems = verify_model_files(cache, manifest)
    assert problems and "model.onnx" in problems[0]
    assert verify_model_files(tmp_path / "absent", manifest)


def test_verify_model_files_skipped_when_unpinned(tmp_path):
    entries = make_corpus()
    pack_path = tmp_path / "corpus.ovpack"
    index = make_pack(pack_path, entries)
    manifest = validate_release_manifest(
        make_manifest(pack_path, entries, index), expected=ExpectedContract()
    )
    assert verify_model_files(tmp_path / "absent", manifest) == []
