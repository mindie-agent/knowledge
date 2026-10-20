"""Tier 0 deterministic tests for Ascend identifier subword indexing and RetrievalEvalCase harness."""
from __future__ import annotations

import pytest

from mindie_knowledge.graded import (
    RetrievalEvalCase,
     evaluate_retrieval_suite,
)
from mindie_knowledge.graded.index import RoutingIndex
from mindie_knowledge.graded.units import GradedUnit


def _make_unit(
    ident: str,
    *,
    tier: str = "L2",
    category: str = "sop",
    domain_path: str = "ops/ascendc",
    title: str = "",
    keywords: list[str] | None = None,
    conditions: dict | None = None,
    parents: list[str] | None = None,
    body: str = "",
) -> GradedUnit:
    strength = {
        "L1": "hardware_spec",
        "L2": "reproduced_empirical",
        "L3": "reproduced_empirical",
        "L4": "reproduced_empirical",
    }[tier]
    return GradedUnit.from_parts(
        {
            "schema": "mindie-graded-unit/1",
            "id": ident,
            "tier": tier,
            "category": category,
            "evidence_strength": strength,
            "domain_path": domain_path,
            "title": title or ident,
            "triggers": {
                "error_signatures": [],
                "keywords": list(keywords or [ident]),
            },
            "conditions": conditions or {"npu_model": ["Ascend910B4", "Ascend910B"]},
            "inherits_from": list(parents or ()),
            "provenance": {
                "source_type": "cannbot_okf",
                "source_locator": f"cannbot-knowledge@fe3268a:knowledge/{domain_path}/{ident}.md",
                "source_digest": "a" * 64,
                "evidence_refs": [f"okf:{ident}"],
                "supersedes": [],
            },
        },
        body or f"Specification for {ident}.",
        f"{domain_path}/{ident}.md",
    )


def _build_ascend_eval_corpus() -> list[GradedUnit]:
    l1_common = _make_unit(
        "unit-ascend-910b-ub-alignment-axiom",
        tier="L1",
        category="axiom",
        domain_path="_common",
        title="Ascend910B4 Unified Buffer 32B alignment and PipeBarrier axiom",
        keywords=["Ascend910B4", "PipeBarrier"],
        conditions={"npu_model": ["Ascend910B4", "Ascend910B"]},
        body=(
            "Unified Buffer transfers on Ascend910B4 require 32-byte alignment and "
            "explicit PipeBarrier synchronization between MTE2 and Vector pipes."
        ),
    )
    l2_datacopypad = _make_unit(
        "unit-ascendc-datacopypad-tail-sop",
        tier="L2",
        category="sop",
        domain_path="ops/ascendc",
        title="DataCopyPad unaligned GM-to-UB tail handling with SetFlag and WaitFlag",
        keywords=["DataCopyPad", "SetFlag", "WaitFlag", "MatmulImpl"],
        conditions={"npu_model": ["Ascend910B4", "Ascend910B"]},
        parents=[l1_common.id],
        body=(
            "Use DataCopyPad for unaligned tail elements and pair SetFlag / WaitFlag "
            "across MTE2 and VEC before MatmulImpl accumulation."
        ),
    )
    l2_gdn = _make_unit(
        "unit-vllm-ascend-gateddeltanet-aclgraph-sop",
        tier="L2",
        category="sop",
        domain_path="serving/vllm-ascend",
        title="GatedDeltaNet state capture under ACLGraph and aclrtSynchronizeStream",
        keywords=["GatedDeltaNet", "ACLGraph", "aclrtSynchronizeStream", "npu_fused_infer_attention_score"],
        conditions={"npu_model": ["Ascend910B4"], "graph_mode": "ACLGraph"},
        parents=[l1_common.id],
        body=(
            "Capture GatedDeltaNet recurrent conv_state under ACLGraph after "
            "verifying aclrtSynchronizeStream and npu_fused_infer_attention_score workspace."
        ),
    )
    l3_workflow = _make_unit(
        "unit-ascendc-tiling-routine-workflow",
        tier="L3",
        category="workflow",
        domain_path="ops/ascendc",
        title="Standard 5-stage DataCopyPad and PipeBarrier tiling workflow",
        keywords=["DataCopyPad", "PipeBarrier", "tiling"],
        conditions={"npu_model": ["Ascend910B4", "Ascend910B"]},
        parents=[l2_datacopypad.id],
        body=(
            "Follow the standard 5-stage DataCopyPad tiling decision tree before "
            "changing block split factors."
        ),
    )
    l4_mutator = _make_unit(
        "unit-ascendc-vectorized-pad-mutator",
        tier="L4",
        category="mutator",
        domain_path="ops/ascendc",
        title="Vectorized DataCopyPad zero-fill fastpath mutator",
        keywords=["DataCopyPad", "SetFlag", "WaitFlag"],
        conditions={"npu_model": ["Ascend910B4", "Ascend910B"]},
        parents=[l1_common.id],
        body=(
            "Replace scalar tail loop with DataCopyPad hardware padding and "
            "SetFlag/WaitFlag ping-pong double buffering. Fallback when stride < 32B."
        ),
    )
    l2_a3_only = _make_unit(
        "unit-a3-dual-die-roce-sop",
        tier="L2",
        category="sop",
        domain_path="serving/vllm-ascend",
        title="Ascend910_93 dual-die RoCE link check",
        keywords=["DataCopyPad", "GatedDeltaNet"],
        conditions={"npu_model": ["Ascend910_93"]},
        body="A3-only dual-die diagnostic procedure that must not match Ascend910B4.",
    )
    return [l1_common, l2_datacopypad, l2_gdn, l3_workflow, l4_mutator, l2_a3_only]


def test_routing_index_matches_camel_pascal_snake_and_preserves_exact_boost():
    corpus = _build_ascend_eval_corpus()
    index = RoutingIndex(corpus)
    obs = {"npu_model": "Ascend910B4", "graph_mode": "ACLGraph"}

    # Subword queries match camelCase/PascalCase/snake_case symbols indexed in units
    for subword_query, expected_id in (
        ("data copy pad tail", "unit-ascendc-datacopypad-tail-sop"),
        ("pipe barrier alignment", "unit-ascend-910b-ub-alignment-axiom"),
        ("set flag wait flag matmul impl", "unit-ascendc-datacopypad-tail-sop"),
        ("acl graph gated delta net", "unit-vllm-ascend-gateddeltanet-aclgraph-sop"),
        ("synchronize stream infer attention score", "unit-vllm-ascend-gateddeltanet-aclgraph-sop"),
        ("910b4 unified buffer", "unit-ascend-910b-ub-alignment-axiom"),
    ):
        res = index.query(subword_query, conditions=obs, limit=5, token_budget=8000)
        hit_ids = [row["id"] for row in res["results"]]
        assert expected_id in hit_ids, f"Subword query {subword_query!r} missed {expected_id}: {hit_ids}"

    # Exact compound symbol still outranks partial subword distractor
    exact_unit = _make_unit(
        "unit-exact-datacopypad",
        title="DataCopyPad GM-to-UB contract",
        keywords=["DataCopyPad"],
        conditions={"npu_model": ["Ascend910B4"]},
        body="Invoke DataCopyPad when copying unaligned GM tensors.",
    )
    partial_unit = _make_unit(
        "unit-partial-data-copy",
        title="General data copy helper",
        keywords=["data", "copy"],
        conditions={"npu_model": ["Ascend910B4"]},
        body="Perform data copy without padding.",
    )
    pair_index = RoutingIndex([partial_unit, exact_unit])
    ranked = pair_index.query("DataCopyPad", conditions={"npu_model": "Ascend910B4"})["results"]
    assert [row["id"] for row in ranked] == ["unit-exact-datacopypad", "unit-partial-data-copy"]
    assert ranked[0]["score"] > ranked[1]["score"]


def test_evaluate_retrieval_suite_standard_explore_conditions_and_foundation(store):
    corpus = _build_ascend_eval_corpus()
    index = RoutingIndex(corpus)
    store.graded.install(corpus)

    cases = [
        RetrievalEvalCase(
            case_id="std-datacopypad-subwords",
            query="DataCopyPad SetFlag WaitFlag",
            conditions={"npu_model": "Ascend910B4"},
            mode="standard",
            expected_unit_ids=[
                "unit-ascendc-datacopypad-tail-sop",
                "unit-ascendc-tiling-routine-workflow",
            ],
            forbidden_unit_ids=["unit-a3-dual-die-roce-sop"],
            require_foundation_baseline=["unit-ascend-910b-ub-alignment-axiom"],
            top_k=5,
        ),
        RetrievalEvalCase(
            case_id="std-gdn-aclgraph",
            query="GatedDeltaNet ACLGraph aclrtSynchronizeStream",
            conditions={"npu_model": "Ascend910B4", "graph_mode": "ACLGraph"},
            mode="standard",
            expected_unit_ids=["unit-vllm-ascend-gateddeltanet-aclgraph-sop"],
            forbidden_unit_ids=["unit-a3-dual-die-roce-sop"],
            require_foundation_baseline=True,
            top_k=3,
        ),
        RetrievalEvalCase(
            case_id="explore-strips-l3-retains-l1-l2-l4",
            query="DataCopyPad PipeBarrier tiling",
            conditions={"npu_model": "Ascend910B4"},
            mode="explore",
            expected_unit_ids=[
                "unit-ascend-910b-ub-alignment-axiom",
                "unit-ascendc-datacopypad-tail-sop",
                "unit-ascendc-vectorized-pad-mutator",
            ],
            forbidden_unit_ids=[
                "unit-ascendc-tiling-routine-workflow",
                "unit-a3-dual-die-roce-sop",
            ],
            require_foundation_baseline=["unit-ascend-910b-ub-alignment-axiom"],
            top_k=5,
        ),
        RetrievalEvalCase(
            case_id="cold-query-mounts-foundation-baseline",
            query="completely_unseen_cold_query_token",
            conditions={"npu_model": "Ascend910B4"},
            mode="standard",
            expected_unit_ids=["unit-ascend-910b-ub-alignment-axiom"],
            forbidden_unit_ids=["unit-a3-dual-die-roce-sop"],
            require_foundation_baseline=True,
            top_k=3,
        ),
    ]

    for target in (index, store.graded):
        report = evaluate_retrieval_suite(
            target,
            cases,
            min_recall_at_k=1.0,
            min_mrr=0.8,
            min_foundation_baseline_rate=1.0,
            max_forbidden_leak_rate=0.0,
        )
        assert report["passed"] is True
        assert report["recall_at_k"] == pytest.approx(1.0)
        assert report["mrr"] >= 0.8
        assert report["foundation_baseline_rate"] == pytest.approx(1.0)
        assert report["forbidden_leak_rate"] == pytest.approx(0.0)
        assert report["failed_case_ids"] == []
        explore_case = next(
            item for item in report["per_case_results"]
            if item["case_id"] == "explore-strips-l3-retains-l1-l2-l4"
        )
        assert explore_case["explore_l3_stripped"] is True
        assert "unit-ascendc-tiling-routine-workflow" in explore_case["stripped_l3_ids"]
        assert "unit-ascendc-tiling-routine-workflow" not in explore_case["ranked_unit_ids"]


def test_evaluate_retrieval_suite_detects_forbidden_leak_and_threshold_failures():
    corpus = _build_ascend_eval_corpus()
    index = RoutingIndex(corpus)

    # In standard mode, the L3 workflow is returned; marking it forbidden triggers a leak failure.
    leaking_suite = evaluate_retrieval_suite(
        index,
        [
            {
                "id": "leak-check",
                "query": "DataCopyPad PipeBarrier tiling",
                "conditions": {"npu_model": "Ascend910B4"},
                "mode": "standard",
                "expected_unit_ids": ["unit-ascendc-tiling-routine-workflow"],
                "forbidden_unit_ids": ["unit-ascendc-tiling-routine-workflow"],
                "top_k": 5,
            },
            {
                "id": "missing-unit-check",
                "query": "DataCopyPad",
                "conditions": {"npu_model": "Ascend910_93"},
                "mode": "standard",
                "expected_unit_ids": ["unit-ascendc-datacopypad-tail-sop"],
                "require_foundation_baseline": True,
                "top_k": 3,
            },
        ],
        min_recall_at_k=0.9,
        min_mrr=0.9,
        min_foundation_baseline_rate=1.0,
        max_forbidden_leak_rate=0.0,
    )
    assert leaking_suite["passed"] is False
    assert leaking_suite["recall_at_k"] == pytest.approx(0.5)
    assert leaking_suite["mrr"] == pytest.approx(0.5)
    assert leaking_suite["foundation_baseline_rate"] == pytest.approx(0.0)
    assert leaking_suite["forbidden_leak_rate"] == pytest.approx(0.5)
    assert set(leaking_suite["failed_case_ids"]) == {"leak-check", "missing-unit-check"}
    assert len(leaking_suite["failures"]) == 4
