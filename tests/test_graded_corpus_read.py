"""Local corpus reads preserve graded retrieval across checkout line endings."""
from pathlib import Path

import pytest
from graded_fixture import OBSERVED, unit
from mindie_knowledge.graded.index import RoutingIndex
from mindie_knowledge.graded.maintenance import build_routing_index, lint_corpus


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_local_corpus_retrieves_child_and_inherited_rules(tmp_path, newline):
    parent = unit("unit-parent")
    child = unit("unit-child", parents=[parent.id])
    for value in (parent, child):
        path = tmp_path / value.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value.render().replace("\n", newline).encode("utf-8"))

    index = RoutingIndex.from_root(tmp_path)
    result = index.query("graph", conditions=OBSERVED)
    assert child.id in {row["id"] for row in result["results"]}
    assert index.closure[child.id] == [parent.id]
    assert index.units[child.id].body == child.body


def test_vllm_ascend_pd_disaggregation_graded_units_top1_recall_and_closure():
    corpus_root = Path(__file__).resolve().parents[2] / "knowledge-vllm-ascend"
    if not (corpus_root / "index" / "routing-manifest.json").is_file():
        pytest.skip("sibling knowledge-vllm-ascend checkout not present")

    check_report = build_routing_index(corpus_root, check=True)
    assert check_report["ok"] is True, check_report["errors"]
    lint_report = lint_corpus(corpus_root)
    assert lint_report["valid"] is True, lint_report["errors"]

    index = RoutingIndex.from_root(corpus_root)
    conditions = {"repository": "vllm-ascend", "npu_model": "Ascend910B"}

    queries_and_expected = [
        (
            "KVConnectorBase_V1 wait_for_layer_load start_load_kv MooncakeConnector block_stride N-1",
            "unit-vllm-ascend-pd-kv-connector-layout-and-handoff-invariants",
        ),
        (
            "CircularBufferSpec MambaSpec KVCacheTensor layer_stride DCP cp_kv_cache_interleave_size mooncake_hybrid_connector",
            "unit-vllm-ascend-pd-connector-layout-compatibility-audit-sop",
        ),
        (
            "load_balance_proxy_server_example kv_transfer_params WAITING_FOR_REMOTE_KVS kv_producer kv_consumer",
            "unit-vllm-ascend-pd-serving-launch-and-transfer-acceptance-sop",
        ),
        (
            "PD accuracy debug gibberish repeated_ngram punctuation_run sentinel fill 2x2 ablation matrix step-0 logits",
            "unit-vllm-ascend-pd-accuracy-topdown-bottomup-diagnosis-workflow",
        ),
    ]

    for query_text, expected_id in queries_and_expected:
        response = index.query(query_text, conditions=conditions, limit=5, token_budget=1600)
        assert response["results"], f"No results for {query_text!r}"
        assert response["results"][0]["id"] == expected_id
        assert response["cards"][0]["id"] == expected_id
        assert response["cards"][0]["ready_to_apply"] is True
        closure = index.explain_closure([expected_id], conditions=conditions, token_budget=4096)
        assert closure["unresolved"] == []
        assert any(item["id"] == expected_id and item["ready_to_apply"] is True for item in closure["units"])
