"""Tiered X/L1/L2/L3/L4 contract matrix for the graded knowledge runtime.

Each test names the plan case it protects. Inputs are synthetic protocol values;
no test claims a hardware measurement. The live A2/A3 paths are exercised in
``test_live_npu_knowledge_loop.py``.
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from graded_fixture import CONDITIONS, OBSERVED, unit
from mindie_knowledge.graded.ablation import classify_heldout_cases
from mindie_knowledge.graded.conditions import match_conditions
from mindie_knowledge.graded.explore import (
    assess_stagnation,
    build_exploration_context,
    evaluate_exploration_candidate,
)
from mindie_knowledge.graded.feedback import record_usage_event, summarize_usage_events
from mindie_knowledge.graded.index import RoutingIndex
from mindie_knowledge.graded.lifecycle import supersede_unit, suspect_unit, transition_unit
from mindie_knowledge.graded.maintenance import growth_metrics
from mindie_knowledge.graded.units import GradedUnit


def _l1(ident="unit-matrix-premise", **updates):
    values = dict(tier="L1", category="axiom", evidence_strength="software_contract",
                  domain_path="_common", body="Keep the reduction accumulator in fp32.")
    values.update(updates)
    return unit(ident, **values)


def _l2(ident="unit-matrix-procedure", *, parents=(), body=None, **updates):
    values = dict(tier="L2", category="sop", parents=parents,
                  body=body or "Warm the device, synchronize, then record the exact revision.")
    values.update(updates)
    return unit(ident, **values)


def _l3(ident, *, parents=(), body=None, **updates):
    values = dict(tier="L3", category="workflow", parents=parents,
                  body=body or "Compare the measured bottleneck before choosing a workflow.")
    values.update(updates)
    return unit(ident, **values)


def _l4(ident, *, conditions=None, parents=(), body=None):
    values = dict(tier="L4", category="mutator", evidence_strength="hypothesis",
                  parents=parents, conditions=conditions or deepcopy(CONDITIONS),
                  body=body or "Apply the native fastpath. Fallback when the tail is unaligned.")
    return unit(ident, **values)


def test_l1_01_to_l1_04_applicability_boundaries_and_distractor_resistance():
    required = {
        "soc": ["Ascend910B"],
        "cann": ">=9.0,<10",
        "dtype": "bfloat16",
        "workload": {"exact_set": [{"shape": "1x37"}, {"shape": "2x131"}]},
    }
    observed = {"soc": "Ascend910B", "cann": "9.1", "dtype": "bfloat16",
                "workload": [{"shape": "2x131"}, {"shape": "1x37"}]}
    assert match_conditions(required, observed).status == "matched"
    assert match_conditions(required, {**observed, "soc": "Ascend910C"}).status == "mismatch"
    assert match_conditions(required, {**observed, "cann": "8.1"}).status == "mismatch"
    assert match_conditions(required, {"soc": "Ascend910B"}).status == "unknown"
    assert match_conditions(required, {**observed, "workload": [{"shape": "1x37"}]}).status == "mismatch"

    current = unit("unit-matrix-current", conditions=required,
                   body="Current path: vectorize the aligned reduction tail.")
    distractor = unit("unit-matrix-distractor",
                      conditions={**required, "soc": ["Ascend910C"]},
                      body="Current path: vectorize the aligned reduction tail. " * 4)
    index = RoutingIndex([current, distractor])
    found = index.query("vectorize reduction tail", conditions=observed)
    assert [row["id"] for row in found["results"]] == [current.id]
    assert found["results"][0]["ready_to_apply"] is True
    assert found["excluded_conditions"]["mismatch"] == 1

    # A no-hit query is a valid diagnostic state, not a failure or a fabricated unit.
    empty = index.query("unrelatedtoken", conditions=observed)
    assert empty["results"] == []
    advised = index.query("vectorize", conditions={"soc": "Ascend910B"}, include_unknown=True)
    assert advised["results"][0]["match_status"] == "condition_unknown"
    assert advised["results"][0]["ready_to_apply"] is False


def test_l2_01_to_l2_04_complete_premises_and_premise_failure_never_refute():
    premise = _l1(body="The tail must be masked with the reduction identity.")
    procedure = _l2(parents=[premise.id], body="STEP 1: verify the tail.\n" * 120)
    index = RoutingIndex([premise, procedure])
    full = index.explain_closure([procedure.id], conditions=OBSERVED, token_budget=16000)
    assert [item["id"] for item in full["requires"]] == [premise.id]
    assert full["units"][0]["content"] == procedure.body
    assert all(item["ready_to_apply"] for item in full["requires"] + full["units"])

    narrow = index.explain_closure([procedure.id], conditions=OBSERVED, token_budget=8)
    assert narrow["units"] == []
    assert narrow["unresolved"][0]["status"] == "budget_excluded"
    assert "STEP 1" not in str(narrow)

    graph = unit("unit-matrix-graph", conditions={**CONDITIONS, "graph_mode": "ACLGraph"},
                 body="Probe capture, replay, then compare eager logits.")
    capture = RoutingIndex([graph]).query("capture replay", conditions=OBSERVED)
    assert capture["results"] == []
    assert capture["excluded_conditions"]["unknown"] == 1
    mismatch = RoutingIndex([graph]).query("capture replay", conditions={**OBSERVED, "graph_mode": "eager"})
    assert mismatch["results"] == [] and mismatch["excluded_conditions"]["mismatch"] == 1

    triton = unit("unit-matrix-triton", conditions={"kernel_language": "triton"},
                  body="A constexpr length stays in the specialization key.")
    assert RoutingIndex([triton]).query("constexpr", conditions={"kernel_language": "triton"})["results"]
    assert RoutingIndex([triton]).query("constexpr", conditions={"kernel_language": "cpu"})["results"] == []

    common = dict(run_id="run-l2-04", candidate_id="cand-invalid", mutation_summary="tile",
                  conditions={"soc": "Ascend910B"}, compile_ok=True, oracle_passed=True,
                  measurement_passed=False)
    invalid = evaluate_exploration_candidate(speedup_ratio=1.4, **common)
    assert invalid["axes"] == {"evidence_validity": "measurement_invalid",
                               "goal_attainment": "not_applicable",
                               "reusability": "inconclusive"}
    assert invalid["distill_ready"] is False and invalid["conditional_avoid"] == []
    repair = assess_stagnation([
        {"run_id": "run-l2-04-a", "outcome": "infra_failed"},
        {"run_id": "run-l2-04-b", "verdict": "inconclusive"},
    ])
    assert repair["status"] == "repair_execution_conditions" and repair["stalled"] is False


def test_x01_x02_shared_parent_budget_compaction_and_domain_boundary():
    premise = _l1(revision=2)
    procedure = _l2(parents=[premise.id], body="Warm the allocator before measuring.")
    left = _l3("unit-matrix-left", parents=[procedure.id], body="Left capture workflow. " * 30)
    right = _l3("unit-matrix-right", parents=[procedure.id], body="Right capture workflow. " * 30)
    index = RoutingIndex([premise, procedure, left, right])

    closure = index.explain_closure([left.id, right.id], conditions=OBSERVED, token_budget=16000)
    assert [item["id"] for item in closure["requires"]] == [premise.id, procedure.id]
    assert len(closure["requires"]) == 2  # shared parents are charged once

    compacted = index.explain_closure([left.id], conditions=OBSERVED,
                                      known_refs=[f"{premise.id}@{premise.revision}",
                                                  f"{procedure.id}@{procedure.revision}"])
    assert compacted["requires"] == []
    assert [item["id"] for item in compacted["units"]] == [left.id]
    stale = index.explain_closure([left.id], conditions=OBSERVED,
                                  known_refs=[f"{premise.id}@1",
                                              f"{procedure.id}@{procedure.revision}"])
    assert [item["id"] for item in stale["requires"]] == [premise.id]

    replacement = _l1("unit-matrix-replacement", body="Current replacement premise.")
    withdrawn = _l1("unit-matrix-withdrawn", status="superseded",
                    superseded_by=replacement.id, body="Withdrawn premise.")
    child = _l3("unit-matrix-child", parents=[withdrawn.id], body="Child workflow.")
    blocked = RoutingIndex([replacement, withdrawn, child]).explain_closure([child.id], conditions=OBSERVED)
    assert blocked["units"] == []
    assert blocked["unresolved"][0]["status"] == "removed_or_superseded"
    assert blocked["unresolved"][0]["blocked_by"] == withdrawn.id

    foreign = index.explain_closure(
        [f"mindie://other-domain/units/{left.id}"], conditions=OBSERVED, domain="vllm-ascend")
    assert foreign["units"] == []
    assert foreign["unresolved"][0]["status"] == "not_found"


def test_x05_duplicate_and_inconclusive_events_never_change_support(store):
    item = _l2(body="Compare eager and graph capture before recording support.")
    store.graded.install([item])
    store.query("graph capture", conditions=OBSERVED, run_id="run-x05")
    observed = store.query("graph capture", conditions=OBSERVED)["results"][0]["ref"]
    first = record_usage_event(store, root_hash="a" * 64, ref=observed,
                               event_type="evaluated", run_id="run-x05-a", outcome="supported")
    duplicate = record_usage_event(store, root_hash="a" * 64, ref=observed,
                                   event_type="evaluated", run_id="run-x05-a", outcome="supported")
    record_usage_event(store, root_hash="a" * 64, ref=observed,
                       event_type="evaluated", run_id="run-x05-b", outcome="inconclusive",
                       reason="device unavailable before measurement")
    assert first["duplicate"] is False and duplicate["duplicate"] is True
    summary = summarize_usage_events(store, item.id, 1)
    assert summary["evaluated"] == 2 and summary["supported"] == 1 and summary["inconclusive"] == 1
    # A feedback event is a signal, not an automatic promotion or demotion gate.
    assert store.explain(observed)["status"] == "active"


def test_x06_growth_retains_a_rare_condition_after_new_mechanisms():
    common = [_l2(f"unit-matrix-common-{index}", body="Common workflow. " * 8) for index in range(4)]
    rare = _l3("unit-matrix-rare", conditions={**CONDITIONS, "soc": ["Ascend910_93"]},
               body="Rare 910_93 dual-die exception with a distinct fallback.")
    metrics = growth_metrics([*common, rare], completed_milestones=2,
                             previous_units=common[:2], loaded_tokens_delta=40)
    assert metrics["active_units"] == 5
    assert metrics["g_units"] == 1.5
    assert metrics["g_body"] > 0 and metrics["g_read"] == 40
    found = RoutingIndex([*common, rare]).query(
        "distinct fallback", conditions={**OBSERVED, "soc": "Ascend910_93"})
    assert [row["id"] for row in found["results"]] == [rare.id]


def test_l3_01_same_symptom_selects_the_different_bottleneck_workflow():
    bandwidth = _l3("unit-matrix-bandwidth", body="MTE2 HBM bandwidth saturation.")
    compute = _l3("unit-matrix-compute", body="Cube compute saturation and tile reuse.")
    index = RoutingIndex([bandwidth, compute])
    assert [row["id"] for row in index.query("MTE2 bandwidth", conditions=OBSERVED)["results"]] == [bandwidth.id]
    assert [row["id"] for row in index.query("Cube compute", conditions=OBSERVED)["results"]] == [compute.id]


def test_l3_02_idle_and_active_conditions_keep_conditional_avoidance_scoped():
    ledger = [{"run_id": "run-idle", "verdict": "refuted",
               "axes": {"evidence_validity": "valid"},
               "conditions": {"idle_rank_state": "no_scheduled_requests"},
               "mutation_summary": "dummy run wrote GDN state"}]
    context = build_exploration_context(
        goal="stabilize idle DP", retrieved_units=[{"id": "unit-old-l3", "tier": "L3", "body": "old"}],
        bottleneck_summary={"layer": "gdn"}, ledger_entries=ledger,
        conditions={"idle_rank_state": "no_scheduled_requests"}, native_subcontext=True)
    assert context["stripped_l3_ids"] == ["unit-old-l3"]
    assert [item["run_id"] for item in context["conditional_avoid"]] == ["run-idle"]
    active = build_exploration_context(
        goal="stabilize active DP", retrieved_units=[], bottleneck_summary={"layer": "gdn"},
        ledger_entries=ledger, conditions={"idle_rank_state": "active_requests"})
    assert active["conditional_avoid"] == []


def test_l3_lifecycle_active_to_suspect_to_superseded_keeps_evidence_auditable():
    premise = _l1()
    rule = _l2(parents=[premise.id], body="Use the native fused path.")
    active = RoutingIndex([premise, rule])
    assert active.query("native fused", conditions=OBSERVED)["results"][0]["ready_to_apply"] is True

    with pytest.raises(ValueError, match="illegal lifecycle transition"):
        transition_unit(rule, "superseded", replacement="unit-matrix-successor")
    suspect = suspect_unit(rule, reason="run-c regressed the decode path")
    assert suspect.status == "suspect" and suspect.revision == rule.revision + 1
    assert suspect.body == rule.body
    assert suspect.metadata["provenance"] == rule.metadata["provenance"]
    revised = RoutingIndex([premise, suspect])
    assert suspect.id not in {row["id"] for row in revised.query("native fused", conditions=OBSERVED)["results"]}
    audit = revised.query("native fused", conditions=OBSERVED, include_archived=True)
    audited = next(row for row in audit["results"] if row["id"] == suspect.id)
    assert audited["status"] == "suspect"
    assert audited["ready_to_apply"] is False
    explained = revised.explain_closure([suspect.id], conditions=OBSERVED)
    assert explained["units"][0]["ready_to_apply"] is False

    child = _l3("unit-matrix-suspect-child", parents=[suspect.id], body="Child must not adopt.")
    blocked = RoutingIndex([premise, suspect, child]).explain_closure([child.id], conditions=OBSERVED)
    assert blocked["unresolved"][0]["status"] == "suspect_ancestor"
    assert blocked["unresolved"][0]["blocked_by"] == suspect.id

    successor = _l2("unit-matrix-successor", body="Replacement fused path.")
    superseded = supersede_unit(suspect, successor.id, reason="replacement verified")
    assert superseded.status == "superseded" and superseded.superseded_by == successor.id
    old = RoutingIndex([premise, superseded, successor]).explain_closure([superseded.id], conditions=OBSERVED)
    assert old["units"] == []
    assert old["unresolved"][0]["status"] == "removed_or_superseded"


def test_l4_01_applicability_and_fallback_boundary_are_conditions_not_prose():
    conditions = {
        "soc": ["Ascend910B", "Ascend910B4"],
        "dtype": "bfloat16",
        "tail_alignment": "32B",
        "register_pressure": "headroom",
    }
    mutator = _l4("unit-matrix-l4-boundary", conditions=conditions,
                  body="Apply the fused store. Fallback when the tail is unaligned or UB spills.")
    index = RoutingIndex([mutator])
    observed = {"soc": "Ascend910B4", "dtype": "bfloat16",
                "tail_alignment": "32B", "register_pressure": "headroom"}
    assert index.query("fused store", conditions=observed)["results"][0]["ready_to_apply"] is True
    for external in ({"tail_alignment": "unaligned"},
                     {"register_pressure": "spill"},
                     {"dtype": "float32"}):
        assert index.query("fused store", conditions={**observed, **external})["results"] == []
    unknown = index.query("fused store", conditions={"soc": "Ascend910B4"}, include_unknown=True)
    assert unknown["results"][0]["match_status"] == "condition_unknown"
    assert unknown["results"][0]["ready_to_apply"] is False
    assert "Fallback" in mutator.body and "unaligned" in mutator.body


def test_l4_02_to_l4_04_two_mutator_families_five_buckets_and_guard_protection():
    fastpath = _l4("unit-matrix-l4-fastpath", conditions={"soc": ["Ascend910B"], "dtype": "bfloat16"})
    splitk = _l4("unit-matrix-l4-splitk", conditions={"soc": ["Ascend910_93"], "dtype": "bfloat16"})
    index = RoutingIndex([fastpath, splitk])
    mutators = [unit for unit in index.units.values() if unit.metadata["tier"] == "L4"]
    assert {unit.id for unit in mutators} == {fastpath.id, splitk.id}
    assert {row["id"] for row in index.query("fallback", conditions={"soc": "Ascend910B", "dtype": "bfloat16"})["results"]} == {fastpath.id}
    assert {row["id"] for row in index.query("fallback", conditions={"soc": "Ascend910_93", "dtype": "bfloat16"})["results"]} == {splitk.id}

    five = classify_heldout_cases([
        {"case_id": "both", "baseline_pass": True, "candidate_pass": True,
         "baseline_ms": 12.0, "candidate_ms": 10.0},
        {"case_id": "regression", "baseline_pass": True, "candidate_pass": False},
        {"case_id": "both-fail", "baseline_pass": False, "candidate_pass": False},
        {"case_id": "improvement", "baseline_pass": False, "candidate_pass": True},
        {"case_id": "error", "baseline_pass": True, "candidate_pass": True,
         "error": "device lost before measurement"},
    ])
    assert five["buckets"] == {"both_pass": 1, "opt_regression": 1, "both_fail": 1,
                               "opt_improvement": 1, "evaluation_error": 1}
    assert five["correctness_retention"] == pytest.approx(0.5)
    assert five["speedup_geomean"] == pytest.approx(1.2)
    assert five["complete"] is False

    guarded = classify_heldout_cases([
        {"case_id": "main", "baseline_pass": True, "candidate_pass": True,
         "baseline_ms": 10.0, "candidate_ms": 5.0},
        {"case_id": "workspace-guard", "guard": True, "baseline_pass": True,
         "candidate_pass": True, "baseline_ms": 10.0, "candidate_ms": 12.0},
    ])
    assert guarded["main_speedup_geomean"] == pytest.approx(2.0)
    assert guarded["guard_speedup_geomean"] == pytest.approx(10 / 12)
    assert guarded["guard_ok"] is False
    assert guarded["complete"] is True

    unmeasured_guard = classify_heldout_cases([
        {"case_id": "guard-no-timing", "guard": True,
         "baseline_pass": True, "candidate_pass": True},
    ])
    assert unmeasured_guard["buckets"]["both_pass"] == 1
    assert unmeasured_guard["guard_ok"] is False
    assert unmeasured_guard["speedup_geomean"] is None
