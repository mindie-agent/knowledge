"""Behavioral retrieval cases: exact evidence, outage use and rank fusion."""
from __future__ import annotations

import hashlib
import json
import math
from unittest.mock import Mock

import pytest

from mindie_knowledge.local.backend import Hit, MemoryBackend, UnavailableBackend
from mindie_knowledge.markdown import load_document, meta_path
from mindie_knowledge.retrieval import fuse, source_excerpt
from mindie_knowledge.server.layers import load_config
from mindie_knowledge.server.query import explain, query


def setup(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    config = load_config({"state_root": str(tmp_path / "state"), "backend": "memory",
                          "layers": {"shared": {"enabled": False}, "candidate": {"enabled": False},
                                     "project": str(notes)}}, env={})
    config.retrieval = MemoryBackend()
    return config, notes


def test_long_note_returns_matching_lines_and_current_source_after_edit(tmp_path):
    config, notes = setup(tmp_path)
    path = notes / "case.md"
    raw = "# Graph observations\n\n" + "ordinary unrelated context\n" * 40 + "## Replay diagnosis\nACLGraph ERR0417 occurs only in this observed case.\nCause remains unknown.\n"
    path.write_text(raw, encoding="utf-8")
    hit = query(config, text="ERR0417").results[0]
    evidence = hit["evidence"]
    assert evidence["line_start"] > 35
    assert "Cause remains unknown" in hit["excerpt"]
    assert hit["excerpt"] == "\n".join(raw.splitlines()[evidence["line_start"] - 1:evidence["line_end"]])
    assert "text" not in evidence  # Do not repeat the source text in Agent context.
    assert evidence["content_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert explain(config, hit["ref"])["content"].endswith("Cause remains unknown.")
    path.write_text(raw.replace("unknown.", "still under investigation."), encoding="utf-8")
    assert query(config, text="ERR0417").results[0]["evidence"]["content_sha256"] != evidence["content_sha256"]


def test_vector_outage_and_search_exception_retain_local_matches_without_startup(tmp_path):
    config, notes = setup(tmp_path)
    (notes / "exact.md").write_text("# Operator note\n\naclnnFoo_42 fails in the recorded shape.\n", encoding="utf-8")
    for backend in (UnavailableBackend("offline"), MemoryBackend()):
        config.retrieval = backend
        backend.available = Mock(side_effect=AssertionError("query prepared backend"))
        backend.upsert = Mock(side_effect=AssertionError("query embedded"))
        if isinstance(backend, MemoryBackend):
            backend.search = Mock(side_effect=TimeoutError("read timed out"))
        result = query(config, text="aclnnFoo_42")
        assert result.degraded and result.unavailable
        assert result.results[0]["retrieval"] == ["lexical"]
        assert not config.state_root.exists()


def test_chinese_terms_exact_identifiers_and_related_semantic_hit(tmp_path):
    config, notes = setup(tmp_path)
    for name, text in {"exact": "# Evidence\n\nHCCL_E_PARA 参数错误，检查通信域。",
                       "semantic": "# Worker timeout\n\nCollective setup exceeded the deadline.",
                       "irrelevant": "# Browser\n\nWindow placement and font colors."}.items():
        (notes / f"{name}.md").write_text(text, encoding="utf-8")
    document = load_document(notes / "semantic.md", layer="project", root=notes)
    config.retrieval.search = Mock(return_value=[Hit(document.uri, .98, title=document.title)])
    result = query(config, text="HCCL_E_PARA 通信参数错误")
    assert {hit["title"] for hit in result.results} == {"Evidence", "Worker timeout"}
    assert any(hit["retrieval"] == ["lexical"] for hit in result.results)
    chinese = query(config, text="参数错误")
    assert any("参数错误" in hit["excerpt"] for hit in chinese.results)


def test_fusion_deduplicates_routes_and_ignores_vector_score_units():
    first = Hit("one", .01)
    second = Hit("two", 900)
    results = fuse([first, first, Hit("three", .001)], [second, first])
    assert results[0][0].uri == "one"
    assert results[0][1] == ["vector", "lexical"]
    assert len(results) == 3
    changed_vector_units = fuse([Hit("one", 1e9), Hit("one", -1), Hit("three", float("nan"))], [second, first])
    assert [(hit.uri, hit.score, methods) for hit, methods in changed_vector_units] == [
        (hit.uri, hit.score, methods) for hit, methods in results]


@pytest.mark.parametrize("position", [1, 2])
def test_weak_dual_route_matches_do_not_crowd_out_strong_lexical_match(position):
    rare = Hit("rare-diagnostic", 18.0)
    common = [Hit(f"platform-{index:02}", 0.000003) for index in range(32)]
    direct = [Hit("direct-original", 20.0)] if position == 2 else []
    vector = direct + [Hit(hit.uri, .99) for hit in common]
    lexical = direct + [rare] + common
    results = fuse(vector, lexical)
    # The old flat RRF put every dual-route common match ahead of this note.
    assert "rare-diagnostic" in [hit.uri for hit, _ in results[:8]]
    assert any(hit.uri == "rare-diagnostic" and methods == ["lexical"] for hit, methods in results)
    assert any(methods == ["vector", "lexical"] for _, methods in results[:8])


def test_real_agreement_survives_a_stronger_lexical_outlier_and_flat_scores():
    lexical = [Hit("rare", 20), Hit("related", 18), Hit("other", 1)]
    results = fuse([Hit("related", -2000), Hit("semantic-only", 50000)], lexical)
    assert results[0][0].uri == "related"
    assert any(hit.uri == "semantic-only" and methods == ["vector"] for hit, methods in results)
    flat = fuse([Hit("first", .1), Hit("both", .01)], [Hit("second", 2), Hit("both", 2)])
    assert flat[0][0].uri == "both"
    # Lexical normalization is within this query and invariant to unit scaling.
    scaled = fuse([Hit("related", -2000), Hit("semantic-only", 50000)],
                  [Hit(hit.uri, hit.score * 100) for hit in lexical])
    assert [(hit.uri, hit.score) for hit, _ in results] == [(hit.uri, hit.score) for hit, _ in scaled]


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf"), -float("inf")])
def test_invalid_lexical_scores_do_not_poison_fusion_or_single_route_ranks(bad):
    lexical = [Hit("first", bad), Hit("second", bad)]
    assert [hit.uri for hit, _ in fuse([], lexical)] == ["first", "second"]
    vector = [Hit("semantic", bad), Hit("first", bad)]
    results = fuse(vector, lexical)
    assert {hit.uri for hit, _ in results} == {"first", "second", "semantic"}
    assert all(math.isfinite(hit.score) and hit.score > 0 for hit, _ in results)


@pytest.mark.parametrize("catalog", [False, True])
def test_public_hybrid_query_keeps_exact_alias_and_semantic_reference_among_common_hits(tmp_path, catalog):
    from mindie_knowledge.catalog import refresh_catalog
    from mindie_knowledge.markdown import normalized_sha256

    config, notes = setup(tmp_path)
    raw = "# Diagnostic notebook\n\nObservation has not established its cause.\n"
    path = notes / "diagnostic.md"
    path.write_text(raw, encoding="utf-8")
    meta_path(path).write_text(json.dumps({"retrieval": {"source_sha256": normalized_sha256(raw),
        "aliases": ["tensor_fold_73 graph revision"]}}), encoding="utf-8")
    semantic = notes / "semantic.md"
    semantic.write_text("# Related execution evidence\n\nReplay diverged after a shape change; diagnosis remains open.\n", encoding="utf-8")
    vector = [Hit(load_document(semantic, layer="project", root=notes).uri, .99)]
    for index in range(40):
        common = notes / f"platform-{index:02}.md"
        common.write_text(f"# Platform {index}\n\ngraph revision constants notes.\n", encoding="utf-8")
        vector.append(Hit(load_document(common, layer="project", root=notes).uri, .98))
    config.retrieval.search = Mock(side_effect=lambda *args, limit, **kwargs: vector[:limit])
    config.retrieval.upsert = Mock(side_effect=AssertionError("query must not embed"))
    if catalog:
        assert refresh_catalog(config)["status"] == "ready"
    result = query(config, text="tensor_fold_73 graph revision", limit=8)
    refs = {hit["slug"] for hit in result.results}
    assert {"diagnostic", "semantic"} <= refs
    assert len(result.results) <= 8 and result.source_reads <= 8
    diagnostic = next(hit for hit in result.results if hit["slug"] == "diagnostic")
    assert diagnostic["evidence"]["content_sha256"] == normalized_sha256(raw)
    assert "cause" in diagnostic["excerpt"]
    assert result.to_dict()["score_kind"] == "rank_fusion_with_lexical_agreement"


def test_deleted_disabled_and_forged_identities_cannot_appear_via_fallback(tmp_path):
    config, notes = setup(tmp_path)
    path = notes / "forged.md"
    path.write_text("# Forged\n\nforgedneedle", encoding="utf-8")
    meta_path(path).write_text(json.dumps({"uri": "viking://resources/shared/v1/secret.md"}), encoding="utf-8")
    response = query(config, text="forgedneedle")
    assert response.results == [] and response.degraded
    meta_path(path).unlink()
    config.retrieval.upsert("viking://resources/project/forged.md", path.read_text(), layer="project")
    path.unlink()
    assert query(config, text="forgedneedle").results == []
    assert query(config, text="forgedneedle", layers=["shared"]).results == []


def test_exact_rare_token_ranks_above_long_generic_note(tmp_path):
    config, notes = setup(tmp_path)
    for name, raw in {"specific": "# Runtime note\n\nHCCL_E_PARA graph fails.",
                      "generic": "# Graph guide\n\n" + "graph capture replay " * 100,
                      "other": "# Graph introduction\n\ngraph background."}.items():
        (notes / f"{name}.md").write_text(raw, encoding="utf-8")
    result = query(config, text="HCCL_E_PARA graph", limit=1)
    assert result.results[0]["slug"] == "specific"


def test_long_single_line_keeps_match_and_reports_column():
    raw = "# Evidence\n" + "intro " * 220 + "unique_marker end\n"
    evidence = source_excerpt(raw, "unique_marker")
    assert "unique_marker" in evidence["text"]
    assert evidence["line_start"] == evidence["line_end"] == 2
    assert evidence["column_start"] > 1
    assert raw.splitlines()[1][evidence["column_start"] - 1:].startswith(evidence["text"])


def test_newline_hash_scope_matches_normalized_source_text():
    windows = source_excerpt("# Note\r\n\r\nunique_marker\r\n", "unique_marker")
    unix = source_excerpt("# Note\n\nunique_marker\n", "unique_marker")
    assert windows == unix
    assert unix["hash_scope"] == "utf8_text_with_normalized_newlines"


def test_partly_missing_mount_is_incomplete_even_if_backend_ready(tmp_path):
    from dataclasses import replace
    from mindie_knowledge.distribution.manifest import atomic_write_json

    config, notes = setup(tmp_path)
    (notes / "visible.md").write_text("# Seen\n\nunique_marker", encoding="utf-8")
    config.mounts["project"] = replace(config.mount("project"), roots=(notes, tmp_path / "missing"))
    atomic_write_json(config.state_root / "maintenance.json", {"ready": True})
    result = query(config, text="unique_marker")
    assert result.results and result.degraded
    assert any("could not be read" in note for note in result.notes)
