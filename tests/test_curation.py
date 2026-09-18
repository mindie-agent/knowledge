from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from mindie_knowledge import curation
from mindie_knowledge.catalog import refresh_catalog
from mindie_knowledge.markdown import load_document, meta_path
from mindie_knowledge.server.layers import load_config
from mindie_knowledge.server.query import query


def test_cli_missing_or_invalid_explicit_config_cannot_fall_back(tmp_path, capsys):
    path = tmp_path / "service.json"
    assert curation.main(["status", "--config", str(path), "--job", "a" * 32]) == 1
    assert "existing file" in json.loads(capsys.readouterr().out)["reason"]
    path.write_text('{"backend":"unsupported"}', encoding="utf-8")
    assert curation.main(["status", "--config", str(path), "--job", "a" * 32]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "incomplete"
    assert sorted(item.name for item in tmp_path.iterdir()) == ["service.json"]


@pytest.fixture
def handoff(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    source = notes / "graph.md"
    source.write_text("# ACL Graph observation\n\n[Original PR](https://github.com/vllm-project/vllm-ascend/pull/16157).\nRuntime still unknown.\n", encoding="utf-8")
    config = load_config({"state_root": str(tmp_path / "state"), "backend": "disabled",
                          "layers": {"shared": {"enabled": False}, "project": str(notes), "candidate": str(tmp_path / "curated")},
                          "shared_sync": {"enabled": False}}, env={})
    target = tmp_path / "curated" / "topics" / "graph"
    def prepare(**kwargs):
        return curation.prepare(config, brief="Maintain a source-linked graph topic", sources=[source], target=target, **kwargs)
    def result(job, text=None):
        path = Path(job["output"]) / "topic.md"
        path.write_text(text or "# ACL Graph topic\n\nSee [PR 16157](https://github.com/vllm-project/vllm-ascend/pull/16157). Runtime remains unknown.\n\n## Retrieval queries\n\n1. 捕获回放的边界条件\n2. graph capture conditions\n", encoding="utf-8")
        return path
    return config, source, target, prepare, result


def test_native_markdown_becomes_source_bound_aliases_and_can_be_undone(handoff):
    config, source, target, prepare, result = handoff
    job = prepare(kind="topic")
    result(job)
    applied = curation.apply(config, job["job"])
    assert applied["status"] == "applied"
    note = load_document(target / "topic.md", layer="candidate", root=target.parents[1])
    assert note.retrieval["aliases"] == ["捕获回放的边界条件", "graph capture conditions"]
    assert note.evidence[0]["sha256"] == curation._hash(source.read_bytes())
    refresh_catalog(config)
    assert query(config, text="捕获回放的边界条件").results[0]["ref"] == note.uri
    assert curation.apply(config, job["job"])["status"] == "unchanged"
    assert curation.undo(config, job["job"])["status"] == "undone"
    assert not (target / "topic.md").exists()


def test_changed_source_and_changed_target_preserve_current_work(handoff):
    config, source, target, prepare, result = handoff
    job = prepare()
    result(job)
    source.write_text("# Updated source\n\nNew evidence\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source changed"):
        curation.apply(config, job["job"])
    assert not target.exists()
    fresh = prepare()
    result(fresh)
    target.mkdir(parents=True)
    (target / "topic.md").write_text("# Human note\n\nKeep this work\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed since preparation"):
        curation.apply(config, fresh["job"])
    assert "Keep this work" in (target / "topic.md").read_text(encoding="utf-8")


def test_existing_revision_and_metadata_history_restore_exact_bytes(handoff):
    config, source, target, prepare, result = handoff
    target.mkdir(parents=True)
    path = target / "topic.md"
    path.write_bytes(b"# Old\r\n\r\nUncertain old condition\r\n")
    original = path.read_bytes()
    metadata = {"source": {"url": "https://example.org/original"}, "evidence": "old measured evidence", "conditions": {"version": "v1"}}
    meta_path(path).write_text(json.dumps(metadata), encoding="utf-8")
    original_meta = meta_path(path).read_bytes()
    job = prepare()
    result(job)
    curation.apply(config, job["job"])
    changed = json.loads(meta_path(path).read_bytes())
    assert changed["source"] == metadata["source"]
    assert changed["evidence"][0]["previous_evidence"] == metadata["evidence"]
    curation.undo(config, job["job"])
    assert path.read_bytes() == original and meta_path(path).read_bytes() == original_meta


def test_cancellation_expiry_and_byte_limit_do_not_apply_results(handoff):
    config, source, target, prepare, result = handoff
    job = prepare(seconds=30, max_bytes=1024)
    result(job)
    with patch("mindie_knowledge.curation.time.time", return_value=job["deadline"] + 1):
        assert curation.status(config, job["job"])["status"] == "expired"
        with pytest.raises(ValueError, match="time budget"):
            curation.apply(config, job["job"])
    cancelled = prepare()
    curation.status(config, cancelled["job"], cancel=True)
    with pytest.raises(ValueError, match="cancelled"):
        curation.apply(config, cancelled["job"])
    large = prepare(max_bytes=1024)
    result(large, "# Large\n\n" + "x" * 1024)
    with pytest.raises(ValueError, match="byte budget"):
        curation.apply(config, large["job"])
    assert not target.exists()


def test_interrupted_batch_rolls_back_only_its_own_writes(handoff):
    config, source, target, prepare, result = handoff
    job = prepare()
    result(job)
    write = curation._write
    def fail_metadata(path, raw):
        if path.parent == target and path.suffix == ".json":
            raise OSError("interrupted filesystem write")
        write(path, raw)
    with patch("mindie_knowledge.curation._write", side_effect=fail_metadata), pytest.raises(OSError):
        curation.apply(config, job["job"])
    assert not (target / "topic.md").exists()
    assert curation.status(config, job["job"])["status"] == "interrupted"


def test_undo_refuses_to_overwrite_later_agent_edits(handoff):
    config, source, target, prepare, result = handoff
    job = prepare()
    result(job)
    curation.apply(config, job["job"])
    (target / "topic.md").write_text("# Later\n\nNewer agent evidence\n", encoding="utf-8")
    with pytest.raises(ValueError, match="newer work"):
        curation.undo(config, job["job"])
    assert "Newer agent" in (target / "topic.md").read_text(encoding="utf-8")


def test_handoff_rejects_mount_root_and_source_change_during_commit(handoff):
    config, source, target, prepare, result = handoff
    with pytest.raises(ValueError, match="entire knowledge mount"):
        curation.prepare(config, brief="x", sources=[source], target=config.mount("candidate").roots[0])
    job = prepare()
    result(job)
    write = curation._write
    def concurrent_source_edit(path, raw):
        write(path, raw)
        if path.parent == target and path.name == "topic.md":
            source.write_text("# New source\n\nConcurrent evidence\n", encoding="utf-8")
    with patch("mindie_knowledge.curation._write", side_effect=concurrent_source_edit), pytest.raises(ValueError, match="changed during"):
        curation.apply(config, job["job"])
    assert not (target / "topic.md").exists()


def test_readonly_mount_and_corrupt_job_are_explicit_failures(handoff):
    from dataclasses import replace
    config, source, target, prepare, result = handoff
    job = prepare()
    root = curation._job_root(config, job["job"])
    (root / "job.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="missing or incompatible"):
        curation.status(config, job["job"])
    config.mounts["candidate"] = replace(config.mount("candidate"), read_only=True)
    with pytest.raises(ValueError, match="writable"):
        prepare()


def test_existing_topic_can_be_an_input_and_damaged_history_cannot_restore(handoff):
    config, source, target, prepare, result = handoff
    target.mkdir(parents=True)
    old = target / "topic.md"
    old.write_text("# Existing topic\n\nPrevious uncertainty\n", encoding="utf-8")
    job = curation.prepare(config, brief="Update the existing topic", sources=[old, source], target=target)
    result(job)
    assert curation.apply(config, job["job"])["status"] == "applied"
    current = old.read_bytes()
    (curation._job_root(config, job["job"]) / "baseline" / "topic.md").write_bytes(b"damaged")
    with pytest.raises(ValueError, match="history is damaged"):
        curation.undo(config, job["job"])
    assert old.read_bytes() == current
