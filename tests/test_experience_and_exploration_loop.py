"""Two-task experience replay, 4-stage usage events, growth, and exploration axes."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys

import pytest

from graded_fixture import OBSERVED, unit as fixture_unit
from mindie_knowledge.graded.distill import (
    UNIT_BODY_HARD_BUDGET_BYTES,
    apply_decision,
    noop_decision,
    prepare_request,
    validate_decision,
    value_gate,
)
from mindie_knowledge.graded.explore import (
    assess_stagnation,
    build_exploration_context,
    evaluate_exploration_candidate,
)
from mindie_knowledge.graded.feedback import (
    record_usage_event,
    record_vote,
    summarize_usage_events,
)
from mindie_knowledge.graded.index import RoutingIndex, response_token_estimate, token_estimate
from mindie_knowledge.graded.maintenance import growth_metrics
from mindie_knowledge.graded.publication import prepare_decision
from mindie_knowledge.graded.units import GradedUnit
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.materials.references import ReadReferenceError

ROOT = "a" * 64
DOMAIN = "vllm-ascend"


def scanner():
    matches = list((Path.home() / ".cache/mindie-agent/gitleaks/8.30.1").glob("*/gitleaks"))
    if not matches:
        pytest.skip("installed pinned scanner not present; no download in component tests")
    return str(matches[0])


def ref(item, revision=None):
    revision = item.revision if revision is None else revision
    return f"mindie://{DOMAIN}/units/{item.id}@{revision}"


def evidence(item, *, outcome="verified", refs=None):
    return {"kind": "authorized_transcript", "outcome": outcome,
            "source_locator": item.metadata["provenance"]["source_locator"],
            "content": item.body.strip(), "evidence_refs": refs or ["fixture-observation"],
            "repetitions": 1}


def proposal(session_digest, item, *, operation="create", body=None):
    metadata = {key: value for key, value in item.metadata.items() if key != "revision"}
    return {"schema_version": "mindie-distill-decision/1", "session_digest": session_digest,
            "evidence_inputs": ["authorized_transcript"], "action": "propose_graded_units",
            "drop_reason": None,
            "units": [{"target_unit_id": item.id, "target_tier": metadata["tier"],
                       "category": metadata["category"],
                       "evidence_strength": metadata["evidence_strength"],
                       "domain_path": metadata["domain_path"], "operation": operation,
                       "frontmatter": metadata, "markdown_body": body or item.body}]}


def test_verified_in_flight_milestone_and_foreground_proposal_apply_without_model(store):
    item = fixture_unit()
    engine = Engine(store, redactor_executable=scanner())
    session = "a" * 64
    result = engine.distill_selected([evidence(item)], session_digest=session,
                                     task_state="verified_milestone", proposal=proposal(session, item))
    assert result["status"] == "applied"
    assert result["admitted_unit_ids"] == [item.id]
    assert result["model_calls"] == 0
    assert result["model_execution"] == "foreground_proposal"
    assert result["model_status"] == "not_invoked"
    explained = store.explain(ref(item, 1))
    assert explained["content"] == item.body
    assert explained["revision"] == 1


def test_value_gate_admits_verified_milestone_and_blocks_in_flight(store):
    item = fixture_unit()
    observed = prepare_request([evidence(item, outcome="observed")], session_digest="a" * 64,
                               task_state="in_flight", redactor_executable=scanner(),
                               redaction_key=store.redaction_key())
    assert value_gate(observed) == "in_flight_incomplete"
    verified = prepare_request([evidence(item)], session_digest="a" * 64,
                               task_state="verified_milestone", redactor_executable=scanner(),
                               redaction_key=store.redaction_key())
    assert value_gate(verified) is None
    all_pending = prepare_request([evidence(item, outcome="in_flight")], session_digest="a" * 64,
                                  task_state="verified_milestone", redactor_executable=scanner(),
                                  redaction_key=store.redaction_key())
    assert value_gate(all_pending) == "in_flight_incomplete"


def test_in_flight_session_distills_verified_milestone_but_blocks_unverified_inputs(store):
    item = fixture_unit()
    session = "f" * 64
    pending = {
        "kind": "authorized_transcript",
        "outcome": "in_flight",
        "source_locator": "fixture@abcdef1:pending.md",
        "content": "Only the verified milestone may become evidence; this probe is still running.",
        "evidence_refs": ["pending-run"],
        "repetitions": 1,
    }
    only_pending = prepare_request([pending], session_digest=session, task_state="in_flight",
                                   redactor_executable=scanner(), redaction_key=store.redaction_key())
    assert value_gate(only_pending) == "in_flight_incomplete"
    assert noop_decision(only_pending)["drop_reason"] == "in_flight_incomplete"

    mixed = prepare_request([evidence(item), pending], session_digest=session, task_state="in_flight",
                            redactor_executable=scanner(), redaction_key=store.redaction_key())
    assert value_gate(mixed) is None
    applied = apply_decision(store, mixed, proposal(session, item))
    assert applied["status"] == "applied"
    assert applied["admitted_unit_ids"] == [item.id]
    assert store.explain(ref(item, 1))["content"] == item.body

    cites_pending_source = proposal(session, item)
    row = cites_pending_source["units"][0]
    row["frontmatter"]["provenance"] = {
        **row["frontmatter"]["provenance"],
        "source_locator": pending["source_locator"],
        "evidence_refs": ["pending-run"],
    }
    with pytest.raises(ValueError, match="selected evidence"):
        validate_decision(cites_pending_source, mixed)

    cites_pending_ref = proposal(session, item)
    cites_pending_ref["units"][0]["frontmatter"]["provenance"] = {
        **cites_pending_ref["units"][0]["frontmatter"]["provenance"],
        "evidence_refs": ["pending-run"],
    }
    with pytest.raises(ValueError, match="not present"):
        validate_decision(cites_pending_ref, mixed)


def test_two_task_replay_revises_and_deduplicates_usage_and_inconclusive_failure(store):
    item = fixture_unit()
    engine = Engine(store, redactor_executable=scanner())
    session_a = "a" * 64
    applied = engine.distill_selected([evidence(item)], session_digest=session_a,
                                      task_state="verified_milestone", proposal=proposal(session_a, item))
    assert applied["status"] == "applied"
    explained = store.explain(ref(item, 1))
    assert explained["revision"] == 1

    # Task B discovers, loads, declares applied, and records a refutation.
    assert record_usage_event(store, root_hash=ROOT, ref=ref(item, 1),
                              event_type="discovered", run_id="run-b1")["duplicate"] is False
    record_usage_event(store, root_hash=ROOT, ref=ref(item, 1),
                       event_type="loaded", run_id="run-b1")
    record_usage_event(store, root_hash=ROOT, ref=ref(item, 1), event_type="applied",
                       run_id="run-b1", step_id="step-3")
    refuted = record_usage_event(store, root_hash=ROOT, ref=ref(item, 1),
                                 event_type="evaluated", run_id="run-b1",
                                 outcome="refuted", reason="run-b1 regressed the decode path")
    assert refuted["outcome"] == "refuted" and refuted["duplicate"] is False
    summary = summarize_usage_events(store, item.id, 1)
    assert summary == {"unit_id": item.id, "revision": 1, "discovered": 1, "loaded": 1,
                       "applied": 1, "evaluated": 1, "supported": 0, "refuted": 1,
                       "inconclusive": 0}

    with pytest.raises(ValueError, match="run_id"):
        record_usage_event(store, root_hash=ROOT, ref=ref(item, 1),
                           event_type="evaluated", run_id="", outcome="refuted")
    record_usage_event(store, root_hash=ROOT, ref=ref(item, 1), event_type="evaluated",
                       run_id="run-b1-infra", outcome="inconclusive",
                       reason="device unavailable before measurement")
    duplicate = record_usage_event(store, root_hash=ROOT, ref=ref(item, 1),
                                   event_type="evaluated", run_id="run-b1",
                                   outcome="refuted")
    assert duplicate["duplicate"] is True
    summary = summarize_usage_events(store, item.id, 1)
    assert summary["evaluated"] == 2 and summary["refuted"] == 1
    assert summary["supported"] == 0 and summary["inconclusive"] == 1

    # Task B revises U@1 -> U@2 with a boundary that does not overlap triggers.
    revised = GradedUnit.from_parts(
        {**deepcopy(item.metadata),
         "provenance": {**deepcopy(item.metadata["provenance"]),
                        "evidence_refs": ["fixture-observation", "run-b1-experiment"]}},
        item.body + "Boundary from run-b1: decode gains did not transfer to the measured shape.\n",
        item.path,
    )
    session_b = "b" * 64
    task_b_input = evidence(item, refs=["fixture-observation", "run-b1-experiment"])
    result = engine.distill_selected([task_b_input], session_digest=session_b,
                                     task_state="completed",
                                     proposal=proposal(session_b, revised,
                                                       operation="merge_into_existing"))
    assert result["status"] == "applied" and result["admitted_unit_ids"] == [item.id]
    assert result["model_calls"] == 0
    updated = store.explain(ref(item, 2))
    assert updated["revision"] == 2 and "Boundary from run-b1" in updated["content"]
    with pytest.raises(ReadReferenceError, match="replaced"):
        store.explain(ref(item, 1))
    assert store.query("graph", conditions=OBSERVED)["results"][0]["revision"] == 2

    vote = record_vote(store, root_hash=ROOT, ref=ref(item, 2), rating="up", reason="",
                       publishable=False, event_type="applied", run_id="run-b1-vote")
    assert vote["rating"] == "up" and vote["usage_event"]["event_type"] == "applied"
    assert summarize_usage_events(store, item.id, 2)["applied"] == 1


def test_growth_metrics_are_per_milestone_and_none_without_milestones():
    old = fixture_unit("unit-growth-a", body="Older rule body.\n")
    current_a = fixture_unit("unit-growth-a", body="Older rule body plus a boundary.\n")
    current_b = fixture_unit("unit-growth-b", body="Second active rule.\n")
    archived = fixture_unit("unit-growth-c", body="Archived rule.\n", status="archived")
    superseded = fixture_unit("unit-growth-d", body="Superseded rule.\n",
                              status="superseded", superseded_by="unit-growth-b")
    current = [current_a, current_b, archived, superseded]
    metrics = growth_metrics(current, completed_milestones=2, previous_units=[old],
                             loaded_tokens_delta=42)
    assert metrics["active_units"] == 2
    assert metrics["archived_units"] == 1 and metrics["superseded_units"] == 1
    assert metrics["delta_active_units"] == 1
    assert metrics["g_units"] == 0.5
    assert metrics["g_read"] == 42
    net_body = sum(token_estimate(unit.body) for unit in current) - token_estimate(old.body)
    assert net_body > 0 and metrics["g_body"] == net_body / 2
    unmeasured = growth_metrics(current, completed_milestones=0, previous_units=[old])
    assert unmeasured["g_units"] is None and unmeasured["g_body"] is None


def test_exploration_judgment_keeps_valid_subtarget_gain_and_separates_axes():
    common = dict(run_id="run-e1", candidate_id="cand-tile", conditions={"soc": "Ascend910B"},
                  mutation_summary="tile the reduction", compile_ok=True, oracle_passed=True,
                  measurement_passed=True, min_speedup_ratio=1.05, noise_margin=0.01)
    subtarget = evaluate_exploration_candidate(speedup_ratio=1.04, ablation_retained=False, **common)
    assert subtarget["axes"] == {"evidence_validity": "valid",
                                 "goal_attainment": "not_attained",
                                 "reusability": "reusable_observation"}
    assert subtarget["verdict"] == "subtarget_valid"
    assert subtarget["distill_ready"] is True
    assert subtarget["return_tier"] == "L2"
    assert subtarget["conditional_avoid"] == []

    negative = evaluate_exploration_candidate(speedup_ratio=0.95, ablation_retained=True, **common)
    assert negative["reusability"] == "reusable_negative_boundary"
    assert negative["verdict"] == "refuted" and negative["conditional_avoid"]
    noise = evaluate_exploration_candidate(speedup_ratio=1.005, ablation_retained=None, **common)
    assert noise["reusability"] == "inconclusive" and noise["distill_ready"] is False
    pending_ablation = evaluate_exploration_candidate(speedup_ratio=1.06, ablation_retained=None, **common)
    assert pending_ablation["axes"] == {"evidence_validity": "valid",
                                        "goal_attainment": "not_applicable",
                                        "reusability": "inconclusive"}
    assert pending_ablation["verdict"] == "inconclusive" and pending_ablation["distill_ready"] is False
    broken = evaluate_exploration_candidate(**{**common, "compile_ok": False}, speedup_ratio=1.04)
    assert broken["evidence_validity"] == "compile_failed"
    assert broken["goal_attainment"] == "not_applicable"
    assert broken["distill_ready"] is False


def test_stagnation_counts_information_not_infrastructure_and_deduplicates_runs():
    deduped = assess_stagnation([
        {"run_id": "run-1", "verdict": "unsupported"},
        {"run_id": "run-1", "verdict": "unsupported"},
        {"run_id": "run-2", "verdict": "supported"},
    ])
    assert deduped["status"] == "progressing" and deduped["stalled"] is False
    assert deduped["stalled_count"] == 0
    attained = assess_stagnation([
        {"run_id": "run-a1", "verdict": "attained"},
        {"run_id": "run-a2", "verdict": "attained"},
    ])
    assert attained["status"] == "progressing" and attained["stalled"] is False
    assert attained["stalled_count"] == 0
    repair = assess_stagnation([
        {"run_id": "run-3", "outcome": "infra_failed"},
        {"run_id": "run-4", "verdict": "inconclusive"},
    ])
    assert repair["status"] == "repair_execution_conditions"
    assert repair["stalled"] is False and repair["stalled_count"] == 0
    stalled = assess_stagnation([
        {"run_id": "run-5", "verdict": "unsupported"},
        {"run_id": "run-6", "verdict": "unsupported"},
    ])
    assert stalled["status"] == "stalled" and stalled["stalled"] is True
    assert stalled["stalled_count"] == 2


def test_exploration_context_strips_l3_and_only_mounts_valid_matching_refutations():
    units = [
        {"id": "unit-l1", "tier": "L1", "revision": 1, "body": "hard constraint"},
        {"id": "unit-l2", "category": "sop", "revision": 2, "body": "diagnostic steps"},
        {"id": "unit-l3", "category": "workflow", "revision": 1, "body": "old recommendation"},
        {"id": "unit-l4", "tier": "L4", "revision": 3, "body": "code transform"},
    ]
    ledger = [
        {"run_id": "run-1", "verdict": "refuted", "axes": {"evidence_validity": "valid"},
         "conditions": {"soc": "Ascend910B"}, "mutation_summary": "bad tile"},
        {"run_id": "run-2", "verdict": "refuted", "axes": {"evidence_validity": "valid"},
         "conditions": {"soc": "Ascend910C"}, "mutation_summary": "other chip"},
        {"run_id": "run-3", "verdict": "supported", "axes": {"evidence_validity": "valid"},
         "conditions": {"soc": "Ascend910B"}},
        {"run_id": "run-4", "verdict": "refuted", "axes": {"evidence_validity": "invalid"},
         "conditions": {"soc": "Ascend910B"}},
    ]
    context = build_exploration_context(
        goal="explain the decode gap", retrieved_units=units,
        bottleneck_summary={"layer": "attention"}, ledger_entries=ledger,
        conditions={"soc": "Ascend910B"}, native_subcontext=True,
    )
    assert context["isolation_mode"] == "native_subcontext"
    assert context["stripped_l3_ids"] == ["unit-l3"]
    assert {unit["id"] for unit in context["mounted_units"]} == {"unit-l1", "unit-l2", "unit-l4"}
    assert [item["run_id"] for item in context["conditional_avoid"]] == ["run-1"]
    assert context["conditional_avoid"][0]["avoid"] == "bad tile"


def test_query_forwards_include_archived_and_explain_forwards_batch_refs(store):
    active = fixture_unit("unit-archive-active")
    archived = fixture_unit("unit-archive-old", status="archived")
    store.graded.install([active, archived])
    default = store.query("graph", conditions=OBSERVED)
    assert archived.id not in {row["id"] for row in default["results"]}
    included = store.query("graph", conditions=OBSERVED, include_archived=True)
    assert archived.id in {row["id"] for row in included["results"]}
    batch = store.explain(refs=[ref(active), ref(archived)])
    assert {item["id"] for item in batch["units"]} == {active.id, archived.id}


def test_exploration_negative_boundary_stays_l2_and_non_speedup_goals_share_axes():
    common = dict(run_id="run-x", candidate_id="cand-x", conditions={"soc": "Ascend910B"},
                  mutation_summary="guarded candidate", compile_ok=True, oracle_passed=True,
                  measurement_passed=True)
    negative = evaluate_exploration_candidate(speedup_ratio=0.95, ablation_retained=True,
                                              target_tier="L4", **common)
    assert negative["reusability"] == "reusable_negative_boundary"
    assert negative["return_tier"] == "L2"

    correct = evaluate_exploration_candidate(goal_type="correctness", goal_met=True, **common)
    assert correct["axes"] == {"evidence_validity": "valid", "goal_attainment": "attained",
                               "reusability": "reusable_observation"}
    assert correct["verdict"] == "attained" and correct["return_tier"] == "L2"

    diagnose = evaluate_exploration_candidate(goal_type="diagnose", diagnostic_gain=True, **common)
    assert diagnose["goal_attainment"] == "attained"
    assert diagnose["verdict"] == "attained" and diagnose["return_tier"] == "L2"


def test_repository_outer_response_estimate_matches_usage(store):
    item = fixture_unit()
    store.graded.install([item])
    found = store.query("graph", conditions=OBSERVED)
    assert found["usage"]["estimated_tokens"] == response_token_estimate(found)
    assert found["response_token_estimate"] == response_token_estimate(found)

    closure = store.graded.explain(refs=[item.id], conditions=OBSERVED, token_budget=16000)
    assert closure["usage"]["estimated_tokens"] == response_token_estimate(closure)
    assert closure["response_token_estimate"] == response_token_estimate(closure)


def test_delivery_counts_and_evaluated_evidence_source_are_queryable(store):
    item = fixture_unit()
    store.graded.install([item])
    store.query("graph", conditions=OBSERVED, run_id="run-query")
    explained = store.explain(store.graded.ref(item.id), run_id="run-explain")
    record_usage_event(store, root_hash=ROOT, ref=explained["feedback_ref"],
                       event_type="evaluated", run_id="run-eval", outcome="supported",
                       evidence_source="run_manifest_verified")
    summary = summarize_usage_events(store, item.id, include_delivery=True)
    assert summary["query_deliveries"] >= 1
    assert summary["explain_deliveries"] >= 1
    assert summary["evidence_sources"]["run_manifest_verified"] == 1
    assert summarize_usage_events(store, item.id) == {
        "unit_id": item.id, "revision": None, "discovered": 0, "loaded": 0,
        "applied": 0, "evaluated": 1, "supported": 1, "refuted": 0,
        "inconclusive": 0,
    }


def test_default_related_lookup_is_snapshotted_for_idempotent_retry(store, tmp_path, monkeypatch):
    from mindie_knowledge.graded import worker
    existing = fixture_unit("unit-related-existing",
                            body="Graph capture fixture replay details for comparison.\n")
    store.graded.install([existing])
    calls = tmp_path / "calls.txt"
    monkeypatch.setenv("MINDIE_GRADED_FIXTURE_CALLS", str(calls))
    engine = Engine(store, redactor_executable=scanner(),
                    summary_command=[sys.executable, str(Path(__file__).parent / "fixtures/graded_worker.py")])
    captured = []
    original = worker.prepare_request

    def capture(inputs, **kwargs):
        captured.append(kwargs.get("related_units"))
        return original(inputs, **kwargs)

    monkeypatch.setattr(worker, "prepare_request", capture)
    selected = [evidence(fixture_unit())]
    first = engine.distill_selected(selected, session_digest="b" * 64,
                                    task_state="verified_milestone")
    assert first["status"] == "applied" and calls.read_text() == "1"
    retry = engine.distill_selected(selected, session_digest="b" * 64,
                                    task_state="verified_milestone")
    assert retry["replay"] == "existing_local_receipt" and calls.read_text() == "1"
    assert len(captured) == 2 and captured[0] == captured[1]
    assert [item["id"] for item in captured[0]] == [existing.id]


def test_related_units_feed_the_organizer_prompt_without_changing_old_digests(store):
    item = fixture_unit()
    request = prepare_request([evidence(item)], session_digest="a" * 64,
                              task_state="completed", redactor_executable=scanner(),
                              redaction_key=store.redaction_key())
    assert "related_units" not in request
    related = [{"id": item.id, "revision": 1, "tier": item.metadata["tier"],
                "category": item.metadata["category"], "title": item.metadata["title"],
                "conditions": item.metadata["conditions"], "triggers": item.metadata["triggers"],
                "body_excerpt": item.body[:100]}]
    with_related = prepare_request([evidence(item)], session_digest="a" * 64,
                                   task_state="completed", redactor_executable=scanner(),
                                   redaction_key=store.redaction_key(), related_units=related)
    assert with_related["related_units"] == related
    assert with_related["request_digest"] != request["request_digest"]
    from mindie_knowledge.graded.distill import build_prompt
    prompt = build_prompt(with_related)
    assert "related_units" in prompt and "merge_into_existing" in prompt


def test_related_lookup_keeps_late_counterevidence_and_recovery_conditions(store):
    from mindie_knowledge.graded.worker import _matching_related_units

    # A late correction must survive comparison even after a long procedure.
    procedure = (
        "Compare the same logical input and state at each producer/consumer boundary. "
        "Keep the run reference and distinguish a completed probe from a verified fix.\n"
    ) * 10
    correction = (
        "Correction: the instrumented run passed, but the uninstrumented run still failed. "
        "The earlier fix claim is withdrawn; the observer effect remains unresolved.\n"
    )
    existing = fixture_unit("unit-observer-effect", body=procedure + correction,
                            tier="L3", category="workflow")
    store.graded.install([existing])
    related = _matching_related_units(store, [], [existing.id])
    assert related[0]["body_complete"] is True
    assert related[0]["body_excerpt"] == existing.body


@pytest.mark.parametrize("legacy_snapshot", [False, True])
def test_incomplete_related_body_cannot_overwrite_existing_branches(store, legacy_snapshot):
    from mindie_knowledge.graded.distill import apply_decision
    from mindie_knowledge.graded.worker import RELATED_BODY_BUDGET_BYTES, _matching_related_units

    branch = "Preserve the initial state, request order, reset semantics and independent oracle.\n"
    existing = fixture_unit("unit-history-dependent-probe",
                            body=branch * (RELATED_BODY_BUDGET_BYTES // len(branch) + 1))
    store.graded.install([existing])
    related = _matching_related_units(store, [], [existing.id])
    assert related[0]["body_complete"] is False
    assert related[0]["body_excerpt"] == ""
    if legacy_snapshot:
        related[0].pop("body_complete")
        related[0]["body_excerpt"] = existing.body[:1200]
    session = "c" * 64
    selected = {**evidence(existing, outcome="observed"),
                "content": "A fresh process passed; the historical request sequence was not tested.",
                "source_locator": "fixture@abcdef1:history-observation.txt"}
    request = prepare_request([selected], session_digest=session,
                              task_state="verified_milestone", redactor_executable=scanner(),
                              redaction_key=store.redaction_key(), related_units=related)
    replacement = proposal(session, existing, operation="merge_into_existing",
                           body="Only retry from a fresh process.\n")
    replacement["units"][0]["frontmatter"]["provenance"].update(
        source_locator=selected["source_locator"],
        source_digest=request["sanitized_inputs"][0]["source_digest"])
    with pytest.raises(ValueError, match="complete related unit"):
        apply_decision(store, request, replacement)
    assert store.explain(ref(existing))["content"] == existing.body

    # The existing review route can retain a candidate without overwriting it.
    replacement["units"][0]["operation"] = "propose_semantic_merge_review"
    review = apply_decision(store, request, replacement)
    assert review["status"] == "review_or_unchanged"
    assert store.explain(ref(existing))["content"] == existing.body


def test_negative_branch_folding_keeps_one_compacted_l3_unit(store, tmp_path, monkeypatch):
    existing = fixture_unit(
        "unit-k3-a5-pipeline-fusion", tier="L3", category="workflow",
        title="K3/A5 pipeline fusion triage",
        body=(
            "## Active branches and rare conditions\n\n"
            "Keep double-buffered UB ping-pong when burst_skew=19 on decode_tp=8.\n\n"
            "Conditional counter-evidence: under dynamic_prefix_cache=true, disable static workspace reuse.\n\n"
            "Superseded plan: legacy split-K workspace layout was replaced by persistent tile staging.\n\n"
            "## Negative pipeline attempts\n\n"
            "PV2 on the K3 A5 decode path: the isolated PV2 schedule removed the staging copy, "
            "but the paired full-pipeline run regressed because the producer still materialized "
            "the attention result to GM and the consumer reloaded it; the branch was rejected.\n\n"
            "Score2 on the K3 A5 decode path: the score tile change reduced local cycles, yet the "
            "same GM round-trip remained and the critical path did not move; the branch was rejected.\n\n"
            "vmula on the K3 A5 decode path: the vector multiply-add helper was folded, but a "
            "collective barrier split the fused region and the extra synchronization erased the "
            "gain; the branch was rejected.\n"
        ),
    )
    store.graded.install([existing])
    calls = tmp_path / "c6_calls.txt"
    monkeypatch.setenv("MINDIE_GRADED_FIXTURE_CALLS", str(calls))
    monkeypatch.setenv(
        "MINDIE_GRADED_FIXTURE_SOURCE_TYPE",
        existing.metadata["provenance"]["source_type"],
    )
    monkeypatch.setenv(
        "MINDIE_GRADED_FIXTURE_STRENGTH",
        existing.metadata["evidence_strength"],
    )
    monkeypatch.setenv(
        "MINDIE_GRADED_FIXTURE_DOMAIN_PATH",
        existing.metadata["domain_path"],
    )
    engine = Engine(
        store,
        redactor_executable=scanner(),
        summary_command=[sys.executable, str(Path(__file__).parent / "fixtures/graded_worker.py")],
    )
    new_evidence = {
        "kind": "authorized_transcript",
        "outcome": "verified",
        "source_locator": existing.metadata["provenance"]["source_locator"],
        "content": (
            "## Active branches and rare conditions\n\n"
            "Keep double-buffered UB ping-pong when burst_skew=19 on decode_tp=8.\n\n"
            "New verified branch: enable fused QKV epilogue when head_dim=128 and seq_len>=2048.\n\n"
            "PipeV3 on the K3 A5 decode path: async cube-vector prefetch overlapped MTE2, "
            "but L0C accumulator contention stalled the consumer; the branch was rejected.\n"
        ),
        "evidence_refs": ["run-k3-a5-pipev3"],
        "repetitions": 2,
    }
    result = engine.distill_selected(
        [new_evidence],
        session_digest="d" * 64,
        task_state="completed",
        related_refs=[existing.id],
    )
    assert result["status"] == "applied"
    assert result["admitted_unit_ids"] == [existing.id]
    assert calls.read_text() == "1"
    plan = result["publication_plan"]
    assert plan["status"] == "proposed"
    assert len(plan["writes"]) == 1 and plan["writes"][0]["unit_id"] == existing.id
    revised_body = plan["writes"][0]["markdown_body"]
    assert "## Verified exclusion branches" in revised_body
    assert "## Negative pipeline attempts" not in revised_body
    assert all(name in revised_body for name in ("PV2", "Score2", "vmula", "PipeV3"))
    assert "burst_skew=19" in revised_body
    assert "Conditional counter-evidence: under dynamic_prefix_cache=true" in revised_body
    assert "Superseded plan: legacy split-K workspace layout" in revised_body
    assert "New verified branch: enable fused QKV epilogue" in revised_body
    assert plan["anti_bloat"]["active_unit_delta"] == 0
    assert plan["anti_bloat"]["net_bytes_delta"] < 0
    assert plan["anti_bloat"]["max_unit_body_bytes"] == len(revised_body.encode("utf-8"))
    assert plan["anti_bloat"]["compacted_within_budget"] is True
    assert plan["anti_bloat"]["conditions_retained"] is True
    assert plan["anti_bloat"]["evidence_refs_retained"] is True
    assert plan["anti_bloat"]["triggers_retained"] is True
    explained = store.explain(ref(existing, 2))
    assert explained["revision"] == 2
    assert explained["content"] == revised_body


def test_bloated_unit_body_is_rejected_before_admission(store):
    item = fixture_unit()
    session = "e" * 64
    request = prepare_request([evidence(item)], session_digest=session,
                              task_state="verified_milestone", redactor_executable=scanner(),
                              redaction_key=store.redaction_key())
    bloated = proposal(session, item, body="x" * (UNIT_BODY_HARD_BUDGET_BYTES + 1))

    with pytest.raises(ValueError, match="8 KiB anti-bloat budget"):
        validate_decision(bloated, request)


def test_pareto_candidate_selection_filters_dominated_below_floor_and_infra_errors():
    from mindie_knowledge.graded.explore import select_pareto_candidates

    candidates = [
        {"id": "h0_vanilla", "pass_rate": 0.70, "cost": 0.89},
        {"id": "rho_heavy_expensive", "pass_rate": 0.74, "cost": 1.09},
        {"id": "hp_lite", "pass_rate": 0.70, "cost": 0.40, "pass_count": 42, "total_cost": 24.0},
        {"id": "hp_medium", "pass_rate": 0.74, "cost": 0.76, "pass_count": 45, "total_cost": 45.6},
        {"id": "hp_heavy", "pass_rate": 0.80, "cost": 1.00, "pass_count": 48, "total_cost": 60.0},
        {"id": "below_floor", "pass_rate": 0.62, "cost": 0.35},
        {"id": "infra_timeout", "pass_rate": 0.0, "cost": 0.10, "status": "infra_error"},
    ]
    selected = select_pareto_candidates(candidates, pass_floor=0.70)
    assert selected["below_floor_ids"] == ["below_floor"]
    assert selected["inconclusive_ids"] == ["infra_timeout"]
    assert "h0_vanilla" in selected["dominated_ids"]
    assert "rho_heavy_expensive" in selected["dominated_ids"]
    assert selected["frontier_ids"] == ["hp_lite", "hp_medium", "hp_heavy"]
    assert selected["representatives"]["lite"]["id"] == "hp_lite"
    assert selected["representatives"]["medium"]["id"] == "hp_medium"
    assert selected["representatives"]["heavy"]["id"] == "hp_heavy"
    assert selected["representatives"]["heavy"]["cost_of_pass"] == pytest.approx(60.0 / 48, rel=1e-5)


def test_stage_budget_guidance_and_weak_oracle_direction_gate():
    from mindie_knowledge.graded.explore import enforce_direction_budget, stage_budget_guidance

    eco = stage_budget_guidance(14)
    assert eco["stage"] == "economy"
    assert eco["should_deny"] is False
    assert eco["injection_target"] == "tool_result_tail"
    assert eco["preserves_prompt_prefix_cache"] is True
    assert "Economy stage" in eco["additional_context"]

    conv = stage_budget_guidance(26)
    assert conv["stage"] == "convergence"
    assert "Convergence stage" in conv["additional_context"]

    interactive_over = stage_budget_guidance(45, allow_hard_deny=False)
    assert interactive_over["should_deny"] is False

    unattended_over = stage_budget_guidance(45, allow_hard_deny=True)
    assert unattended_over["should_deny"] is True
    assert unattended_over["stage"] == "exhausted_denied"

    budgeted = enforce_direction_budget(
        [
            {"direction_id": "d1", "hypothesis": "deep search on full 4-node e2e launch", "requires_deep_iteration": True, "learned_refs": ["u1"]},
            {"direction_id": "d2", "hypothesis": "local single-op probe", "requires_deep_iteration": False, "learned_refs": []},
        ],
        oracle_strength="weak_e2e",
        probe_cost_fraction=0.40,
    )
    assert budgeted["downgraded_weak_oracle_ids"] == ["d1"]
    assert budgeted["directions"][0]["weak_oracle_downgraded"] is True
    assert budgeted["directions"][0]["effective_profile"] == "standard_build_local_probe_first"
