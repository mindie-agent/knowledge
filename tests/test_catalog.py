"""Snapshot reuse, source-bound retrieval and real source validation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mindie_knowledge.catalog import catalog_path, export_catalog, refresh_catalog, repair_catalog, search_catalog
from mindie_knowledge.local.backend import MemoryBackend
from mindie_knowledge.markdown import load_document, meta_path, normalized_sha256, save_document
from mindie_knowledge.server.layers import load_config
from mindie_knowledge.server.query import explain, query


@pytest.fixture
def library(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    config = load_config({"backend": "memory", "state_root": str(tmp_path / "state"),
                          "layers": {"shared": {"enabled": False}, "project": str(notes),
                                     "candidate": {"enabled": False}}}, env={})
    config.retrieval = MemoryBackend()
    return config, notes


def note(notes, name, raw, *, aliases=None, topics=None):
    path = notes / name
    path.write_text(raw, encoding="utf-8")
    if aliases or topics:
        meta_path(path).write_text(json.dumps({"retrieval": {"source_sha256": normalized_sha256(raw),
                                                           "aliases": aliases or [], "topics": topics or []}}), encoding="utf-8")
    return path


def test_unchanged_refresh_reads_no_bodies_and_query_does_not_scan(library, monkeypatch):
    config, notes = library
    for i in range(40):
        note(notes, f"note-{i}.md", f"# Graph case {i}\n\nACLGraph codeonly_{i} under investigation.")
    first = refresh_catalog(config)
    assert first["status"] == "ready" and first["parsed"] == 40
    second = refresh_catalog(config)
    assert second["parsed"] == second["read_bytes"] == 0 and second["unchanged"] == 40
    assert first["snapshot"] == second["snapshot"]
    from mindie_knowledge.server import query as module
    monkeypatch.setattr(module, "load_layer_documents", lambda *_a, **_k: pytest.fail("query scanned sources"))
    read = Path.open
    reads = []
    def tracked(path, *args, **kwargs):
        if path.suffix == ".md":
            reads.append(path)
        return read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", tracked)
    result = query(config, text="codeonly_31", limit=1)
    assert len(result.results) == len(reads) == result.source_reads == 1
    assert result.results[0]["path"] == str(notes / "note-31.md")
    assert not result.incomplete


def test_one_changed_note_only_reparses_one_and_removes_stale_alias(library):
    config, notes = library
    path = note(notes, "graph.md", "# ACLGraph\n\nReplay differs in this layout.", aliases=["图模式精度对不上"], topics=["graph"])
    note(notes, "hccl.md", "# HCCL\n\nCollective rank map mismatch.")
    refresh_catalog(config)
    assert query(config, text="图模式精度对不上").results
    path.write_text("# ACLGraph\n\nA new observation supersedes the old layout.", encoding="utf-8")
    # Even before maintenance, stale generated aliases cannot return a hit.
    assert query(config, text="图模式精度对不上").results == []
    changed = refresh_catalog(config)
    assert changed["parsed"] == 1 and changed["unchanged"] == 1
    assert "source_changed" in changed["errors"][0]
    assert search_catalog(config, "图模式精度对不上").hits == []


def test_new_vector_reference_reads_exact_original_before_catalog_refresh(library, monkeypatch):
    config, notes = library
    note(notes, "old.md", "# Existing\n\nOld catalog content")
    refresh_catalog(config)
    path = note(notes, "fresh.md", "# New graph observation\n\nnewvectorquartz")
    uri = "viking://resources/project/fresh.md"
    config.retrieval.upsert(uri, path.read_text(encoding="utf-8"), layer="project")
    from mindie_knowledge.server import query as module
    monkeypatch.setattr(module, "load_layer_documents", lambda *_a, **_k: pytest.fail("new hit scanned collection"))
    result = query(config, text="newvectorquartz", limit=1)
    assert result.results[0]["ref"] == uri and result.source_reads == 1
    assert result.results[0]["retrieval"] == ["vector"]
    assert result.incomplete  # the new original is not yet in lexical lookup
    path.unlink()
    assert query(config, text="newvectorquartz", limit=1).results == []


def test_deleted_source_is_not_returned_before_refresh(library):
    config, notes = library
    path = note(notes, "a.md", "# Triton\n\nUniqueUBspill reproducer.")
    refresh_catalog(config)
    path.unlink()
    result = query(config, text="UniqueUBspill")
    assert result.results == [] and result.incomplete
    assert refresh_catalog(config)["deleted"] == 1


def test_remounted_relative_path_rebinds_without_deleting_original(library):
    config, notes = library
    original = note(notes, "graph.md", "# Graph\n\nold_binding_case")
    refresh_catalog(config)
    replacement = notes.parent / "replacement"
    replacement.mkdir()
    note(replacement, "graph.md", "# Graph\n\nnew_binding_case")
    from dataclasses import replace
    config.mounts["project"] = replace(config.mount("project"), roots=(replacement,))
    report = refresh_catalog(config)
    assert report["status"] == "ready" and report["parsed"] == 1
    assert original.is_file()
    assert query(config, text="old_binding_case").results == []
    assert query(config, text="new_binding_case").results


def test_rebuilt_catalog_has_a_distinct_snapshot_generation(library):
    config, notes = library
    note(notes, "graph.md", "# Graph\n\nACLGraph observed failure.")
    original = refresh_catalog(config)
    assert refresh_catalog(config)["previous_snapshot"] == original["snapshot"]
    catalog_path(config).unlink()
    rebuilt = refresh_catalog(config)
    assert rebuilt["previous_snapshot"] is None
    assert rebuilt["snapshot"] != original["snapshot"]
    assert rebuilt["changed_uris"] == original["changed_uris"]


def test_selection_prefers_without_hiding_cross_topic_evidence(library):
    config, notes = library
    note(notes, "graph.md", "# Observed graph\n\nFailure observed under this condition.", topics=["ACLGraph"])
    note(notes, "hccl.md", "# Observed communication\n\nFailure observed under this condition.", topics=["HCCL"])
    refresh_catalog(config)
    config.selection = {"topics": ["HCCL"]}
    result = query(config, text="Failure observed")
    assert result.results[0]["topics"] == ["HCCL"]
    assert len(result.results) == 2
    assert query(config, text="topic:ACLGraph Failure observed").results[0]["topics"] == ["ACLGraph"]
    assert len(query(config, text="all-topics Failure observed").results) == 2
    selected = search_catalog(config, "Failure observed", selection={"topics": ["HCCL"], "mode": "only"})
    assert len(selected.hits) == 1
    assert len(query(config, text="Failure observed", selection={"topics": ["HCCL"], "mode": "only"}).results) == 1


def test_source_bound_enrichment_preserves_original_conditions_and_old_metadata(library):
    config, notes = library
    document = save_document(notes, layer="project", title="Operator", content="aclnnFoo fails.", conditions={"soc": "910B4"})
    enriched = save_document(notes, layer="project", title=document.title, content=document.content,
                             retrieval={"source_sha256": normalized_sha256(document.raw_text), "aliases": ["算子异常"], "topics": ["operators"]})
    assert enriched.conditions == {"soc": "910B4"} and enriched.retrieval["aliases"] == ["算子异常"]
    edited = save_document(notes, layer="project", title=document.title, content="aclnnBar now fails.")
    assert edited.conditions == {"soc": "910B4"} and edited.retrieval["ignored"] == "source_changed"
    assert "aliases" not in edited.retrieval


def test_scoped_aliases_agree_in_catalog_and_cold_fallback(library):
    config, notes = library
    note(notes, "operator.md", "# Operator\n\nAn observed shape issue.", topics=["Triton"], aliases=[
        {"text": "allowedquartz", "scope": ["triton"]},
        {"text": "wrongquartz", "scope": "HCCL"},
        {"text": "malformedquartz", "scope": [{"private": "invalid"}]}])
    for warm in (False, True):
        if warm:
            assert refresh_catalog(config)["status"] == "ready"
        assert query(config, text="allowedquartz").results
        assert query(config, text="wrongquartz").results == []
        assert query(config, text="malformedquartz").results == []


def test_export_contains_context_and_aliases_without_machine_paths(library):
    config, notes = library
    path = note(notes, "graph.md", "# ACLGraph\n\n## Conditions\n- soc: Ascend910B4\n\nReplay differed.", aliases=["重放错误"], topics=["graph"])
    refresh_catalog(config)
    rows = list(export_catalog(config))
    assert len(rows) == 1 and rows[0]["retrieval"]["aliases"] == ["重放错误"]
    assert rows[0]["contexts"]["conditions"][0]["value"] == "Ascend910B4"
    assert str(path) not in json.dumps(rows)
    assert rows[0]["source_sha256"] == normalized_sha256(path.read_text(encoding="utf-8"))


def test_cold_fallback_is_bounded_and_does_not_create_catalog(library):
    config, notes = library
    for number in range(12):
        note(notes, f"{number}.md", f"# Device {number}\n\nHCCL failure {number}.")
    config.catalog_options = {"fallback_documents": 3}
    result = query(config, text="HCCL failure")
    assert result.incomplete and len(result.results) <= 3
    assert not config.state_root.exists()
    assert any("budget" in message for message in result.notes)


def _observed_scandir(original, on_entry):
    class Observed:
        def __init__(self, path):
            self.entries = original(path)

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self.entries)
            on_entry(entry)
            return entry

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.entries.close()

    return Observed


def test_cold_fallback_bounds_non_markdown_directory_entries(library, monkeypatch):
    from mindie_knowledge.server import query as module

    config, notes = library
    for number in range(256):
        (notes / f"asset-{number}.txt").write_text("asset", encoding="utf-8")
    original = module.os.scandir
    visited = []

    monkeypatch.setattr(module.os, "scandir", _observed_scandir(original, lambda entry: visited.append(entry.name)))
    config.catalog_options = {"fallback_documents": 1, "fallback_ms": 500}
    result = query(config, text="ACLGraph")
    assert len(visited) <= 128
    assert result.results == [] and result.incomplete
    assert any("directory-entry" in message for message in result.notes)
    assert not config.state_root.exists()


def test_cold_fallback_checks_deadline_while_visiting_empty_directories(library, monkeypatch):
    from mindie_knowledge.server import query as module

    config, notes = library
    for number in range(16):
        (notes / f"empty-{number}").mkdir()
    original = module.os.scandir
    clock, visited = [0.0], []

    def observed(entry):
        visited.append(entry.name)
        clock[0] += 0.002

    monkeypatch.setattr(module.os, "scandir", _observed_scandir(original, observed))
    monkeypatch.setattr(module.time, "perf_counter", lambda: clock[0])
    errors = []
    result = module.load_layer_documents(config, ["project"], errors=errors,
                                         max_documents=128, max_bytes=1048576, time_budget_ms=1)
    assert visited and len(visited) <= 1
    assert result == [] and any("time budget" in message for message in errors)
    assert not config.state_root.exists()


def test_cold_fallback_entry_budget_is_shared_across_mount_roots(library, monkeypatch):
    from dataclasses import replace
    from mindie_knowledge.server import query as module

    config, notes = library
    other = notes.parent / "other"
    other.mkdir()
    for root in (notes, other):
        for number in range(80):
            (root / f"asset-{number}.txt").write_text("asset", encoding="utf-8")
    config.mounts["project"] = replace(config.mount("project"), roots=(notes, other))
    visited, errors = [], []
    monkeypatch.setattr(module.os, "scandir", _observed_scandir(module.os.scandir, lambda entry: visited.append(Path(entry.path))))
    result = module.load_layer_documents(config, ["project"], errors=errors, max_documents=1)
    assert len(visited) == 128 and {path.parent for path in visited} == {notes, other}
    assert result == [] and any("directory-entry" in message for message in errors)


def test_growing_oversize_hit_is_unknown_without_unbounded_read(library):
    config, notes = library
    path = note(notes, "large.md", "# Graph\n\nunique_growth_case")
    refresh_catalog(config)
    from mindie_knowledge.markdown import MAX_REFERENCE_BYTES
    path.write_text("# Graph\n\nunique_growth_case " + "x" * MAX_REFERENCE_BYTES, encoding="utf-8")
    found = query(config, text="unique_growth_case")
    assert found.results == [] and found.incomplete
    refreshed = refresh_catalog(config)
    assert refreshed["status"] == "partial" and refreshed["parsed"] == 0
    assert "read budget" in refreshed["errors"][0]


def test_config_keeps_backend_holder_separate_from_catalog_options(tmp_path):
    config = load_config({"catalog": {"fallback_documents": 7}, "selection": {"topics": ["triton"]},
                          "sources": [{"kind": "git"}], "curation": {"provider": "native-agent"}}, env={})
    assert config.retrieval is None and config.catalog_options["fallback_documents"] == 7
    assert config.selection["topics"] == ["triton"]
    assert config.sources == [{"kind": "git"}] and config.curation["provider"] == "native-agent"


def test_older_catalog_title_index_is_added_only_by_maintenance(library):
    import sqlite3
    from mindie_knowledge.catalog import catalog_title_matches

    config, notes = library
    note(notes, "legacy.md", "# Legacy title\n\nACLGraph observation.")
    refresh_catalog(config)
    assert catalog_title_matches(config, "Legacy title", layer="project", root=notes)["matches"]
    with sqlite3.connect(catalog_path(config)) as db:
        db.execute("DROP INDEX documents_title")
    observed = catalog_title_matches(config, "Legacy title", layer="project", root=notes)
    assert not observed["available"] and observed["incomplete"]
    with sqlite3.connect(catalog_path(config)) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='documents_title'").fetchone() is None
    assert query(config, text="ACLGraph").results  # retrieval does not need that title index
    assert refresh_catalog(config)["parsed"] == 0
    assert catalog_title_matches(config, "Legacy title", layer="project", root=notes)["matches"]


def test_catalog_title_lookup_retains_observed_source_gaps(library):
    from mindie_knowledge.catalog import catalog_title_matches

    config, notes = library
    path = note(notes, "bad.md", "# Unreadable metadata\n\nEarlier evidence.")
    meta_path(path).write_text("[]", encoding="utf-8")
    assert refresh_catalog(config)["status"] == "partial"
    observed = catalog_title_matches(config, "A new title", layer="project", root=notes)
    assert observed["available"] and observed["incomplete"] and observed["matches"] == []


def test_catalog_explain_uses_current_original(library, monkeypatch):
    config, notes = library
    path = note(notes, "pd.md", "# PD\n\nNIXL registration timeout under investigation.")
    refresh_catalog(config)
    ref = query(config, text="NIXL").results[0]["ref"]
    path.write_text("# PD\n\nThe investigation now includes HCCL.", encoding="utf-8")
    assert "now includes" in explain(config, ref)["content"]


def test_imported_shared_versions_switch_without_mixing_old_aliases(tmp_path, monkeypatch):
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    config = load_config({"backend": "memory", "state_root": str(tmp_path / "state"),
                          "layers": {"shared": str(bootstrap), "project": {"enabled": False},
                                     "candidate": {"enabled": False}}}, env={})
    documents = []
    pointers = []
    for version in ("old", "new"):
        prepared = tmp_path / version
        prepared.mkdir()
        path = note(prepared, "graph.md", f"# ACLGraph\n\n{version} source observed.", aliases=["sharedquartz"])
        document = load_document(path, layer="shared", root=prepared)
        document.uri = f"viking://resources/shared/{version}/graph.md"
        documents.append(document)
        pointers.append({"root_uri": f"viking://resources/shared/{version}", "prepared_root": str(prepared),
                         "source_git_sha": version})
    from mindie_knowledge.server import query as module
    monkeypatch.setattr(module, "current_shared", lambda _: pointers[0])
    old = refresh_catalog(config, extra_documents=[documents[0]])
    assert query(config, text="sharedquartz").results[0]["ref"] == documents[0].uri
    # A pointer switches before the next maintenance tick: old lexical rows
    # cannot cross that boundary, even if the old source is still on disk.
    monkeypatch.setattr(module, "current_shared", lambda _: pointers[1])
    assert query(config, text="sharedquartz").results == []
    new = refresh_catalog(config, extra_documents=[documents[1]])
    assert new["previous_snapshot"] == old["snapshot"]
    assert new["deleted_uris"] == [documents[0].uri]
    result = query(config, text="sharedquartz")
    assert [hit["ref"] for hit in result.results] == [documents[1].uri]
    assert result.source_reads == 1
    assert "new source" in explain(config, documents[1].uri)["content"]
    assert not explain(config, documents[0].uri)["found"]


def test_failed_shared_iterator_preserves_previous_snapshot(library):
    config, notes = library
    path = note(notes, "source.md", "# Graph\n\nobserved")
    document = load_document(path, layer="shared", root=notes)
    document.uri = "viking://resources/shared/old/source.md"
    before = refresh_catalog(config, extra_documents=[document])
    from dataclasses import replace
    from mindie_knowledge.distribution.errors import CorruptPack
    def interrupted():
        yield replace(document, uri="viking://resources/shared/new/source.md")
        raise CorruptPack("prepared hash mismatch")
    failed = refresh_catalog(config, extra_documents=interrupted())
    assert failed["status"] == "pending"
    from mindie_knowledge.catalog import get_catalog_document
    assert get_catalog_document(config, document.uri) is not None
    assert get_catalog_document(config, "viking://resources/shared/new/source.md") is None
    assert refresh_catalog(config)["snapshot"] == before["snapshot"]


def test_explicit_repair_recovers_corrupt_catalog_without_removing_it(library):
    config, notes = library
    source = note(notes, "source.md", "# Graph\n\nrepair_quartz")
    config.state_root.mkdir()
    old = catalog_path(config)
    old.write_bytes(b"interrupted or corrupt sqlite")
    assert query(config, text="repair_quartz").incomplete
    assert old.read_bytes() == b"interrupted or corrupt sqlite"
    report = repair_catalog(config)
    assert report["status"] == "ready" and report["switched"]
    assert catalog_path(config) != old and old.read_bytes() == b"interrupted or corrupt sqlite"
    assert source.read_text() == "# Graph\n\nrepair_quartz"
    assert query(config, text="repair_quartz").results
    assert refresh_catalog(config)["snapshot"] == report["snapshot"]


def test_invalid_catalog_pointer_is_unknown_and_explicitly_repairable(library):
    config, notes = library
    note(notes, "source.md", "# Graph\n\nrepair_quartz")
    refresh_catalog(config)
    outside = config.state_root.parent / "outside.sqlite3"
    outside.write_bytes(b"unrelated artifact")
    pointer = config.state_root / "reference-catalog.json"
    pointer.write_text(json.dumps({"schema": 1, "file": "../outside.sqlite3"}), encoding="utf-8")
    assert catalog_path(config) is None
    assert query(config, text="repair_quartz").incomplete
    assert repair_catalog(config)["switched"]
    assert outside.read_bytes() == b"unrelated artifact"


def test_repair_leaves_an_existing_read_transaction_usable(library):
    import sqlite3
    config, notes = library
    source = note(notes, "source.md", "# Graph\n\nold_snapshot_quartz")
    old = refresh_catalog(config)
    path = catalog_path(config)
    connection = sqlite3.connect(path)
    try:
        connection.execute("BEGIN")
        assert "old_snapshot" in connection.execute("SELECT raw FROM documents").fetchone()[0]
        source.write_text("# Graph\n\nnew_snapshot_quartz", encoding="utf-8")
        repaired = repair_catalog(config)
        assert repaired["switched"] and repaired["snapshot"] != old["snapshot"]
        assert "old_snapshot" in connection.execute("SELECT raw FROM documents").fetchone()[0]
        assert query(config, text="new_snapshot_quartz").results
    finally:
        connection.close()


def test_failed_pointer_switch_and_repair_budget_preserve_active_generation(library, monkeypatch):
    config, notes = library
    note(notes, "source.md", "# Graph\n\nrepair_quartz")
    before = refresh_catalog(config)
    original = catalog_path(config)
    from mindie_knowledge.distribution import manifest
    write = manifest.atomic_write_json
    def locked_pointer(*_a, **_k):
        raise PermissionError("Windows reader temporarily owns the pointer")
    monkeypatch.setattr(manifest, "atomic_write_json", locked_pointer)
    busy = repair_catalog(config)
    assert busy["status"] == "busy" and not busy["switched"]
    assert catalog_path(config) == original
    monkeypatch.setattr(manifest, "atomic_write_json", write)
    note(notes, "second.md", "# Graph\n\nsecond source")
    limited = repair_catalog(config, limits={"max_documents": 1})
    assert limited["status"] == "pending" and not limited["switched"]
    assert search_catalog(config, "repair_quartz").snapshot == before["snapshot"]
    assert catalog_path(config) == original
