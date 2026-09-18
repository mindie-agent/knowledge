"""Reconciliation repairs real record loss without treating the ledger as truth."""

import hashlib
import json
from pathlib import Path

import pytest

from mindie_knowledge.local.backend import MemoryBackend
from mindie_knowledge.local.reconcile import reconcile_markdown
from mindie_knowledge.markdown import uri_for
from mindie_knowledge.server.layers import load_config


class TrackingBackend(MemoryBackend):
    def __init__(self, config=None):
        super().__init__(config)
        self.writes = []
        self.checks = []

    def upsert(self, uri, content, *, layer, wait=True):
        self.writes.append(uri)
        super().upsert(uri, content, layer=layer, wait=wait)

    def check_document(self, uri, content):
        self.checks.append(uri)
        return super().check_document(uri, content)


@pytest.fixture
def library(tmp_path):
    roots = {layer: tmp_path / layer for layer in ("shared", "project", "candidate")}
    for root in roots.values():
        root.mkdir()
    config = load_config({
        "backend": "memory", "state_root": str(tmp_path / "state"),
        "layers": {layer: {"roots": [str(root)]} for layer, root in roots.items()},
    }, env={})
    backend = TrackingBackend(config)
    config.retrieval = backend
    note = roots["project"] / "observation.md"
    note.write_text("# Graph observation\n\nA reproducible graphcanary with an unknown cause.\n", encoding="utf-8")
    return config, backend, roots, note, uri_for("project", note.name)


def test_incremental_add_edit_delete_and_rename_leave_unmanaged_records(library):
    config, backend, roots, note, uri = library
    assert reconcile_markdown(config, verify=True).upserted == 1
    backend.writes.clear()
    backend.checks.clear()
    assert reconcile_markdown(config).unchanged == 1
    assert backend.writes == backend.checks == []
    backend.upsert("viking://resources/project/unmanaged.md", "Do not touch", layer="project")
    note.write_text("# Revised observation\n\nThe condition is now recorded.\n", encoding="utf-8")
    added = roots["candidate"] / "new.md"
    added.write_text("# New case\n\nA separate candidate finding.\n", encoding="utf-8")
    changed = reconcile_markdown(config, verify=True)
    assert (changed.upserted, changed.deleted) == (2, 0)
    renamed = note.with_name("renamed.md")
    note.rename(renamed)
    added.unlink()
    moved = reconcile_markdown(config, verify=True)
    assert (moved.upserted, moved.deleted) == (1, 2)
    assert uri not in backend.documents
    assert backend.read(uri_for("project", renamed.name)) == renamed.read_text(encoding="utf-8")
    assert backend.read("viking://resources/project/unmanaged.md") == "Do not touch"


@pytest.mark.parametrize("damage", ["content_missing", "vector_missing", "content_changed"])
def test_integrity_audit_repairs_damage_despite_unchanged_ledger(library, damage):
    config, backend, roots, note, uri = library
    original = note.read_bytes()
    assert reconcile_markdown(config, verify=True).ok
    if damage == "content_missing":
        backend.documents.pop(uri)
    elif damage == "vector_missing":
        backend.vectors.remove(uri)
        assert backend.read(uri) is not None
        assert backend.search("graphcanary", layers=["project"]) == []
    else:
        backend.documents[uri]["content"] = "Unrelated stale indexed content"
    assert reconcile_markdown(config).upserted == 0
    repaired = reconcile_markdown(config, verify=True)
    assert repaired.ok and repaired.repaired == repaired.upserted == 1
    assert backend.check_document(uri, original.decode("utf-8"))
    assert note.read_bytes() == original
    assert len(backend.search("graphcanary", layers=["project"])) == 1


def test_model_contract_change_rebuilds_even_without_full_audit(library):
    config, backend, roots, note, uri = library
    assert reconcile_markdown(config).ok
    backend.fingerprint["model"] = "test-v2"
    rebuilt = reconcile_markdown(config)
    assert rebuilt.repaired == rebuilt.upserted == 1
    assert reconcile_markdown(config).unchanged == 1


def test_recreated_database_is_repopulated_from_original_files(library):
    config, backend, roots, note, uri = library
    assert reconcile_markdown(config, verify=True).ok
    replacement = TrackingBackend(config)
    config.retrieval = replacement
    repaired = reconcile_markdown(config, verify=True)
    assert repaired.ok and repaired.upserted == 1
    assert replacement.check_document(uri, note.read_text(encoding="utf-8"))


@pytest.mark.parametrize("bad_state", ["{broken", "[]", '{"documents": []}'])
def test_corrupt_ledger_recovers_current_notes_without_pruning_unknown_content(library, bad_state):
    config, backend, roots, note, uri = library
    assert reconcile_markdown(config).ok
    backend.documents.pop(uri)
    foreign = "viking://resources/shared/release-one/corpus/public.md"
    backend.upsert(foreign, "A separately managed release", layer="shared")
    ledger = config.state_root / "markdown-index.json"
    ledger.write_text(bad_state, encoding="utf-8")
    repaired = reconcile_markdown(config, verify=True)
    assert repaired.ok and repaired.upserted == 1
    assert backend.check_document(uri, note.read_text(encoding="utf-8"))
    assert backend.read(foreign) == "A separately managed release"
    assert uri in json.loads(ledger.read_text(encoding="utf-8"))["documents"]


def test_failed_vector_repair_is_not_admitted_as_success(library, monkeypatch):
    config, backend, roots, note, uri = library
    original = backend.upsert

    def broken(uri, content, *, layer, wait=True):
        original(uri, content, layer=layer, wait=wait)
        backend.vectors.discard(uri)

    monkeypatch.setattr(backend, "upsert", broken)
    report = reconcile_markdown(config, verify=True)
    assert report.degraded and report.upserted == 0
    assert "verification failed" in report.errors[0]
    ledger = json.loads((config.state_root / "markdown-index.json").read_text(encoding="utf-8"))
    assert uri not in ledger["documents"]
    assert note.is_file()
    monkeypatch.setattr(backend, "upsert", original)
    assert reconcile_markdown(config, verify=True).upserted == 1


def test_transient_scan_error_does_not_delete_previously_indexed_note(library, monkeypatch):
    config, backend, roots, note, uri = library
    assert reconcile_markdown(config).ok
    from mindie_knowledge.local import reconcile
    original = reconcile.load_document

    def unreadable(path, **kwargs):
        if path == note:
            raise OSError("temporarily unreadable")
        return original(path, **kwargs)

    monkeypatch.setattr(reconcile, "load_document", unreadable)
    report = reconcile_markdown(config, verify=True)
    assert report.degraded and report.deleted == 0
    assert backend.read(uri) == note.read_text(encoding="utf-8")


def test_legacy_shared_migration_is_limited_to_owned_local_records(library):
    config, backend, roots, note, uri = library
    shared = roots["shared"] / "public.md"
    shared.write_text("# Bundled note\n\nKeep this bootstrap reference.\n", encoding="utf-8")
    old_uri = "viking://resources/shared/public.md"
    active_uri = "viking://resources/shared/release-one/corpus/public.md"
    body = shared.read_text(encoding="utf-8")
    backend.upsert(old_uri, body, layer="shared")
    backend.upsert(active_uri, "Imported version", layer="shared")
    config.state_root.mkdir(parents=True)
    old_record = {"path": str(shared.resolve()), "layer": "shared", "sha256": hashlib.sha256(shared.read_bytes()).hexdigest()}
    (config.state_root / "markdown-index.json").write_text(json.dumps({
        "documents": {old_uri: old_record, active_uri: old_record},
    }), encoding="utf-8")
    report = reconcile_markdown(config, verify=True)
    assert report.ok and report.deleted == 1
    assert old_uri not in backend.documents
    assert backend.check_document(uri_for("shared", shared.name), body)
    assert backend.read(active_uri) == "Imported version"


def test_deleted_source_only_prunes_uris_derived_from_that_source(library):
    config, backend, roots, note, uri = library
    assert reconcile_markdown(config).ok
    foreign = "viking://resources/candidate/foreign.md"
    backend.upsert(foreign, "Another owner", layer="candidate")
    ledger_path = config.state_root / "markdown-index.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["documents"][foreign] = {**ledger["documents"][uri], "layer": "project"}
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    note.unlink()
    report = reconcile_markdown(config, verify=True)
    assert report.ok and report.deleted == 1
    assert backend.read(foreign) == "Another owner"
