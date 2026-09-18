from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mindie_knowledge import curation_export as export
from mindie_knowledge.markdown import meta_path, normalized_sha256


@pytest.fixture
def feed(tmp_path):
    source, output = tmp_path / "source", tmp_path / "exports"
    (source / "topics").mkdir(parents=True)
    path = source / "topics" / "graph.md"
    path.write_text("# ACLGraph observations\n\nGraph replay differs; its cause remains unknown.\n\n## Retrieval queries\n- 图重放精度差异\n- graph replay conditions\n", encoding="utf-8")
    return source, output, path


def verified(result):
    assert result["status"] == "prepared", result
    root = Path(result["prepared_root"])
    manifest = export.verify_export(root, manifest_sha256=result["manifest_sha256"])
    return root, manifest


def test_prepared_copy_reuses_redaction_and_omits_private_sidecars(feed):
    source, output, path = feed
    text = path.read_text(encoding="utf-8") + "\nObserved on 192.168.31.218.\n"
    path.write_text(text, encoding="utf-8")
    original = path.read_bytes()
    meta_path(path).write_text(json.dumps({
        "source": {"path": "private source object must not leave"}, "evidence": "private evidence must not leave",
        "conditions": {"soc": "Ascend910B4", "host": "192.168.31.218"},
        "retrieval": {"source_sha256": normalized_sha256(text), "topics": ["ACLGraph"],
                      "aliases": [{"text": "graph alias on 192.168.31.218", "relation": "related", "scope": "ACLGraph"}]} }), encoding="utf-8")
    root, manifest = verified(export.export_notes(source, output))
    public = (root / "topics" / "graph.md").read_text(encoding="utf-8")
    metadata = json.loads(meta_path(root / "topics" / "graph.md").read_bytes())
    assert "192.168.31.218" not in public + json.dumps(metadata)
    assert set(metadata) == {"conditions", "retrieval"}
    assert "private source object" not in json.dumps(metadata) and "private evidence" not in json.dumps(metadata)
    assert "图重放精度差异" in metadata["retrieval"]["aliases"]
    assert metadata["retrieval"]["source_sha256"] == normalized_sha256(public)
    assert manifest["files"][0]["input_sha256"] == hashlib.sha256(original).hexdigest()
    assert path.read_bytes() == original


def test_repeat_reuses_verified_generation_without_rewriting_public_files(feed):
    source, output, _path = feed
    first = export.export_notes(source, output)
    root, _manifest = verified(first)
    public_files = [*root.rglob("*"), output / "current.json"]
    before = {str(path): (path.stat().st_mtime_ns, path.read_bytes()) for path in public_files if path.is_file()}
    second = export.export_notes(source, output)
    assert second["status"] == "unchanged" and second["prepared_root"] == first["prepared_root"]
    assert before == {name: (Path(name).stat().st_mtime_ns, Path(name).read_bytes()) for name in before}


@pytest.mark.parametrize("modified", ["body", "metadata", "manifest", "extra"])
def test_modified_prepared_output_fails_and_export_does_not_overwrite_it(feed, modified):
    source, output, _path = feed
    first = export.export_notes(source, output)
    root, _manifest = verified(first)
    pointer = (output / "current.json").read_bytes()
    path = {"body": root / "topics" / "graph.md", "metadata": meta_path(root / "topics" / "graph.md"),
            "manifest": root / "prepared.json", "extra": root / "unmanaged.txt"}[modified]
    path.write_bytes(b"external modification")
    with pytest.raises((export.ExportError, ValueError)):
        export.verify_export(root, manifest_sha256=first["manifest_sha256"])
    assert export.export_notes(source, output)["status"] == "incomplete"
    assert (output / "current.json").read_bytes() == pointer
    assert path.read_bytes() == b"external modification"


def test_delete_and_unique_content_rename_are_observed_without_removing_old_or_foreign_files(feed):
    source, output, path = feed
    previous, old = verified(export.export_notes(source, output))
    foreign = output / "notes-from-another-owner.md"
    foreign.write_text("unrelated output", encoding="utf-8")
    renamed = path.with_name("graph-replay.md")
    path.rename(renamed)
    root, moved = verified(export.export_notes(source, output))
    assert moved["previous_snapshot"] == old["snapshot"]
    assert moved["changes"]["renamed"] == [{"from": "topics/graph.md", "to": "topics/graph-replay.md", "basis": "identical_input_sha256"}]
    assert (previous / "topics" / "graph.md").exists()
    renamed.unlink()
    _empty, deleted = verified(export.export_notes(source, output))
    assert deleted["files"] == [] and deleted["changes"]["removed"] == ["topics/graph-replay.md"]
    assert (root / "topics" / "graph-replay.md").exists() and foreign.read_text() == "unrelated output"


def test_invalid_public_source_or_missing_root_keeps_previous_pointer(feed):
    source, output, path = feed
    first = export.export_notes(source, output)
    verified(first)
    pointer = (output / "current.json").read_bytes()
    # Existing r2 preparation can fail closed when a masked value still
    # matches a published rule. Export must propagate that block atomically.
    path.write_text("# Local observation\n\nC:\\Users\\maintainer\\graph has local results.\n", encoding="utf-8")
    blocked = export.export_notes(source, output)
    assert blocked["status"] == "incomplete" and "public preparation failed" in blocked["reason"]
    assert (output / "current.json").read_bytes() == pointer
    path.write_text("# No body\n", encoding="utf-8")
    assert export.export_notes(source, output)["status"] == "incomplete"
    assert (output / "current.json").read_bytes() == pointer
    moved = source.with_name("disconnected-source")
    source.rename(moved)
    assert export.export_notes(source, output)["status"] == "incomplete"
    assert (output / "current.json").read_bytes() == pointer


def test_stale_sidecar_alias_is_ignored_but_plain_markdown_queries_are_used(feed):
    source, output, path = feed
    meta_path(path).write_text(json.dumps({"retrieval": {"source_sha256": "a" * 64, "aliases": ["stale only"], "topics": ["stale"]}}), encoding="utf-8")
    root, _manifest = verified(export.export_notes(source, output))
    metadata = json.loads(meta_path(root / "topics" / "graph.md").read_bytes())
    assert "stale only" not in metadata["retrieval"]["aliases"]
    assert "图重放精度差异" in metadata["retrieval"]["aliases"] and metadata["retrieval"]["topics"] == []


def test_document_and_byte_limits_keep_previous_output(feed):
    source, output, path = feed
    verified(export.export_notes(source, output))
    pointer = (output / "current.json").read_bytes()
    path.with_name("another.md").write_text("# HCCL\n\nCollective mismatch under investigation.\n", encoding="utf-8")
    assert export.export_notes(source, output, max_files=1)["status"] == "incomplete"
    assert export.export_notes(source, output, max_file_bytes=32)["status"] == "incomplete"
    assert export.export_notes(source, output, max_bytes=200)["status"] == "incomplete"
    assert (output / "current.json").read_bytes() == pointer
    with pytest.raises(export.ExportError):
        export.export_notes(source, output, max_bytes=export.MAX_BYTES + 1)


def test_long_lived_feed_accepts_1024_notes_and_rejects_overflow_without_switching(tmp_path):
    source, output = tmp_path / "source", tmp_path / "exports"
    cases = source / "cases"
    cases.mkdir(parents=True)
    for number in range(1024):
        (cases / f"case-{number:04d}.md").write_text(
            f"# ACLGraph observation {number}\n\nStatic public source analysis; NPU execution remains unverified.\n",
            encoding="utf-8")
    first = export.export_notes(source, output)
    root, manifest = verified(first)
    assert first["files"] == 1024 and len(manifest["files"]) == 1024
    assert (root / "prepared.json").stat().st_size > 256 * 1024
    pointer = (output / "current.json").read_bytes()
    (cases / "one-too-many.md").write_text("# Extra case\n\nCapacity overflow must keep the active feed.\n", encoding="utf-8")
    failed = export.export_notes(source, output)
    assert failed["status"] == "incomplete" and "1024-document" in failed["reason"]
    assert (output / "current.json").read_bytes() == pointer


def test_concurrent_source_edit_and_failed_activation_preserve_current_export(feed, monkeypatch):
    source, output, path = feed
    verified(export.export_notes(source, output))
    pointer = (output / "current.json").read_bytes()
    original = export.prepare_reference
    calls = 0
    def changing(name, text, metadata):
        nonlocal calls
        calls += 1
        value = original(name, text, metadata)
        if calls == 2:  # after checking the old export, during new preparation
            path.write_text("# Updated while exporting\n\nNew observation.\n", encoding="utf-8")
        return value
    monkeypatch.setattr(export, "prepare_reference", changing)
    assert export.export_notes(source, output)["status"] == "incomplete"
    assert (output / "current.json").read_bytes() == pointer
    monkeypatch.setattr(export, "prepare_reference", original)
    def busy_pointer(*_a, **_k):
        raise PermissionError("pointer is in use")
    monkeypatch.setattr(export, "atomic_write_json", busy_pointer)
    assert export.export_notes(source, output)["status"] == "incomplete"
    assert (output / "current.json").read_bytes() == pointer


def test_selection_is_explicit_and_cannot_escape_or_overlap(feed):
    source, output, _path = feed
    (source / "private.md").write_text("# Not selected\n\nDo not export this note.\n", encoding="utf-8")
    root, manifest = verified(export.export_notes(source, output))
    assert [row["path"] for row in manifest["files"]] == ["topics/graph.md"]
    assert not (root / "private.md").exists()
    with pytest.raises(export.ExportError):
        export.export_notes(source, output, includes=("../outside",))
    with pytest.raises(export.ExportError):
        export.export_notes(source, source / "exports")


def test_cli_export_and_verification_emit_only_bounded_receipts(feed, capsys):
    source, output, _path = feed
    assert export.main(["--source-root", str(source), "--output-root", str(output)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "prepared" and receipt["changes"]["added"] == 1
    assert "manifest" not in receipt and isinstance(receipt["files"], int)
    assert export.main(["--verify-root", receipt["prepared_root"], "--manifest-sha256", receipt["manifest_sha256"]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "verified"
    (Path(receipt["prepared_root"]) / "topics" / "graph.md").write_text("# Edited\n\nChanged output.\n", encoding="utf-8")
    assert export.main(["--verify-root", receipt["prepared_root"], "--manifest-sha256", receipt["manifest_sha256"]]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "incomplete"
