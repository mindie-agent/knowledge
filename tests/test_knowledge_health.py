"""Maintainer hints reuse observations and never turn them into task gates."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mindie_knowledge import health
from mindie_knowledge.distribution.sync import SwitchLock
from mindie_knowledge.markdown import meta_path
from mindie_knowledge.server.layers import load_config


NOW = datetime(2026, 9, 13, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def library(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    config = load_config({"backend": "unavailable", "state_root": str(tmp_path / "state"),
                          "layers": {"shared": {"enabled": False}, "project": str(notes),
                                     "candidate": {"enabled": False}}}, env={})
    return config, notes


def note(root, name, body, *, meta=None):
    path = root / name
    path.write_text(body, encoding="utf-8")
    os.utime(path, (NOW, NOW))
    if meta is not None:
        meta_path(path).write_text(json.dumps(meta), encoding="utf-8")
    return path


def kinds(report):
    return {finding["kind"] for finding in report["findings"]}


def test_exact_duplicates_keep_conditions_evidence_and_original_files(library, monkeypatch):
    config, notes = library
    first = note(notes, "a.md", "# Graph\n\nReplay differed in this test.\n", meta={"conditions": {"mode": "graph"}})
    second = note(notes, "b.md", first.read_text(), meta={"conditions": {"mode": "graph"}})
    third = note(notes, "c.md", first.read_text(), meta={"conditions": {"mode": "eager"}})
    fourth = note(notes, "d.md", first.read_text(), meta={"conditions": {"mode": "graph"}, "evidence": "Another case"})
    before = {path: path.read_bytes() for path in notes.iterdir()}
    from mindie_knowledge.local import backend
    monkeypatch.setattr(backend, "backend_for_config", lambda *_: pytest.fail("retrieval must not start"))
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: pytest.fail("network must not start"))
    result = health.inspect_knowledge(config, now=NOW)
    duplicates = [item for item in result["findings"] if item["kind"] == "exact_duplicate"]
    assert len(duplicates) == 1
    assert {item["path"] for item in duplicates[0]["documents"]} == {str(first), str(second)}
    assert third.exists() and fourth.exists()
    assert {path: path.read_bytes() for path in notes.iterdir()} == before
    assert result["status"] == "ok"


def test_same_snapshot_reuses_parsing_report_and_cache_bytes(library, monkeypatch):
    config, notes = library
    note(notes, "a.md", "# Observation\n\nStill only one observation.\n")
    initial = health.inspect_knowledge(config, now=NOW)
    cache = config.state_root / health.CACHE_NAME
    before = cache.read_bytes()
    monkeypatch.setattr(health, "_parse", lambda *_: pytest.fail("unchanged text was parsed again"))
    reused = health.inspect_knowledge(config, now=NOW + 1)
    assert reused["reused"] and reused["parsed"] == 0
    assert reused["snapshot"] == initial["snapshot"]
    assert cache.read_bytes() == before


def test_only_changed_note_is_reparsed_and_cache_is_recoverable(library):
    config, notes = library
    first = note(notes, "a.md", "# A\n\nOne observation.")
    note(notes, "b.md", "# B\n\nAnother observation.")
    assert health.inspect_knowledge(config, now=NOW)["parsed"] == 2
    first.write_text("# A\n\nUpdated observation.", encoding="utf-8")
    assert health.inspect_knowledge(config, now=NOW)["parsed"] == 1
    (config.state_root / health.CACHE_NAME).write_text("{broken", encoding="utf-8")
    assert health.inspect_knowledge(config, now=NOW)["parsed"] == 2


@pytest.mark.parametrize("corruption", [b"[]", b"\xff", b'{"schema": 1, "records": []}', "parsed", "baseline"])
def test_corrupt_cache_does_not_hide_authoritative_notes(library, corruption):
    config, notes = library
    note(notes, "a.md", "# A\n\nObservation.")
    health.inspect_knowledge(config, now=NOW)
    cache = config.state_root / health.CACHE_NAME
    if isinstance(corruption, bytes):
        cache.write_bytes(corruption)
    else:
        payload = json.loads(cache.read_text(encoding="utf-8"))
        record = next(iter(payload["records"].values()))
        record[corruption] = {} if corruption == "parsed" else []
        cache.write_text(json.dumps(payload), encoding="utf-8")
    recovered = health.inspect_knowledge(config, now=NOW)
    assert recovered["status"] == "ok" and recovered["documents"] == recovered["parsed"] == 1


@pytest.mark.parametrize(("key", "value"), [
    ("findings", None), ("findings", {}), ("findings", [None]),
    ("findings", [{"kind": "age_review", "status": None}]),
    ("findings", [{"kind": "age_review", "status": {}}]),
    ("documents", -1), ("parsed", False), ("status", "invented"),
    ("status", []),
    ("snapshot", "another snapshot"),
])
def test_corrupt_report_rebuilds_worklist_and_cli_stays_usable(library, monkeypatch, capsys, key, value):
    from mindie_knowledge.cli import main
    from mindie_knowledge.server import layers

    config, notes = library
    path = note(notes, "a.md", "# A\n\nObservation.")
    original = path.read_bytes()
    health.inspect_knowledge(config, now=NOW)
    cache = config.state_root / health.CACHE_NAME
    payload = json.loads(cache.read_text(encoding="utf-8"))
    payload["report"][key] = value
    cache.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(layers, "load_config", lambda **_: config)
    assert main(["health"]) == 0
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["status"] == "ok" and recovered["documents"] == 1
    assert isinstance(recovered["findings"], list)
    assert not recovered["reused"]
    assert path.read_bytes() == original


def test_busy_inspection_does_not_reuse_a_corrupt_report(library):
    config, notes = library
    note(notes, "a.md", "# A\n\nObservation.")
    health.inspect_knowledge(config, now=NOW)
    cache = config.state_root / health.CACHE_NAME
    payload = json.loads(cache.read_text(encoding="utf-8"))
    payload["report"]["findings"] = None
    cache.write_text(json.dumps(payload), encoding="utf-8")
    with SwitchLock(cache.with_suffix(".lock")):
        busy = health.inspect_knowledge(config, now=NOW)
    assert busy["status"] == "busy" and "findings" not in busy
    assert health.inspect_knowledge(config, now=NOW)["documents"] == 1


def test_age_boundary_refreshes_hint_without_inventing_expiry(library):
    config, notes = library
    recorded = NOW - 180 * 86400 + 1
    note(notes, "a.md", "# Recorded\n\nEvidence was limited.",
         meta={"captured_at": datetime.fromtimestamp(recorded, timezone.utc).isoformat()})
    assert "age_review" not in kinds(health.inspect_knowledge(config, now=NOW))
    later = health.inspect_knowledge(config, now=NOW + 2)
    assert not later["reused"] and later["parsed"] == 0
    hint = next(item for item in later["findings"] if item["kind"] == "age_review")
    assert hint["status"] == "review_hint" and hint["age_basis"] == "captured_at"
    assert "does not establish" in hint["reason"]


def test_plain_markdown_age_names_its_filesystem_basis(library):
    config, notes = library
    first = note(notes, "a.md", "# A\n\nObservation.")
    old = NOW - 181 * 86400
    os.utime(first, (old, old))
    result = health.inspect_knowledge(config, now=NOW)
    hint = next(item for item in result["findings"] if item["kind"] == "age_review")
    assert hint["age_basis"] == "file_modified_at" and hint["status"] == "review_hint"


def test_missing_note_mount_and_unreadable_metadata_are_unknown(library):
    config, notes = library
    first = note(notes, "a.md", "# A\n\nObservation.")
    health.inspect_knowledge(config, now=NOW)
    first.unlink()
    missing = health.inspect_knowledge(config, now=NOW)
    assert "note_unavailable" in kinds(missing)
    first = note(notes, "a.md", "# A\n\nObservation.")
    meta_path(first).write_text("{broken", encoding="utf-8")
    broken = health.inspect_knowledge(config, now=NOW)
    assert "note_unreadable" in kinds(broken)
    assert all(item["status"] == "unknown" for item in broken["findings"])
    moved = notes.with_name("temporarily_offline")
    notes.rename(moved)
    offline = health.inspect_knowledge(config, now=NOW)
    assert "source_unavailable" in kinds(offline) and offline["status"] == "partial"
    assert first.with_name("a.md").name in {path.name for path in moved.iterdir()}


def test_candidate_not_yet_created_is_empty_but_lost_observations_are_unknown(tmp_path):
    candidate = tmp_path / "candidate"
    config = load_config({"backend": "unavailable", "state_root": str(tmp_path / "state"),
                          "layers": {"shared": {"enabled": False}, "project": {"enabled": False},
                                     "candidate": str(candidate)}}, env={})
    initial = health.inspect_knowledge(config, now=NOW)
    assert initial["status"] == "ok" and initial["documents"] == 0 and initial["findings"] == []
    assert not candidate.exists()
    candidate.mkdir()
    first = note(candidate, "observation.md", "# Captured\n\nOne observation.")
    assert health.inspect_knowledge(config, now=NOW)["documents"] == 1
    first.unlink()
    candidate.rmdir()
    missing = health.inspect_knowledge(config, now=NOW)
    assert missing["status"] == "partial"
    assert "source_unavailable" in kinds(missing)
    assert all(item["status"] == "unknown" for item in missing["findings"])


def test_scan_permission_error_does_not_become_empty_success(library, monkeypatch):
    config, notes = library
    note(notes, "a.md", "# A\n\nObservation.")
    health.inspect_knowledge(config, now=NOW)
    def denied(_):
        raise PermissionError("subdirectory is temporarily unreadable")
    monkeypatch.setattr(health, "_markdown_paths", denied)
    failed = health.inspect_knowledge(config, now=NOW)
    assert failed["status"] == "partial"
    assert kinds(failed) == {"source_unavailable"}


def test_local_source_changes_persist_without_guessing_remote_or_missing_sources(library, tmp_path, monkeypatch):
    config, notes = library
    linked = note(notes, "source.md", "# Source\n\nOriginal observation.")
    note(notes, "consumer.md", "# Consumer\n\n[Local](source.md) [Remote](https://example.com/fact) [Outside](../private.txt)")
    private = tmp_path / "private.txt"
    private.write_text("private source must not be read", encoding="utf-8")
    read = health._file_digest
    def allowed(path):
        assert path != private
        return read(path)
    monkeypatch.setattr(health, "_file_digest", allowed)
    assert "linked_source_changed" not in kinds(health.inspect_knowledge(config, now=NOW))
    linked.write_text("# Source\n\nA changed observation.", encoding="utf-8")
    changed = health.inspect_knowledge(config, now=NOW)
    assert "linked_source_changed" in kinds(changed)
    assert "linked_source_changed" in kinds(health.inspect_knowledge(config, now=NOW + 1))
    assert "linked_source_changed" in kinds(health.inspect_knowledge(config, now=NOW + 2, refresh=True))
    linked.unlink()
    missing = health.inspect_knowledge(config, now=NOW)
    assert "linked_source_unavailable" in kinds(missing)
    assert next(item for item in missing["findings"] if item["kind"] == "linked_source_unavailable")["status"] == "unknown"


def test_shared_link_is_hashed_once_per_pass_and_changes_are_still_detected(library, monkeypatch):
    config, notes = library
    source = notes / "evidence.txt"
    source.write_text("original bytes", encoding="utf-8")
    for index in range(3):
        note(notes, f"consumer{index}.md", f"# Note {index}\n\n[Evidence](evidence.txt)")
    hashed = []
    original = health._file_digest

    def counted(path):
        hashed.append(path)
        return original(path)

    monkeypatch.setattr(health, "_file_digest", counted)
    assert "linked_source_changed" not in kinds(health.inspect_knowledge(config, now=NOW))
    assert hashed == [source]
    before = source.stat()
    source.write_text("modified bytes", encoding="utf-8")
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    changed = health.inspect_knowledge(config, now=NOW)
    assert hashed == [source, source]
    assert sum(item["kind"] == "linked_source_changed" for item in changed["findings"]) == 3


def test_unavailable_source_does_not_advance_observation_baseline(library, monkeypatch):
    config, notes = library
    linked = note(notes, "source.md", "# Source\n\nFirst bytes.")
    note(notes, "consumer.md", "# Consumer\n\n[Local](source.md#section)")
    health.inspect_knowledge(config, now=NOW)
    linked.write_text("# Source\n\nChanged bytes.", encoding="utf-8")
    digest = health._file_digest
    def offline(_):
        raise OSError("temporary read error")
    monkeypatch.setattr(health, "_file_digest", offline)
    failed = health.inspect_knowledge(config, now=NOW)
    assert "linked_source_unavailable" in kinds(failed)
    monkeypatch.setattr(health, "_file_digest", digest)
    assert "linked_source_changed" in kinds(health.inspect_knowledge(config, now=NOW))


def test_symlink_outside_mount_is_not_read(library, tmp_path, monkeypatch):
    config, notes = library
    outside = tmp_path / "private.md"
    outside.write_text("# Private\n\nDo not read.", encoding="utf-8")
    link = notes / "linked.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    original = Path.read_bytes
    def allowed(path):
        assert path != outside and path != link
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", allowed)
    result = health.inspect_knowledge(config, now=NOW)
    assert "note_unreadable" in kinds(result)


def test_concurrent_inspection_reuses_saved_report_without_mutation(library, monkeypatch):
    config, notes = library
    note(notes, "a.md", "# A\n\nObservation.")
    first = health.inspect_knowledge(config, now=NOW)
    lock = SwitchLock((config.state_root / health.CACHE_NAME).with_suffix(".lock"))
    lock.acquire()
    try:
        monkeypatch.setattr(health, "_markdown_paths", lambda *_: pytest.fail("a second inspector scanned"))
        busy = health.inspect_knowledge(config, now=NOW)
    finally:
        lock.release()
    assert busy["status"] == "busy" and busy["snapshot"] == first["snapshot"]


def test_cache_write_failure_preserves_inspection_and_sources(library, monkeypatch):
    config, notes = library
    first = note(notes, "a.md", "# A\n\nObservation.")
    def failed(*_):
        raise OSError("cache filesystem unavailable")
    monkeypatch.setattr(health, "atomic_write_json", failed)
    report = health.inspect_knowledge(config, now=NOW)
    assert report["documents"] == 1 and "cache_error" in report
    assert first.read_text(encoding="utf-8") == "# A\n\nObservation."


def test_unwritable_state_still_allows_read_only_inspection(library, monkeypatch):
    config, notes = library
    note(notes, "a.md", "# A\n\nObservation.")
    def denied(_):
        raise PermissionError("cache lock directory is read-only")
    monkeypatch.setattr(SwitchLock, "acquire", denied)
    monkeypatch.setattr(health, "atomic_write_json", lambda *_: pytest.fail("write attempted without a lock"))
    report = health.inspect_knowledge(config, now=NOW)
    assert report["documents"] == 1 and "cache_error" in report
