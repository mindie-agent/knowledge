"""Bundled shared notes remain available alongside exactly one active release."""

import json

import pytest

from mindie_knowledge.corpus import corpus_root
from mindie_knowledge.local.backend import MemoryBackend
from mindie_knowledge.local.reconcile import reconcile_markdown
from mindie_knowledge.local.shared import shared_search_uris
from mindie_knowledge.markdown import SHARED_BOOTSTRAP_URI, load_document, meta_path, uri_for
from mindie_knowledge.server.layers import load_config


@pytest.mark.parametrize("active", [
    "viking://resources/shared/v0123456789ab",
    "viking://resources/shared/repairs/0123456789abcdef/v0123456789ab",
])
def test_bootstrap_and_exact_active_root_are_both_searched(active, monkeypatch):
    monkeypatch.setattr("mindie_knowledge.local.shared.current_shared", lambda root: {"root_uri": active})
    assert shared_search_uris(None) == (SHARED_BOOTSTRAP_URI, active)


@pytest.mark.parametrize("active", [
    "viking://resources/shared", "viking://resources/shared/bootstrap",
    "viking://resources/shared/bootstrap/nested", "viking://resources/shared/repairs",
    "viking://resources/shared/repairs/0123456789abcdef", "viking://resources/project",
])
def test_broad_parent_pointers_never_expand_the_search_scope(active, monkeypatch):
    monkeypatch.setattr("mindie_knowledge.local.shared.current_shared", lambda root: {"root_uri": active})
    assert shared_search_uris(None) == (SHARED_BOOTSTRAP_URI,)


def test_legacy_shared_sidecar_is_normalized_without_changing_the_source(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("# Existing note\n\nRecorded context stays intact.\n", encoding="utf-8")
    sidecar = meta_path(note)
    sidecar.write_text(json.dumps({"uri": "viking://resources/shared/note.md", "source": {"kind": "recorded"}}), encoding="utf-8")
    before = note.read_bytes(), sidecar.read_bytes()
    document = load_document(note, layer="shared", root=tmp_path)
    assert document.uri == SHARED_BOOTSTRAP_URI + "/note.md"
    assert document.source == {"kind": "recorded"}
    assert (note.read_bytes(), sidecar.read_bytes()) == before


def test_bundled_corpus_and_project_notes_survive_an_active_release(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    for i in range(22):
        (project / f"case-{i}.md").write_text(f"# Project case {i}\n\nprojectcanary{i} recorded once.\n", encoding="utf-8")
    config = load_config({
        "backend": "memory", "state_root": str(tmp_path / "state"),
        "layers": {
            "shared": {"roots": [str(corpus_root())]},
            "project": {"roots": [str(project)]},
            "candidate": {"roots": [str(tmp_path / "candidate")]},
        },
    }, env={})
    backend = MemoryBackend(config)
    config.retrieval = backend
    active = "viking://resources/shared/v0123456789ab"
    stale = "viking://resources/shared/vffffffffffff"
    monkeypatch.setattr("mindie_knowledge.local.shared.current_shared", lambda root: {"root_uri": active})
    backend.upsert(active + "/corpus/public.md", "# Public\n\npubliccanary", layer="shared")
    backend.upsert(stale + "/corpus/old.md", "# Stale\n\npubliccanary", layer="shared")
    report = reconcile_markdown(config, verify=True)
    bundled = list(corpus_root().rglob("*.md"))
    assert len(bundled) >= 65
    assert report.ok and report.upserted == len(bundled) + 22
    for note in bundled:
        uri = uri_for("shared", note.relative_to(corpus_root()).as_posix())
        assert backend.check_document(uri, note.read_text(encoding="utf-8"))
    for i in range(22):
        hits = backend.search(f"projectcanary{i} ", layers=["project"])
        assert any(hit.uri == uri_for("project", f"case-{i}.md") for hit in hits)
    assert [hit.uri for hit in backend.search("publiccanary", layers=["shared"])] == [active + "/corpus/public.md"]
    assert stale + "/corpus/old.md" in backend.documents
