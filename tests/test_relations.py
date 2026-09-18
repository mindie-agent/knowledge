"""Existing prose produces navigable references, never an automatic truth decision."""
from __future__ import annotations

from pathlib import Path
import subprocess

from mindie_knowledge.code_map import build_code_map, compare_maps
from mindie_knowledge.markdown import load_document
from mindie_knowledge.relations import affected_documents, backlinks, build_relations


def note(root, name, text):
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(text, encoding="utf-8")
    return load_document(path, layer="candidate", root=root)


def test_explicit_inline_reference_and_backlinks(tmp_path):
    root = tmp_path / "notes"
    target = note(root, "target.md", "# Target\n\nExisting observation.\n")
    source = note(root, "source.md", '# Source\n\n[inline](target.md#observation) and [prior][p].\n\n[p]: target.md "title"\n\n```md\n[fake](missing.md)\n```\n')
    report = build_relations([source, target])
    links = backlinks(report, target.uri)
    assert len(links["relations"]) == 1  # Same source line/target is one association.
    assert links["relations"][0]["status"] == "resolved"
    assert links["relations"][0]["evidence"]["line_start"] == 3
    assert not any("missing" in edge["target"] for edge in report["edges"])


def test_provenance_links_are_kept_without_requiring_metadata_schema(tmp_path):
    doc = note(tmp_path, "case.md", "# Case\n\nKnown observation, cause unknown.\n")
    doc.source = {"url": "https://github.com/vllm-project/vllm-ascend/pull/123"}
    doc.evidence = {"measurements": [{"path": "run.json"}]}
    report = build_relations([doc])
    assert len(report["edges"]) == 2
    assert {e["status"] for e in report["edges"]} == {"external_unchecked", "outside_snapshot"}
    assert all(e["evidence"]["metadata_field"] for e in report["edges"])


def test_file_uri_from_native_path_resolves_with_encoded_spaces(tmp_path):
    target = note(tmp_path, "target note.md", "# Target\n\nExisting reference.\n")
    source = note(tmp_path, "source.md", f"# Source\n\n[prior]({target.path.as_uri()})\n")
    edges = build_relations([source, target])["edges"]
    assert edges[0]["target"] == target.uri
    assert edges[0]["status"] == "resolved"


def test_file_uri_preserves_remote_host_without_probing_it(tmp_path, monkeypatch):
    target = note(tmp_path, "target.md", "# Target\n\nExisting reference.\n")
    target.path = Path("//reference-host/notes/target.md")
    source = note(tmp_path, "source.md", "# Source\n\n[prior](file://reference-host/notes/target.md)\n")
    monkeypatch.setattr(Path, "resolve", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("UNC probe")))
    edge = build_relations([source, target])["edges"][0]
    assert edge["target"] == target.uri
    assert edge["status"] == "resolved"


def test_code_link_symbol_and_affected_only_changed_function(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    code = source / "op.py"
    code.write_text("def changed():\n    return 1\n\ndef stable():\n    return 9\n", encoding="utf-8")
    before = build_code_map(source, tmp_path / "state")
    docs = [note(tmp_path / "notes", "changed.md", "# Changed experience\n\n`changed` only under the recorded shape.\n"),
            note(tmp_path / "notes", "stable.md", "# Stable experience\n\n`stable` observed once.\n"),
            note(tmp_path / "notes", "file.md", "# Module\n\n[implementation](../repo/op.py#L1-L2)\n")]
    original = {doc.uri: doc.path.read_bytes() for doc in docs}
    report = build_relations(docs, code_maps=[before])
    candidates = [e for e in report["edges"] if e["kind"] == "symbol_candidate"]
    assert len(candidates) == 2 and all(e["status"] == "candidate" for e in candidates)
    link = next(e for e in report["edges"] if e["kind"] == "explicit_code_link")
    assert link["fragment_status"] == "verified"
    code.write_text(code.read_text().replace("return 1", "return 2"), encoding="utf-8")
    changes = compare_maps(before, build_code_map(source, tmp_path / "state"))
    affected = affected_documents(report, changes)
    assert {i["document"] for i in affected["documents"]} == {docs[0].uri, docs[2].uri}
    assert all(doc.path.read_bytes() == original[doc.uri] for doc in docs)


def test_removed_source_and_unavailable_root_have_distinct_impact(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    (source / "op.py").write_text("def gemma(): pass\n", encoding="utf-8")
    before = build_code_map(source, tmp_path / "state")
    doc = note(tmp_path / "notes", "case.md", "# Gemma\n\n`gemma` reference.\n")
    report = build_relations([doc], code_maps=[before])
    (source / "op.py").unlink()
    after = build_code_map(source, tmp_path / "state")
    changes = compare_maps(before, after)
    assert affected_documents(report, changes)["documents"][0]["reasons"][0]["change"] == "removed"
    source.rmdir()
    changes = compare_maps(before, build_code_map(source, tmp_path / "state"))
    assert affected_documents(report, changes)["documents"][0]["reasons"][0]["change"] == "unknown"


def test_same_name_in_two_roots_is_candidate_not_cross_repository_fact(tmp_path):
    maps = []
    for name in ("repo1", "repo2"):
        root = tmp_path / name
        root.mkdir()
        (root / "op.py").write_text("def gemma(): pass\n", encoding="utf-8")
        maps.append(build_code_map(root, tmp_path / "state"))
    doc = note(tmp_path / "notes", "case.md", "# Gemma\n\n`gemma` may be relevant.\n")
    report = build_relations([doc], code_maps=maps)
    assert len(report["edges"]) == 2
    assert len({e["source_id"] for e in report["edges"]}) == 2
    assert all(e["status"] == "candidate" for e in report["edges"])


def test_remote_pinned_code_anchor_and_wrong_version_stay_distinct(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()
    git("init", "-q")
    git("config", "user.name", "Example")
    git("config", "user.email", "example@example.test")
    git("remote", "add", "upstream", "https://github.com/vllm-project/vllm-ascend.git")
    (root / "op.py").write_text("def gemma(): pass\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "snapshot")
    sha = git("rev-parse", "HEAD")
    code_map = build_code_map(root, tmp_path / "state", revision=sha)
    url = "https://github.com/vllm-project/vllm-ascend/blob/"
    doc = note(tmp_path / "notes", "case.md", f"# Gemma\n\n[exact]({url}{sha}/op.py#L1)\n[bad span]({url}{sha}/op.py#L99)\n[branch]({url}main/op.py#L1)\n")
    edges = build_relations([doc], code_maps=[code_map])["edges"]
    assert [e["fragment_status"] for e in edges] == ["verified", "outside_snapshot", "unchecked"]
    assert edges[-1]["status"] == "external_unchecked"


def test_unknown_link_does_not_probe_or_read_target(tmp_path, monkeypatch):
    doc = note(tmp_path, "case.md", "# Case\n\n[unknown](//unavailable-host/share/missing.md)\n")
    def forbid(*args, **kwargs):
        raise AssertionError("relation builder probed an unprovided path")
    monkeypatch.setattr(Path, "resolve", forbid)
    monkeypatch.setattr(Path, "read_text", forbid)
    report = build_relations([doc])
    assert report["edges"][0]["status"] == "outside_snapshot"


def test_bounds_and_malformed_links_are_visible(tmp_path):
    doc = note(tmp_path, "case.md", "# Case\n\n[one](https://example.test/one)\n[two](https://example.test/two)\n[bad](http://[broken)\n")
    report = build_relations([doc], limit=1)
    assert len(report["edges"]) == 1 and report["truncated"]
    assert report["gaps"][0]["kind"] == "invalid_link"
