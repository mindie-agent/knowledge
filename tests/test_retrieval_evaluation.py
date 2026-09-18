import json
from pathlib import Path

import pytest

from mindie_knowledge.catalog import refresh_catalog
from mindie_knowledge.evaluation import evaluate
from mindie_knowledge.local.backend import MemoryBackend
from mindie_knowledge.markdown import meta_path, normalized_sha256
from mindie_knowledge.server.layers import load_config


def test_domain_regression_fixture_uses_real_query_path(tmp_path):
    fixture = json.loads((Path(__file__).parent / "fixtures" / "retrieval-evaluation.json").read_text(encoding="utf-8"))
    notes = tmp_path / "notes"
    notes.mkdir()
    for document in fixture["documents"]:
        path = notes / document["path"]
        path.write_text(document["raw"], encoding="utf-8")
        meta_path(path).write_text(json.dumps({"retrieval": {"source_sha256": normalized_sha256(document["raw"]),
                                                           "aliases": document.get("aliases", []), "topics": document.get("topics", [])}}), encoding="utf-8")
    config = load_config({"backend": "memory", "state_root": str(tmp_path / "state"),
                          "layers": {"shared": {"enabled": False}, "candidate": {"enabled": False}, "project": str(notes)}}, env={})
    # Exercise both candidate routes; an empty memory backend only tests lexical retrieval.
    # This deterministic in-process route is not native embedding quality evidence.
    config.retrieval = MemoryBackend(config)
    for document in fixture["documents"]:
        config.retrieval.upsert("viking://resources/project/" + document["path"], document["raw"], layer="project")
    assert refresh_catalog(config)["status"] == "ready"
    result = evaluate(config, fixture["cases"])
    assert result["summary"]["scored_queries"] == 12
    assert result["summary"]["recall"] == 1
    assert result["summary"]["mrr"] >= .9
    assert result["summary"]["evidence_accuracy"] == 1
    assert result["summary"]["no_evidence_abstention"] == 1
    assert "version_context" in result["categories"]
    assert all(all(case.get("context_coverage", {}).values()) for case in result["cases"])


def test_unknown_qrels_are_not_false_negatives_and_bad_evidence_is_detected():
    cases = [{"id": "unknown", "query": "x"}, {"id": "known", "query": "x", "relevant": {"a": 3, "b": 2}}]
    def search(*_a, **_k):
        return {"results": [{"ref": "b", "excerpt": "fabricated", "evidence": {"content_sha256": "wrong"}}]}
    report = evaluate(None, cases, search_fn=search, source_fn=lambda _: "# Source\n\nActual evidence.")
    assert report["summary"]["scored_queries"] == 1 and report["summary"]["unlabelled_queries"] == 1
    assert report["summary"]["recall"] == .5
    assert report["summary"]["evidence_accuracy"] == 0
    assert 0 < report["summary"]["ndcg"] < 1


def test_invalid_labels_fail_before_claiming_evaluation_success():
    with pytest.raises(ValueError):
        evaluate(None, [{"id": "one", "query": "x", "relevant": {"a": float("nan")}}], search_fn=lambda *_a, **_k: {})


def test_duplicate_results_do_not_inflate_relevance():
    result = evaluate(None, [{"id": "duplicate", "query": "x", "relevant": {"a": 3}}],
                      search_fn=lambda *_a, **_k: {"results": [{"ref": "a"}, {"ref": "a"}]},
                      source_fn=lambda _: None)
    assert result["summary"]["ndcg"] == result["summary"]["recall"] == 1
