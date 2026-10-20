"""Wave 7 RSI closure, stagnation, exploration-context and gate contracts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from mindie_knowledge.graded.explore import (
    assess_stagnation,
    build_exploration_context,
    evaluate_exploration_candidate,
)
from mindie_knowledge.graded.index import RoutingIndex
from mindie_knowledge.graded.maintenance import build_index, validate_repository
from mindie_knowledge.graded.units import GradedUnit

OPERATORS = Path(
    os.environ.get("MINDIE_OPERATORS_ROOT")
    or (Path(__file__).resolve().parents[2] / "knowledge-operators")
)
NEW_L4 = "unit-triton-constexpr-to-runtime-scalar-specialization-mutator"


def _unit(ident, tier, category, conditions, *, parents=(), body="", path=None):
    strength = {"L1": "software_contract", "L4": "reproduced_empirical"}.get(
        tier, "single_observation")
    return GradedUnit.from_parts(
        {"schema": "mindie-graded-unit/1", "id": ident, "tier": tier,
         "category": category, "evidence_strength": strength,
         "domain_path": path or ("_common" if tier == "L1" else "test/kernels"),
         "title": ident,
         "triggers": {"error_signatures": [], "keywords": [ident]},
         "conditions": conditions, "inherits_from": list(parents),
         "provenance": {"source_type": "synthetic_contract", "source_locator": "fixture",
                        "evidence_refs": ["fixture"], "supersedes": []}},
        body or f"Body for {ident}.", f"{path or 'test/kernels'}/{ident}.md")


def test_navigation_only_child_of_unmatched_parent_is_not_ready():
    parent = _unit("unit-p1-parent", "L1", "axiom", {"dp": [1]},
                   body="Hardware premise: DP one only.")
    child = _unit("unit-p1-child", "L2", "sop", {"soc": ["Ascend910B"]},
                  parents=[parent.id], body="Child alpha workflow that needs the parent.")
    index = RoutingIndex([parent, child])
    result = index.query("child alpha", conditions={"soc": "Ascend910B", "dp": 2},
                         navigation_only=True, token_budget=8000)
    card = next(item for item in result["cards"] if item["id"] == child.id)
    assert card["ready_to_apply"] is False
    assert card["conditions_matched"] is True
    assert f"parent_unmatched:{parent.id}:mismatch" in card["missing_conditions"]


def test_stagnation_keeps_latest_run_update_and_exposes_escalation():
    entries = [
        {"run_id": "run-1", "outcome": "no_improvement"},
        {"run_id": "run-1", "outcome": "improvement", "new_observation": True},
        {"run_id": "run-2", "outcome": "no_improvement"},
        {"run_id": "run-3", "outcome": "no_improvement"},
    ]
    # The later run-1 update is progress, so only run-2 and run-3 are stalled.
    progress = assess_stagnation(entries)
    assert progress["stalled"] is False and progress["stalled_count"] == 2
    assert progress["should_escalate"] is False
    assert progress["tail_run_ids"] == ["run-1", "run-2", "run-3"]
    stalled = assess_stagnation([
        {"run_id": "run-a", "outcome": "no_improvement"},
        {"run_id": "run-b", "outcome": "no_improvement"},
        {"run_id": "run-b", "outcome": "no_improvement"},
        {"run_id": "run-c", "outcome": "no_improvement"},
    ], consecutive_limit=3)
    assert stalled["stalled"] is True and stalled["should_escalate"] is True
    assert stalled["stalled_count"] == 3
    assert stalled["escalation_reason"] == "consecutive_unimproved_limit_reached"
    assert stalled["consecutive_limit"] == 3
    assert stalled["budget_exhausted"] is False


def test_explore_mode_strips_l3_but_keeps_l1_premises_and_fact_pack(store):
    premise = _unit("unit-explore-l1", "L1", "axiom", {"soc": ["Ascend910B"]},
                    body="UB 32B hardware premise for the workflow.", path="_common")
    procedure = _unit("unit-explore-l2", "L2", "sop", {"soc": ["Ascend910B"]},
                      parents=[premise.id], body="Diagnose the compile count first.")
    workflow = _unit("unit-explore-l3", "L3", "workflow", {"soc": ["Ascend910B"]},
                     parents=[procedure.id], body="Old workflow alpha recommendation.")
    mutator = _unit("unit-explore-l4", "L4", "mutator",
                    {"soc": ["Ascend910B"], "dtype": "bf16"},
                    body="Apply the runtime scalar mutator.")
    index = RoutingIndex([premise, procedure, workflow, mutator])
    conditions = {"soc": "Ascend910B", "dtype": "bf16"}
    result = index.query("workflow alpha", conditions=conditions, mode="explore",
                         token_budget=16000, goal="remove recompiles",
                         ledger_entries=[{"run_id": "run-1", "verdict": "refuted",
                                          "axes": {"evidence_validity": "valid"},
                                          "conditions": conditions,
                                          "mutation_summary": "bad static tile",
                                          "reason": "regressed"}])
    assert result["mode"] == "explore"
    assert result["stripped_l3_ids"] == [workflow.id]
    assert workflow.id not in {row["id"] for row in result["results"]}
    assert premise.id in {row["id"] for row in result["foundation_cards"]}
    context = result["exploration_context"]
    assert context["stagnation"]["stalled"] is False
    assert context["conditional_avoid"][0]["unit_id"] == ""
    pack = context["subagent_fact_pack"]
    assert pack["schema_version"] == "mindie-rsi-subagent-pack/1"
    assert pack["stripped_l3_ids"] == [workflow.id]
    assert premise.id in pack["l1_foundation_ids"]
    assert workflow.id not in {unit["id"] for unit in context["mounted_units"]}

    store.graded.install([premise, procedure, workflow, mutator])
    delivered = store.query("workflow alpha", conditions=conditions, mode="explore",
                            token_budget=16000)
    assert delivered["mode"] == "explore"
    assert workflow.id not in {row["id"] for row in delivered["results"]}
    assert workflow.id in delivered["stripped_l3_ids"]
    assert premise.id in delivered["exploration_context"]["subagent_fact_pack"]["l1_foundation_ids"]


def test_four_gates_and_explore_cli(tmp_path):
    base = {"run_id": "run-gate", "candidate_id": "cand-gate", "unit_id": "unit-cand",
            "compile_ok": True, "correctness_ok": True, "speedup": 1.12,
            "hidden_input_summary": {"buckets": {"opt_regression": 0}},
            "ablation_ok": True, "workspace_delta_bytes": 512,
            "guard_workloads": [{"baseline_ms": 10.0, "candidate_ms": 10.1}],
            "conditions": {"soc": "Ascend910B"}}
    passed = evaluate_exploration_candidate(base)
    assert passed["gate_reached"] == "PASSED"
    assert passed["outcome"] == "qualified_improvement"
    assert passed["verdict"] == "distill_candidate" and passed["distill_ready"] is True
    assert set(passed["gates"]) == {"G0", "G1", "G2", "G3"}
    assert all(gate["passed"] for gate in passed["gates"].values())

    assert evaluate_exploration_candidate({**base, "compile_ok": False})["outcome"] == "compile_failed"
    assert evaluate_exploration_candidate({
        **base, "hidden_input_summary": {"opt_regression": 1}})["outcome"] == "correctness_failed"
    assert evaluate_exploration_candidate({**base, "speedup": 1.01})["outcome"] == \
        "sub_target_valid_evidence"
    assert evaluate_exploration_candidate({**base, "workspace_delta_bytes": 8192})["outcome"] == \
        "ablation_or_hygiene_failed"
    assert evaluate_exploration_candidate({**base, "guard_workloads": [
        {"baseline_ms": 10.0, "candidate_ms": 14.0}]})["gate_reached"] == "G3"

    # Minimal historical candidates do not gain fabricated constraints.
    minimal = evaluate_exploration_candidate({"compile_ok": True, "correctness_ok": True,
                                              "speedup": 1.1})
    assert minimal["gate_reached"] == "PASSED" and minimal["verdict"] == "distill_candidate"

    cli_input = tmp_path / "candidate.json"
    cli_input.write_text(json.dumps({"candidate": base, "target_speedup": 1.05}),
                         encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-m", "mindie_knowledge.graded.explore", "evaluate",
         "--input", str(cli_input)],
        capture_output=True, text=True, check=True,
        cwd=Path(__file__).resolve().parents[1], env={**os.environ})
    cli = json.loads(completed.stdout)
    assert cli["gate_reached"] == "PASSED" and cli["verdict"] == "distill_candidate"


def test_exploration_context_promotes_l3_foundation_dicts():
    context = build_exploration_context(
        [{"id": "unit-old-l3", "tier": "L3", "body": "old",
          "inherited_rules": [
              {"id": "unit-promoted-l1", "tier": "L1", "category": "axiom",
               "body": "32 B UB premise"},
              {"id": "unit-promoted-l2", "category": "sop", "body": "triage premise"},
              {"id": "unit-skipped-l3", "tier": "L3", "body": "nested workflow"}]}],
        goal="promote premises", native_subcontext=True)
    assert context["stripped_l3_ids"] == ["unit-old-l3"]
    assert {unit["id"] for unit in context["mounted_units"]} == {
        "unit-promoted-l1", "unit-promoted-l2"}
    assert context["subagent_fact_pack"]["l1_foundation_ids"] == ["unit-promoted-l1"]
    assert context["direction_budget_policy"] == {
        "kb_dir_cap": 1, "require_cold_direction": True, "roofline_status": "calibrated"}
    assert len(context["subagent_fact_pack"]["prompt_brief"]) < 1200


def test_geak_direction_budget_and_survivor_bias_free_citation_feedback():
    from mindie_knowledge.graded.explore import (
        build_citation_feedback_record,
        enforce_direction_budget,
    )

    budget = enforce_direction_budget(
        [
            {"direction_id": "d1", "hypothesis": "Reach 90% of peak HBM roofline", "learned_refs": ["u1"]},
            {"direction_id": "d2", "hypothesis": "Vectorized fp32 cast", "learned_refs": ["u1"]},
            {"direction_id": "d3", "hypothesis": "Tile size 256", "learned_refs": ["u2"]},
        ],
        kb_dir_cap=1,
        require_cold_direction=True,
        roofline_status="unknown",
    )
    assert budget["filtered_uncalibrated_roofline_ids"] == ["d1"]
    assert budget["kb_cap_bound"] is True
    assert budget["kb_seeded_count"] == 1
    assert budget["cold_direction_count"] == 1
    assert budget["directions"][0]["learned_refs"] == ["u1"]
    assert budget["directions"][1]["learned_refs"] == []
    assert budget["directions"][1]["is_cold_direction"] is True

    # A round achieving 1.5x when incumbent is already 2.5x must not become winner or credit KB cards.
    regressed_vs_incumbent = build_citation_feedback_record(
        {"run_id": "r2", "candidate_id": "c2", "speedup": 1.5, "evidence_valid": True, "distill_ready": True},
        cited_unit_ids=["u1"],
        incumbent_speedup=2.5,
        min_improve_delta=0.01,
    )
    assert regressed_vs_incumbent["improved_over_incumbent"] is False
    assert regressed_vs_incumbent["became_winner"] is False
    assert regressed_vs_incumbent["cited_then_verified"] is False
    assert regressed_vs_incumbent["record_in_feedback_ledger"] is True
    assert regressed_vs_incumbent["allow_new_card_distillation"] is False

    # Verifier timeout/infra void must encode cited_then_verified=None (never False/0).
    void_record = build_citation_feedback_record(
        {"run_id": "r3", "candidate_id": "c3", "outcome": "infra_failed"},
        cited_unit_ids=["u1"],
        incumbent_speedup=2.5,
        verification_void=True,
    )
    assert void_record["cited_then_verified"] is None
    assert void_record["record_in_feedback_ledger"] is True



@pytest.mark.skipif(
    not (OPERATORS / "units/L4/optimization" / f"{NEW_L4}.md").is_file(),
    reason="knowledge-operators checkout is outside the writable workspace")
def test_knowledge_operators_has_two_l4_mutators_and_stable_index():
    assert validate_repository(OPERATORS)["valid"] is True
    report = build_index(OPERATORS, check=False)
    assert report["ok"] is True
    index = RoutingIndex.from_directory(OPERATORS)
    assert NEW_L4 in index.units
    assert index.units[NEW_L4].metadata["tier"] == "L4"
    assert index.units[NEW_L4].metadata["inherits_from"] == [
        "unit-ascend-ub-vector-alignment-and-tiling-basics",
        "unit-msprof-aicore-pipe-bottleneck-triage"]
    tiers = [unit for unit in index.units.values() if unit.metadata["tier"] == "L4"]
    assert len(tiers) == 2
