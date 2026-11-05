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
    build_strategy_navigation_cards,
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
                    body="Apply the runtime scalar mutator for workflow alpha.")
    index = RoutingIndex([premise, procedure, workflow, mutator])
    conditions = {"soc": "Ascend910B", "dtype": "bf16"}
    reuse_res = index.query("workflow alpha", conditions=conditions, mode="reuse",
                            limit=1, token_budget=16000)
    assert [row["id"] for row in reuse_res["results"]] == [workflow.id]

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
    assert mutator.id in {row["id"] for row in result["results"]}
    assert premise.id in {row["id"] for row in result["foundation_cards"]}
    context = result["exploration_context"]
    assert [row["id"] for row in context["l4_mutators"]] == [mutator.id]
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


def test_strategy_navigation_cards_remain_body_free_and_expose_condition_fit():
    ready_workflow = _unit(
        "unit-strategy-ready",
        "L3",
        "workflow",
        {"soc": ["Ascend910B"], "dp": [2]},
        body="SECRET_BODY_ALPHA that must never enter navigation cards.",
    )
    missing_sop = _unit(
        "unit-strategy-missing",
        "L2",
        "sop",
        {"soc": ["Ascend910B"], "graph_capture": ["aclgraph"]},
        body="SECRET_BODY_BETA that must never enter navigation cards.",
    )
    cards = build_strategy_navigation_cards(
        [ready_workflow, missing_sop],
        [ready_workflow.id, missing_sop.id],
        conditions={"soc": "Ascend910B", "dp": 2},
    )
    assert [card["id"] for card in cards] == ["unit-strategy-ready", "unit-strategy-missing"]
    for card in cards:
        assert "body" not in card
        assert "SECRET_BODY" not in json.dumps(card, ensure_ascii=False)
    assert cards[0]["ready_to_apply"] is True
    assert cards[0]["missing_conditions"] == []
    assert cards[1]["ready_to_apply"] is False
    assert "graph_capture" in cards[1]["missing_conditions"]




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


@pytest.mark.parametrize("operation", ["query", "explain"])
def test_exploration_seed_preserves_complete_premise_and_exact_reference(store, operation):
    from graded_fixture import OBSERVED, unit
    body = "Graph capture premise detail. " * 50 + "FINAL CONSTRAINT: preserve 64-byte alignment."
    premise = unit("unit-complete-explore-premise", tier="L1", category="axiom",
                   evidence_strength="software_contract", body=body)
    store.graded.install([premise])
    exact_ref = store.graded.ref(premise.id, 1)
    if operation == "query":
        page = store.query("graph capture", conditions=OBSERVED, mode="explore",
                           native_subcontext=True, token_budget=16000)
    else:
        page = store.explain(refs=[exact_ref], conditions=OBSERVED, mode="explore",
                             native_subcontext=True, token_budget=16000)
    context = page["exploration_context"]
    mounted, = context["mounted_units"]
    assert mounted["body"] == premise.body
    assert mounted["ref"] == exact_ref
    seed = context["subagent_fact_pack"]["prompt_brief"]
    assert premise.body in seed and exact_ref in seed
    assert "Do not replay prior L3 workflow steps" in seed
    assert page["budget_met"] is True
    assert not any(item["id"] == premise.id and item["status"] == "budget_excluded"
                   for item in page.get("unresolved", []))
    assert premise.id not in page.get("excluded_budget", [])


@pytest.mark.parametrize("operation", ["query", "explain"])
def test_exploration_budget_omits_whole_premise_without_claiming_delivery(store, operation):
    from graded_fixture import OBSERVED, unit
    from mindie_knowledge.graded.feedback import summarize_usage_events
    from mindie_knowledge.graded.index import response_token_estimate
    premise = unit("unit-excluded-explore-premise", tier="L1", category="axiom",
                   evidence_strength="software_contract", body="Graph capture guard. " * 400)
    store.graded.install([premise])
    exact_ref = store.graded.ref(premise.id, 1)
    if operation == "query":
        page = store.query("graph capture", conditions=OBSERVED, mode="explore", token_budget=1000)
    else:
        page = store.explain(refs=[exact_ref], conditions=OBSERVED, mode="explore", token_budget=1000)
    context = page["exploration_context"]
    assert context["mounted_units"] == [] and context["l1_foundation"] == []
    omitted, = context["excluded_units"]
    assert omitted["ref"] == exact_ref and omitted["status"] == "budget_excluded"
    seed = context["subagent_fact_pack"]["prompt_brief"]
    assert exact_ref in seed and "These bodies were withheld" in seed
    assert "Graph capture guard." not in seed
    assert summarize_usage_events(store, premise.id, include_delivery=True)[operation + "_deliveries"] == 0
    assert page["usage"]["estimated_tokens"] == response_token_estimate(page)
    if not page["budget_met"]:
        assert page["minimum_tokens"] == response_token_estimate(page)


def test_exploration_seed_keeps_l2_facts_scoped_avoidance_and_tail_instruction():
    units = [{"id": f"unit-seed-l1-{i}", "tier": "L1", "title": "A short title",
              "body": f"Complete foundation {i}. " * 30} for i in range(4)]
    units += [{"id": "unit-seed-l2", "tier": "L2", "body": "Measured bottleneck: DMA serialization."},
              {"id": "unit-seed-l4", "tier": "L4", "body": "Mutate only the launch-grid extent."}]
    context = build_exploration_context(
        units, bottleneck_summary={"summary": "Measured trace. " * 80 + "FINAL MEASUREMENT: 17 us"},
        failed_attempts=[{"run_id": "observed-run", "verdict": "refuted",
                          "conditions": {"soc": "Ascend910B"}, "mutation_summary": "remove barrier",
                          "failure_reason": "corrupts partial tiles"}],
        conditions={"soc": "Ascend910B"}, native_subcontext=True)
    seed = context["subagent_fact_pack"]["prompt_brief"]
    assert all(item["body"] in seed for item in units)
    assert "FINAL MEASUREMENT: 17 us" in seed
    assert "remove barrier" in seed and "corrupts partial tiles" in seed and "Ascend910B" in seed
    assert "Do not replay prior L3 workflow steps" in seed


def test_evaluate_exploration_candidate_measurement_guard_and_ablation_pending():
    from mindie_knowledge.graded import explore

    invalid = explore.evaluate_exploration_candidate({
        "candidate_id": "cand-incomplete",
        "mutation_summary": "fuse epilogue",
        "compile_ok": True,
        "parity_ok": False,
        "measurement_passed": False,
        "speedup": 1.25,
    })
    assert invalid["verdict"] == "reject_invalid_evidence"
    assert invalid["outcome"] == "measurement_invalid"

    invalid_legacy = explore.evaluate_exploration_candidate(
        run_id="r1",
        candidate_id="cand-incomplete",
        mutation_summary="fuse epilogue",
        conditions={"soc": "Ascend910B"},
        compile_ok=True,
        oracle_passed=False,
        measurement_passed=False,
        speedup_ratio=1.25,
    )
    assert invalid_legacy["verdict"] == "measurement_invalid"
    assert invalid_legacy["outcome"] == "measurement_invalid"
    assert invalid_legacy["reusability"] == "inconclusive"

    pending_abl = explore.evaluate_exploration_candidate({
        "candidate_id": "cand-2factor",
        "mutation_summary": "tile + double buffer",
        "compile_ok": True,
        "parity_ok": True,
        "measurement_passed": True,
        "speedup": 1.03,
        "ablation": {"passed": None},
    }, target_speedup=1.05)
    assert pending_abl["gate_reached"] == "G3"
    assert pending_abl["outcome"] == "ablation_pending"

    pending_abl_legacy = explore.evaluate_exploration_candidate(
        run_id="r2",
        candidate_id="cand-2factor",
        mutation_summary="tile + double buffer",
        conditions={"soc": "Ascend910B"},
        compile_ok=True,
        oracle_passed=True,
        measurement_passed=True,
        speedup_ratio=1.03,
        target_speedup=1.05,
        ablation_retained=None,
    )
    assert pending_abl_legacy["verdict"] == "inconclusive"
    assert pending_abl_legacy["outcome"] == "ablation_pending"

    budget = explore.enforce_direction_budget(
        [
            {"direction_id": "d1", "hypothesis": "KB-guided tile", "learned_refs": ["u1"]},
            {"direction_id": "d2", "hypothesis": "Cold profile-only control", "learned_refs": []},
        ],
        kb_dir_cap=1,
        require_cold_direction=True,
    )
    assert budget["cold_control_genuine"] is True
    assert budget["cold_control_missing"] is False


def test_unified_graded_explore_ledger_and_rsi_loop(tmp_path):
    from mindie_knowledge.graded import explore, rsi_loop

    ledger_path = tmp_path / "experiment-ledger.jsonl"
    base_ev = {
        "exp_id": "e1", "hypothesis": "tile 128", "run_ids": ["r1", "r2"],
        "repetitions": 2, "direction": "maximize", "compile_ok": True,
        "correctness": "passed", "baseline_source_digest": "sha256:base",
        "source_digest": "sha256:cand1", "scope_conditions": {"soc": "Ascend910B"},
        "baseline": [{"status": "completed", "metric": 100.0}, {"status": "completed", "metric": 100.0}],
        "candidate": [{"status": "completed", "metric": 120.0}, {"status": "completed", "metric": 120.0}],
    }
    judged = explore.judge(base_ev)
    assert judged["verdict"] == "supported"
    assert explore.judge(dict(base_ev, ablation_ok=False))["verdict"] == "refuted"
    assert explore.judge(dict(base_ev, workspace_delta_bytes=9000))["verdict"] == "refuted"
    assert explore.judge(dict(base_ev, guard_workloads=[{"regression": 0.08}]))["verdict"] == "refuted"
    assert explore.judge(dict(base_ev, hidden_input_summary={"opt_regression": True}))["verdict"] == "refuted"

    rec = dict(judged, task_id="t-unified", exp_id="e1", run_ids=["r1", "r2"], run_status="completed")
    out = explore.append(ledger_path, rec)
    assert out["status"] == "recorded"
    assert explore.gate(ledger_path, task_id="t-unified", source_digest="sha256:cand1",
                        scope={"soc": "Ascend910B"})["decision"] == "duplicate"
    assert hasattr(rsi_loop, "init") and hasattr(rsi_loop, "complete")


def test_rsi_loop_reconciles_superseded_verdicts_older_retries_and_refuted_recovery(tmp_path):
    import subprocess
    from mindie_knowledge.graded import experiment_ledger as ledger, rsi_loop as loop

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.name", "test"],
        ["config", "user.email", "test@example.invalid"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, timeout=10)
    (repo / "k.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True, timeout=10)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True, capture_output=True, timeout=10)

    state = tmp_path / "s.json"
    journal = tmp_path / "l.jsonl"
    loop.init(state, task_id="t", ledger_path=journal, baseline_repo=repo, direction="maximize", repetitions=1)

    (repo / "k.py").write_text("x = 2\n", encoding="utf-8")
    loop.complete(
        state,
        "record",
        dict(
            exp_id="e1",
            hypothesis="h1",
            candidate_repo=str(repo),
            run_ids=["r1"],
            correctness="passed",
            baseline=[dict(status="completed", metric=100.0)],
            candidate=[dict(status="completed", metric=120.0)],
        ),
    )
    (repo / "k.py").write_text("x = 3\n", encoding="utf-8")
    loop.complete(
        state,
        "record",
        dict(
            exp_id="e2",
            hypothesis="h2",
            candidate_repo=str(repo),
            run_ids=["r2"],
            correctness="passed",
            baseline=[dict(status="completed", metric=120.0)],
            candidate=[dict(status="completed", metric=144.0)],
        ),
    )

    # 1. Retrying an older record (e1) with delayed distillation receipt must keep baseline at e2 and rounds [e1, e2]
    (repo / "k.py").write_text("x = 2\n", encoding="utf-8")
    res_retry, code_retry = loop.complete(
        state,
        "record",
        dict(
            exp_id="e1",
            hypothesis="h1",
            candidate_repo=str(repo),
            run_ids=["r1"],
            correctness="passed",
            baseline=[dict(status="completed", metric=100.0)],
            candidate=[dict(status="completed", metric=120.0)],
            distill={"status": "distilled", "unit_id": "u1"},
        ),
    )
    assert code_retry == 0
    assert res_retry["baseline"]["exp_id"] == "e2"
    persisted = json.loads(state.read_text(encoding="utf-8"))
    assert [r["exp_id"] for r in persisted["rounds"]] == ["e1", "e2"]
    assert [p["exp_id"] for p in persisted["pending_distill"]] == ["e2"]

    # 2. Superseding e2 with refuted in ledger must reconcile baseline back to e1 on next_step and complete
    live_now, _ = ledger.fold(ledger.read_ledger(journal))
    rec_e2 = dict(next(r for r in live_now if r["exp_id"] == "e2"))
    rec_e2.update(supersedes_exp_id="e2", verdict="refuted", outcome="oracle_failed")
    ledger.append(journal, rec_e2)
    assert loop.next_step(state)["baseline"]["exp_id"] == "e1"
    res_consult, code_consult = loop.complete(state, "consult", dict(mode="reuse", unit_refs=[]))
    assert code_consult == 0
    assert res_consult["baseline"]["exp_id"] == "e1"
    assert json.loads(state.read_text(encoding="utf-8"))["baseline"]["exp_id"] == "e1"

    # 3. Initializing from a ledger containing a profiled, refuted experiment and replaying its record must not crash
    state2 = tmp_path / "s2.json"
    journal2 = tmp_path / "l2.jsonl"
    loop.init(state2, task_id="t2", ledger_path=journal2, baseline_repo=repo, direction="maximize", repetitions=1)
    (repo / "k.py").write_text("x = 4\n", encoding="utf-8")
    loop.complete(
        state2,
        "record",
        dict(
            bottleneck_summary={"op": "matmul"},
            profile_ref="prof://1",
            exp_id="e_ref",
            hypothesis="h_ref",
            candidate_repo=str(repo),
            run_ids=["r_ref"],
            correctness="failed",
            baseline=[dict(status="completed", metric=100.0)],
            candidate=[dict(status="completed", metric=120.0)],
        ),
    )
    state3 = tmp_path / "s3.json"
    loop.init(state3, task_id="t2", ledger_path=journal2, baseline_repo=repo, direction="maximize", repetitions=1)
    res_replay, code_replay = loop.complete(
        state3,
        "record",
        dict(
            exp_id="e_ref",
            hypothesis="h_ref",
            candidate_repo=str(repo),
            run_ids=["r_ref"],
            correctness="failed",
            baseline=[dict(status="completed", metric=100.0)],
            candidate=[dict(status="completed", metric=120.0)],
            note="replayed refuted record",
        ),
    )
    assert code_replay == 0
    assert res_replay["status"] == "running"


def test_auto_promotion_outside_benchmark_layout_and_l4_disable_isolation(tmp_path, monkeypatch):
    premise = _unit("unit-promo-l1", "L1", "axiom", {"soc": ["Ascend910B"]},
                    body="UB 32B hardware premise for workflow alpha.", path="_common")
    workflow = _unit("unit-promo-l3", "L3", "workflow", {"soc": ["Ascend910B"]},
                     parents=[premise.id], body="Baseline workflow alpha.")
    mutator = _unit("unit-promo-l4", "L4", "mutator", {"soc": ["Ascend910B"]},
                    body="Mutator workflow alpha.")
    index = RoutingIndex([premise, workflow, mutator])
    conds = {"soc": "Ascend910B"}

    ws = tmp_path / "normal_project"
    logs_dir = ws / "logs"
    logs_dir.mkdir(parents=True)
    monkeypatch.chdir(ws)
    monkeypatch.delenv("MINDIE_BENCH_WORKSPACE", raising=False)
    monkeypatch.delenv("MINDIE_DISABLE_RSI_EXPLORE", raising=False)

    # First reuse query in a normal project without any sibling profile/ directory
    first = index.query("workflow alpha", conditions=conds, mode="reuse", token_budget=8000)
    assert first["mode"] == "reuse"
    assert (logs_dir / "knowledge_query_audit.jsonl").is_file()

    # Duplicate candidate_hash across 3 run_ids must not promote
    ledger_file = logs_dir / "experiment-ledger.jsonl"
    dup_rows = [
        {"run_id": f"r-{i}", "verdict": "refuted", "outcome": "no_improvement", "candidate_hash": "same-sha"}
        for i in range(3)
    ]
    ledger_file.write_text("\n".join(json.dumps(r) for r in dup_rows) + "\n", encoding="utf-8")
    still_reuse = index.query("workflow alpha", conditions=conds, mode="reuse", token_budget=8000)
    assert still_reuse["mode"] == "reuse"

    # Two legacy verdict-only records must not promote
    legacy_rows = [
        {"run_id": "leg-1", "verdict": "unsupported"},
        {"run_id": "leg-2", "verdict": "unsupported"},
    ]
    ledger_file.write_text("\n".join(json.dumps(r) for r in legacy_rows) + "\n", encoding="utf-8")
    assert index.query("workflow alpha", conditions=conds, mode="reuse", token_budget=8000)["mode"] == "reuse"

    # Three distinct refuted candidates promote reuse -> explore even across a fresh RoutingIndex
    distinct_rows = [
        {"run_id": f"d-{i}", "verdict": "refuted", "outcome": "no_improvement", "candidate_hash": f"sha-{i}"}
        for i in range(3)
    ]
    ledger_file.write_text("\n".join(json.dumps(r) for r in distinct_rows) + "\n", encoding="utf-8")
    fresh_index = RoutingIndex([premise, workflow, mutator])
    promoted = fresh_index.query("workflow alpha", conditions=conds, mode="reuse", token_budget=16000)
    assert promoted["mode"] == "explore"
    assert mutator.id in {r["id"] for r in promoted["results"]}

    # MINDIE_DISABLE_RSI_EXPLORE=1 must exclude L4 even in an L4-only corpus
    monkeypatch.setenv("MINDIE_DISABLE_RSI_EXPLORE", "1")
    l4_only = RoutingIndex([mutator])
    disabled_res = l4_only.query("workflow alpha", conditions=conds, mode="reuse", token_budget=8000)
    assert disabled_res["results"] == []
    assert disabled_res["cards"] == []



