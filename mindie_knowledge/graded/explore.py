"""[Role] Model-free RSI stagnation, de-biased context, and 4-gate judgment.

[Invariants] Information stagnation is distinct from execution failure: an
infrastructure or measurement fault requests repair and never counts as an
unsupported attempt. L3 workflow bodies are stripped, but any L1/L2 foundation
they carry is promoted before stripping. Conditional avoidance needs valid
refuted evidence and remains scoped to its recorded conditions. Evidence
validity, goal attainment, and reusable encoding are orthogonal. Native run and
event identities deduplicate attempts with the latest update winning.
[Timeouts & Locks] Pure in-memory validation and arithmetic; no subprocess,
network, model call, sleep, or lock is introduced.
[Side-Effect Policy] Returns derived judgments only; never writes knowledge,
runs experiments, or changes a unit revision or conditions.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .conditions import match_conditions
from .explore_support import (
    build_citation_feedback_record,
    enforce_direction_budget,
    flagged,
    guard_regression,
    legacy_evaluate,
    select_pareto_candidates,
    stage_budget_guidance,
)
from .units import CATEGORY_TO_TIER

CONTEXT_SCHEMA = "mindie-exploration-context/1"
EVALUATION_SCHEMA = "mindie-exploration-evaluation/1"
PACK_SCHEMA = "mindie-rsi-subagent-pack/1"
SCOPE_NOTE = ("Negative result under recorded conditions only; not a universal ban "
              "when conditions or bottleneck change.")
TIERS = {"L1", "L2", "L3", "L4"}
REPAIR_OUTCOMES = {"infra_failed", "measurement_invalid"}
PROGRESS_VERDICTS = {"supported", "attained", "subtarget_valid", "improved",
                     "qualified_improvement", "distill_candidate"}
NEGATIVE_VERDICTS = {"refuted", "failed", "negative", "rejected", "unsupported",
                     "record_conditional_finding"}


def _mapping(value: Any, label: str) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    return value.strip()


def _number(value: Any, label: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _tier_of(unit: Mapping[str, Any]) -> str | None:
    tier = unit.get("tier")
    return tier if tier in TIERS else CATEGORY_TO_TIER.get(unit.get("category"))


def _missing(status) -> list[str]:
    return [reason.split(":", 1)[0] for reason in status.reasons
            if reason.endswith(("not supplied", "version not supplied"))]


def assess_stagnation(ledger_entries: Sequence[Mapping[str, Any]], *,
                      consecutive_limit: int = 3,
                      max_stalled_attempts: int | None = None) -> dict[str, Any]:
    """Deduplicate ledger attempts by run_id (latest wins) and count non-progress."""
    limit = consecutive_limit if max_stalled_attempts is None else max_stalled_attempts
    if type(limit) is not int or limit < 1:
        raise ValueError("consecutive_limit must be a positive integer")
    unique: list[dict] = []
    positions: dict[str, int] = {}
    for index, raw in enumerate(ledger_entries or ()):
        entry = _mapping(raw, f"ledger_entries[{index}]")
        run_id = entry.get("run_id")
        key = run_id.strip() if isinstance(run_id, str) and run_id.strip() else f"attempt:{index}"
        if key in positions:
            unique[positions[key]] = entry
        else:
            positions[key] = len(unique)
            unique.append(entry)
    stalled_count, repair, progressed, budget_exhausted, unverified = 0, False, False, False, 0
    for attempt in unique:
        verdict, outcome = attempt.get("verdict"), attempt.get("outcome")
        axes = attempt.get("axes")
        attainment = (axes.get("goal_attainment") if isinstance(axes, Mapping) else None) \
            or attempt.get("goal_attainment")
        if attempt.get("budget_exhausted") is True:
            budget_exhausted = True
        if attempt.get("hypothesis") and not (attempt.get("hypothesis_verified") is True
                                              or verdict in PROGRESS_VERDICTS):
            unverified += 1
        if outcome in REPAIR_OUTCOMES or (verdict == "inconclusive"
                                          and outcome != "speedup_within_noise"):
            repair = True
        elif (verdict in PROGRESS_VERDICTS or attainment == "attained"
              or attempt.get("new_observation") is True
              or attempt.get("divergent_layer_identified") is True
              or attempt.get("improvement") is True
              or (isinstance(axes, Mapping) and axes.get("goal_achieved") is True)):
            stalled_count, progressed = 0, True
        else:
            stalled_count += 1
    # Two consecutive legacy "unsupported" verdicts remain the historical
    # threshold; richer ledger entries use the requested three-attempt window.
    legacy = bool(unique) and all(
        "verdict" in entry and "outcome" not in entry
        and "budget_exhausted" not in entry and "hypothesis" not in entry for entry in unique)
    threshold = min(limit, 2) if legacy and max_stalled_attempts is None else limit
    if stalled_count >= threshold:
        status, stalled = "stalled", True
        reason = "consecutive_unimproved_limit_reached"
    elif repair and not stalled_count:
        status, stalled = "repair_execution_conditions", False
        reason = "execution conditions are invalid; repair them before judging information stagnation"
    elif not unique:
        status, stalled = "no_attempts", False
        reason = "no attempts were supplied"
    elif stalled_count:
        status, stalled = "monitoring", False
        reason = f"{stalled_count} unproductive attempt(s); below the {threshold} reminder threshold"
    elif progressed:
        status, stalled = "progressing", False
        reason = "a new observation, supported verdict, or identified divergent layer is progress"
    else:
        status, stalled = "monitoring", False
        reason = "no decisive information yet"
    if repair and status in {"progressing", "monitoring"}:
        reason += "; some attempts require repaired execution conditions"
    return {"stalled": stalled, "stalled_count": stalled_count, "consecutive_limit": limit,
            "status": status, "reason": reason,
            "tail_run_ids": [entry["run_id"] for entry in unique[-limit:]
                             if isinstance(entry.get("run_id"), str)],
            "unverified_hypotheses_remaining": unverified, "budget_exhausted": budget_exhausted,
            "should_escalate": bool(stalled), "escalation_reason": reason}


def _compact_unit(unit: dict) -> dict | None:
    tier = _tier_of(unit)
    if tier == "L3":
        return None
    if tier not in {"L1", "L2", "L4"}:
        return None
    ident = unit.get("id")
    if not isinstance(ident, str) or not ident:
        raise ValueError("retrieved unit requires a stable id")
    return {"id": ident, "tier": tier, "revision": unit.get("revision"),
            "category": unit.get("category"), "title": unit.get("title"),
            "conditions": unit.get("conditions"), "ref": unit.get("ref"),
            "body": unit.get("body") or unit.get("content") or unit.get("excerpt") or ""}


def _promoted_foundations(units: Sequence[Mapping[str, Any]]) -> dict[str, dict]:
    """Collect L1/L2 foundations carried by L3 units before the L3 strip."""
    promoted: dict[str, dict] = {}
    by_id = {unit.get("id"): unit for unit in units if isinstance(unit.get("id"), str)}

    def visit(unit: Mapping[str, Any], depth: int = 0) -> None:
        if depth > 4 or _tier_of(unit) != "L3":
            return
        for key in ("inherited_rules", "foundation", "requires_units", "requires"):
            rules = unit.get(key)
            if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
                continue
            for rule in rules:
                rule = by_id.get(rule) if isinstance(rule, str) else rule
                if not isinstance(rule, Mapping):
                    continue
                compact = _compact_unit(dict(rule))
                if compact is not None and compact["tier"] != "L3":
                    promoted.setdefault(compact["id"], compact)
                    visit(compact, depth + 1)

    for raw in units:
        visit(raw)
    return promoted


def _normalized_avoid(entries: Sequence[Mapping[str, Any]], *, conditions, bottleneck) -> list[dict]:
    avoid = []
    for index, raw in enumerate(entries):
        entry = _mapping(raw, f"failed_attempts[{index}]")
        verdict, outcome, axes = entry.get("verdict"), entry.get("outcome"), entry.get("axes")
        valid = (entry.get("evidence_valid") is not False and (
            not isinstance(axes, Mapping) or axes.get("evidence_validity") in (None, "valid")))
        negative = (verdict in NEGATIVE_VERDICTS or outcome in NEGATIVE_VERDICTS
                    or (verdict == "inconclusive" and entry.get("budget_exhausted") is True))
        if not valid or not negative:
            continue
        required = entry.get("conditions")
        if required is None and isinstance(entry.get("applicability"), Mapping):
            required = entry["applicability"].get("conditions")
        if not isinstance(required, Mapping) or not required:
            required = dict(bottleneck or {})
        if conditions is not None and required and match_conditions(
                dict(required), dict(conditions)).status != "matched":
            continue
        mutation = entry.get("mutation_summary") or entry.get("candidate_id") or entry.get("unit_id") or ""
        avoid.append({
            "unit_id": entry.get("unit_id") or entry.get("candidate_id") or entry.get("id") or "",
            "hypothesis": entry.get("hypothesis") or mutation,
            "failure_reason": entry.get("failure_reason") or entry.get("reason") or verdict or outcome or "",
            "observed_bottleneck": entry.get("observed_bottleneck") or entry.get("bottleneck")
            or dict(bottleneck or {}),
            "conditions": dict(required), "scope_note": SCOPE_NOTE,
            "run_id": entry.get("run_id"), "avoid": mutation,
            "reason": entry.get("reason") or entry.get("failure_reason") or ""})
    avoid.sort(key=lambda item: (str(item["run_id"]), str(item["avoid"])))
    return avoid


def _prompt_brief(goal: str, foundations, bottleneck, mutators, avoid) -> str:
    lines = ["# RSI subagent brief", f"**Goal:** {goal or '(not supplied)'}", "",
             "## L1 hard constraints"]
    lines.extend(f"- {unit['id']}: {unit.get('title') or unit.get('body', '')[:120]}"
                 for unit in foundations) if foundations else lines.append("- none mounted")
    lines += ["", "## L2 bottleneck evidence", json.dumps(bottleneck or {}, sort_keys=True)[:600],
              "", "## L4 allowed mutators"]
    lines.extend(f"- {unit['id']}: {unit.get('title') or unit.get('body', '')[:120]}"
                 for unit in mutators) if mutators else lines.append("- none mounted")
    lines += ["", "## Conditional avoid"]
    lines.extend(f"- {item['unit_id'] or item['avoid']}: {item['failure_reason']} "
                 f"(scope: {item['scope_note']})" for item in avoid) if avoid else lines.append("- none")
    lines += ["", "Do not replay prior L3 workflow steps; derive the next experiment "
              "from the mounted L1/L2 facts and L4 mutators only."]
    return "\n".join(lines)[:1199]


def build_exploration_context(units=None, *, bottleneck_summary=None, failed_attempts=None,
                              native_subcontext: bool = False, goal: str = "",
                              retrieved_units=None, ledger_entries=None,
                              conditions=None) -> dict[str, Any]:
    """Assemble a self-contained fact pack for a fresh RSI subcontext."""
    if units is not None and retrieved_units is not None:
        raise ValueError("supply units or retrieved_units, not both")
    raw = units if units is not None else retrieved_units
    raw_units = [_mapping(item, f"units[{index}]") for index, item in enumerate(raw or ())]
    goal = "" if goal is None else str(goal)
    bottleneck = _mapping(bottleneck_summary, "bottleneck_summary") \
        if bottleneck_summary is not None else {}
    current = _mapping(conditions, "conditions") if conditions is not None else None
    entries = [*list(failed_attempts or ()), *list(ledger_entries or ())]
    promoted = _promoted_foundations(raw_units)
    mounted, stripped = [], []
    for raw_unit in raw_units:
        tier = _tier_of(raw_unit)
        if tier == "L3":
            ident = raw_unit.get("id")
            if not isinstance(ident, str) or not ident:
                raise ValueError("retrieved L3 unit requires a stable id")
            stripped.append(ident)
        else:
            compact = _compact_unit(raw_unit)
            if compact is not None:
                mounted.append(compact)
    mounted_ids = {unit["id"] for unit in mounted}
    mounted.extend(unit for ident, unit in sorted(promoted.items()) if ident not in mounted_ids)
    mounted.sort(key=lambda item: (item["id"], item.get("revision") or 0))
    stripped = sorted(set(stripped))
    avoid = _normalized_avoid(entries, conditions=current, bottleneck=bottleneck)
    ledger_refs = sorted(({"run_id": _mapping(entry, "ledger_entry").get("run_id"),
                           "verdict": _mapping(entry, "ledger_entry").get("verdict")}
                          for entry in entries),
                         key=lambda item: (str(item["run_id"]), str(item["verdict"])))
    l1 = [unit for unit in mounted if unit["tier"] == "L1"]
    l4 = [unit for unit in mounted if unit["tier"] == "L4"]
    isolation = "native_subcontext" if native_subcontext else "in_session_warning"
    budget_policy = {
        "kb_dir_cap": 1,
        "require_cold_direction": True,
        "roofline_status": str(bottleneck.get("roofline_status") or "calibrated"),
    }
    pack = {"schema_version": PACK_SCHEMA, "goal": goal, "isolation_required": True,
            "isolation_mode": isolation, "mounted_tiers": ["L1", "L2", "L4"],
            "stripped_tiers": ["L3"], "stripped_l3_ids": stripped,
            "l1_foundation_ids": [unit["id"] for unit in l1],
            "l2_bottleneck": bottleneck, "l4_mutator_ids": [unit["id"] for unit in l4],
            "conditional_avoid": avoid, "direction_budget_policy": budget_policy,
            "prompt_brief": _prompt_brief(goal, l1, bottleneck, l4, avoid)}
    return {"schema": CONTEXT_SCHEMA, "schema_version": CONTEXT_SCHEMA, "mode": "explore",
            "goal": goal, "isolation_mode": isolation, "native_subcontext": bool(native_subcontext),
            "requires_fresh_subcontext": not bool(native_subcontext),
            "mounted_units": mounted, "units": mounted,
            "mounted_tiers": ["L1", "L2", "L4"], "stripped_tiers": ["L3"],
            "stripped_l3_ids": stripped, "l1_foundation": l1, "l2_bottleneck": bottleneck,
            "l4_mutators": l4, "conditional_avoid": avoid, "bottleneck_summary": bottleneck,
            "conditions": current or {}, "ledger_refs": ledger_refs,
            "direction_budget_policy": budget_policy, "subagent_fact_pack": pack,
            "notes": ["Retrieved L3 recommendation steps are withheld to reduce anchoring.",
                      "L1/L2 foundations carried by stripped L3 units are promoted before stripping.",
                      "Deleting an L3 key does not remove visible history; only native_subcontext claims isolation.",
                      "Conditional avoidance is conditional evidence, not a permanent ban."]}


def _gates(candidate, target):
    compile_ok = bool(candidate.get("compile_ok", candidate.get("compile",
                     candidate.get("compiled", candidate.get("syntax_ok", True)))))
    parity = candidate.get("correctness_ok", candidate.get("parity_ok", candidate.get(
        "numerical_parity", candidate.get("oracle_passed", True))))
    hidden_bad = ("hidden_input_summary" in candidate
                  and flagged(candidate.get("hidden_input_summary"), "opt_regression"))
    speedup = candidate.get("speedup", candidate.get("profiler_speedup",
               candidate.get("speedup_ratio")))
    if speedup is None and isinstance(candidate.get("measurement"), Mapping):
        speedup = candidate["measurement"].get("speedup")
    speedup = _number(speedup, "speedup", optional=True)
    ablation = candidate.get("ablation_ok", candidate.get("ablation_passed",
               candidate.get("ablation_retained", True)))
    hygiene = candidate.get("hygiene_ok", candidate.get("hygiene_passed", True))
    workspace = candidate.get("workspace_delta_bytes")
    workspace_ok = workspace is None or (
        isinstance(workspace, int) and not isinstance(workspace, bool)
        and workspace <= candidate.get("_max_workspace_delta_bytes", 4096))
    guard_ok = (not guard_regression(candidate.get("guard_workloads"))
                if "guard_workloads" in candidate else True)
    scope_ok = not (candidate.get("require_reusable_scope")
                    and candidate.get("reusable_scope") is False)
    checks = {
        "G0": (compile_ok, "compile/syntax"),
        "G1": (bool(parity) and not hidden_bad, "numerical parity and hidden retention"),
        "G2": (speedup is not None and speedup >= target, "profiler target speedup"),
        "G3": (ablation is not False and hygiene is not False and workspace_ok and guard_ok
               and scope_ok, "ablation, hygiene, workspace and guard workload")}
    gates = {name: {"passed": passed, "ok": passed,
                    "status": "passed" if passed else "failed", "check": label}
             for name, (passed, label) in checks.items()}
    reached = next((name for name in ("G0", "G1", "G2", "G3") if not checks[name][0]), "PASSED")
    outcome = {"G0": "compile_failed", "G1": "correctness_failed",
               "G2": "sub_target_valid_evidence", "G3": "ablation_or_hygiene_failed"}.get(
                   reached, "qualified_improvement")
    return {"gates": gates, "gate_reached": reached, "outcome": outcome,
            "compile_ok": compile_ok, "correctness_ok": bool(parity),
            "speedup": speedup, "target_speedup": target,
            "ablation_ok": ablation is not False, "workspace_delta_bytes": workspace,
            "guard_workloads_ok": guard_ok}


def _evaluate_candidate(candidate: Mapping[str, Any], *, target_speedup: float,
                        require_reusable_scope: bool, preserve_subtarget_evidence: bool,
                        max_workspace_delta_bytes: int = 4096) -> dict[str, Any]:
    if isinstance(target_speedup, bool) or not isinstance(target_speedup, (int, float)) \
            or not math.isfinite(target_speedup) or target_speedup <= 0:
        raise ValueError("target_speedup must be a positive finite number")
    values = dict(candidate)
    values["_max_workspace_delta_bytes"] = max_workspace_delta_bytes
    values["require_reusable_scope"] = require_reusable_scope
    gate = _gates(values, float(target_speedup))
    evidence_valid = (gate["gates"]["G0"]["passed"] and gate["gates"]["G1"]["passed"]
                      and values.get("evidence_valid") is not False)
    goal_achieved = gate["gates"]["G2"]["passed"] and values.get("goal_achieved") is not False
    reusable = gate["gates"]["G3"]["passed"] and values.get("reusable_encoding") is not False
    if not evidence_valid:
        verdict, tier = "reject_invalid_evidence", values.get("target_tier", "L2")
    elif not goal_achieved:
        verdict, tier = "record_conditional_finding", "L2"
    elif not reusable:
        verdict, tier = "retain_verified_evidence", values.get("target_tier", "L4")
    else:
        verdict, tier = "distill_candidate", values.get("target_tier", "L4")
    if not preserve_subtarget_evidence and verdict == "record_conditional_finding":
        verdict = "reject_invalid_evidence"
    avoid = []
    if verdict == "record_conditional_finding":
        avoid = [{"unit_id": values.get("unit_id") or values.get("candidate_id") or "",
                  "hypothesis": values.get("hypothesis") or values.get("mutation_summary") or "",
                  "failure_reason": gate["outcome"],
                  "observed_bottleneck": values.get("observed_bottleneck")
                  or values.get("bottleneck") or {},
                  "conditions": dict(values.get("conditions") or {}), "scope_note": SCOPE_NOTE}]
    axes = {"evidence_valid": bool(evidence_valid), "goal_achieved": bool(goal_achieved),
            "reusable_encoding": bool(reusable)}
    return {"schema": EVALUATION_SCHEMA, "run_id": values.get("run_id"),
            "candidate_id": values.get("candidate_id") or values.get("unit_id"),
            "mutation_summary": values.get("mutation_summary") or values.get("hypothesis") or "",
            "conditions": dict(values.get("conditions") or {}), "axes": axes, **axes,
            "verdict": verdict, "distill_ready": verdict == "distill_candidate",
            "return_tier": tier, "conditional_avoid": avoid, "reason": gate["outcome"], **gate}


def evaluate_exploration_candidate(candidate=None, *, target_speedup=None,
                                   require_reusable_scope=True,
                                   preserve_subtarget_evidence=True,
                                   max_workspace_delta_bytes=4096, **legacy) -> dict[str, Any]:
    """Judge evidence, goal, and reuse with an explicit G0-G3 gate breakdown."""
    if isinstance(candidate, Mapping):
        if legacy:
            raise ValueError(f"unexpected candidate arguments: {sorted(legacy)}")
        values = dict(candidate)
        return _evaluate_candidate(
            values, target_speedup=1.05 if target_speedup is None else target_speedup,
            require_reusable_scope=require_reusable_scope,
            preserve_subtarget_evidence=preserve_subtarget_evidence,
            max_workspace_delta_bytes=max_workspace_delta_bytes)
    if candidate is not None:
        raise ValueError("candidate must be an object")
    minimum = legacy.pop("min_speedup_ratio", 1.05)
    if target_speedup is not None:
        minimum = target_speedup
    return legacy_evaluate(**legacy, min_speedup_ratio=minimum)


def build_strategy_navigation_cards(
    index,
    candidate_ids: Sequence[str] | None = None,
    *,
    conditions: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build body-free strategy navigation cards with condition/trigger cues."""
    from .index import RoutingIndex, token_estimate

    if not hasattr(index, "units"):
        index = RoutingIndex(index)
    observed = None if conditions is None else _mapping(conditions, "conditions")
    if candidate_ids is None:
        ordered_ids = [
            ident for ident, unit in sorted(index.units.items())
            if unit.status == "active" and unit.metadata["category"] in {"sop", "workflow"}
        ]
    else:
        ordered_ids = []
        for raw_id in candidate_ids:
            ident = _text(raw_id, "candidate_id").split("@", 1)[0]
            if ident not in index.units:
                raise ValueError(f"unknown strategy candidate unit: {ident}")
            if ident not in ordered_ids:
                ordered_ids.append(ident)

    applicability = {
        ident: match_conditions(unit.metadata["conditions"], observed)
        for ident, unit in index.units.items()
    }
    status_label = {
        "matched": "matched",
        "mismatch": "mismatch",
        "unknown": "condition_unknown",
    }
    cards: list[dict[str, Any]] = []
    for ident in ordered_ids:
        unit = index.units[ident]
        closure_ids = list(index.closure[ident])
        unit_status = applicability[ident]
        missing = list(_missing(unit_status))
        mismatch_reasons = (
            list(unit_status.reasons) if unit_status.status == "mismatch" else []
        )
        closure_matched = unit_status.status == "matched"
        closure_active = unit.status == "active"
        for parent_id in closure_ids:
            parent_unit = index.units[parent_id]
            parent_status = applicability[parent_id]
            if parent_status.status != "matched":
                closure_matched = False
                for item in _missing(parent_status):
                    if item not in missing:
                        missing.append(item)
                if parent_status.status == "mismatch":
                    for reason in parent_status.reasons:
                        tagged = f"{parent_id}:{reason}"
                        if tagged not in mismatch_reasons:
                            mismatch_reasons.append(tagged)
            if parent_unit.status != "active":
                closure_active = False
        if closure_matched:
            overall_status = "matched"
        elif mismatch_reasons or unit_status.status == "mismatch":
            overall_status = "mismatch"
        else:
            overall_status = "condition_unknown"
        body_est = token_estimate(unit.body)
        closure_est = body_est + sum(
            token_estimate(index.units[parent_id].body) for parent_id in closure_ids
        )
        cards.append({
            "id": ident,
            "revision": unit.revision,
            "ref": f"{ident}@{unit.revision}",
            "title": unit.metadata["title"],
            "tier": unit.metadata["tier"],
            "category": unit.metadata["category"],
            "domain_path": unit.metadata["domain_path"],
            "evidence_strength": unit.metadata["evidence_strength"],
            "triggers": {
                "error_signatures": list(unit.metadata["triggers"]["error_signatures"]),
                "keywords": list(unit.metadata["triggers"]["keywords"]),
            },
            "conditions": dict(unit.metadata["conditions"]),
            "match_status": status_label.get(overall_status, overall_status),
            "ready_to_apply": bool(closure_matched and closure_active),
            "missing_conditions": missing,
            "mismatch_reasons": mismatch_reasons,
            "inherits_from": list(unit.metadata["inherits_from"]),
            "body_token_estimate": body_est,
            "closure_token_estimate": closure_est,
        })
    return cards


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mindie_knowledge.graded.explore")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("stagnation", "context", "evaluate"):
        sub.add_parser(name).add_argument("--input", default="-")
    args = parser.parse_args(argv)
    text = sys.stdin.read() if args.input in (None, "-") else Path(args.input).read_text(encoding="utf-8")
    payload = json.loads(text)
    if args.command == "stagnation":
        if isinstance(payload, Mapping):
            entries = payload.get("ledger_entries") or payload.get("attempts") or []
            options = {key: payload[key] for key in ("consecutive_limit", "max_stalled_attempts")
                       if key in payload}
        else:
            entries, options = payload, {}
        result = assess_stagnation(entries, **options)
    elif args.command == "context":
        if not isinstance(payload, Mapping):
            raise ValueError("context input must be an object")
        result = build_exploration_context(**payload)
    else:
        if not isinstance(payload, Mapping):
            raise ValueError("evaluate input must be an object")
        candidate = payload.get("candidate", payload)
        options = {key: payload[key] for key in
                   ("target_speedup", "require_reusable_scope",
                    "preserve_subtarget_evidence", "max_workspace_delta_bytes")
                   if key in payload}
        result = evaluate_exploration_candidate(candidate, **options)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["assess_stagnation", "build_citation_feedback_record", "build_exploration_context",
           "build_strategy_navigation_cards", "enforce_direction_budget",
           "evaluate_exploration_candidate", "main", "select_pareto_candidates",
           "stage_budget_guidance"]
