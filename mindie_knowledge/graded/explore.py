"""[Role] Model-free stagnation, de-biased context, and three-axis exploration judgment.

[Invariants] Information stagnation is distinct from execution failure: an
infrastructure or measurement fault requests repair and never counts as an
unsupported attempt. L3 bodies are stripped from exploration context; only
L1/L2/L4 rules mount, and a conditional avoidance needs a valid refuted ledger
entry whose recorded conditions match the task. Evidence validity, goal
attainment, and reusability are orthogonal; a valid 1.04x gain below a 1.05x
target is subtarget_valid/reusable_observation, never refuted. Native run and
event identities deduplicate attempts.
[Timeouts & Locks] Pure in-memory validation and arithmetic; no subprocess,
network, model call, sleep, or lock is introduced.
[Side-Effect Policy] Returns derived judgments only; never writes knowledge,
runs experiments, or changes a unit revision or conditions.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .conditions import match_conditions

CONTEXT_SCHEMA = "mindie-exploration-context/1"
EVALUATION_SCHEMA = "mindie-exploration-evaluation/1"
TIERS = {"L1", "L2", "L3", "L4"}
REPAIR_OUTCOMES = {"infra_failed", "measurement_invalid"}


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


def assess_stagnation(attempts: Sequence[Mapping[str, Any]], *,
                      max_stalled_attempts: int = 2) -> dict[str, Any]:
    """Deduplicate attempts and count only attempts that produced no new fact."""
    if type(max_stalled_attempts) is not int or max_stalled_attempts < 1:
        raise ValueError("max_stalled_attempts must be a positive integer")
    unique, seen = [], set()
    for index, raw in enumerate(attempts or ()):
        attempt = _mapping(raw, f"attempts[{index}]")
        run_id = attempt.get("run_id")
        key = ("run", run_id.strip()) if isinstance(run_id, str) and run_id.strip() else ("attempt", index)
        if key not in seen:
            seen.add(key)
            unique.append(attempt)
    stalled_count, repair, progressed = 0, False, False
    for attempt in unique:
        verdict, outcome = attempt.get("verdict"), attempt.get("outcome")
        if outcome in REPAIR_OUTCOMES or verdict == "inconclusive":
            repair = True
        elif (verdict == "supported" or attempt.get("new_observation") is True
              or attempt.get("divergent_layer_identified") is True):
            stalled_count, progressed = 0, True
        else:
            stalled_count += 1
    if stalled_count >= max_stalled_attempts:
        status, stalled = "stalled", True
        reason = f"{stalled_count} deduplicated attempts produced no new observation or identified layer"
    elif repair and not stalled_count:
        status, stalled = "repair_execution_conditions", False
        reason = "execution conditions are invalid; repair them before judging information stagnation"
    elif not unique:
        status, stalled = "no_attempts", False
        reason = "no attempts were supplied"
    elif stalled_count:
        status, stalled = "monitoring", False
        reason = f"{stalled_count} unproductive attempt(s); below the {max_stalled_attempts} reminder threshold"
    elif progressed:
        status, stalled = "progressing", False
        reason = "a new observation, supported verdict, or identified divergent layer is progress"
    else:
        status, stalled = "monitoring", False
        reason = "no decisive information yet"
    if repair and status in {"progressing", "monitoring"}:
        reason += "; some attempts require repaired execution conditions"
    return {"stalled": stalled, "stalled_count": stalled_count, "status": status, "reason": reason}


def _compact_unit(unit: dict) -> dict | None:
    tier = unit.get("tier")
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


def build_exploration_context(*, goal: str, retrieved_units: Sequence[Mapping[str, Any]],
                              bottleneck_summary: Mapping[str, Any],
                              ledger_entries: Sequence[Mapping[str, Any]],
                              conditions: Mapping[str, Any],
                              native_subcontext: bool = False) -> dict[str, Any]:
    """Assemble the minimal independent fact pack; old L3 guidance is withheld."""
    goal = _text(goal, "goal")
    current = _mapping(conditions, "conditions")
    bottleneck = _mapping(bottleneck_summary, "bottleneck_summary")
    mounted, stripped, ledger_refs, avoid = [], [], [], []
    for index, raw in enumerate(retrieved_units or ()):
        unit = _mapping(raw, f"retrieved_units[{index}]")
        if unit.get("tier") == "L3":
            ident = unit.get("id")
            if not isinstance(ident, str) or not ident:
                raise ValueError("retrieved L3 unit requires a stable id")
            stripped.append(ident)
        else:
            compact = _compact_unit(unit)
            if compact is not None:
                mounted.append(compact)
    for index, raw in enumerate(ledger_entries or ()):
        entry = _mapping(raw, f"ledger_entries[{index}]")
        ledger_refs.append({"run_id": entry.get("run_id"), "verdict": entry.get("verdict")})
        axes = entry.get("axes")
        if not isinstance(axes, Mapping) or axes.get("evidence_validity") != "valid":
            continue
        if entry.get("verdict") != "refuted":
            continue
        required = entry.get("conditions")
        if required is None and isinstance(entry.get("applicability"), Mapping):
            required = entry["applicability"].get("conditions")
        if not isinstance(required, Mapping) or not required:
            continue
        if match_conditions(dict(required), current).status != "matched":
            continue
        avoid.append({"run_id": entry.get("run_id"), "conditions": dict(required),
                      "avoid": entry.get("mutation_summary") or entry.get("candidate_id"),
                      "reason": entry.get("reason") or entry.get("failure_reason") or ""})
    mounted.sort(key=lambda item: (item["id"], item.get("revision") or 0))
    stripped.sort()
    avoid.sort(key=lambda item: (str(item["run_id"]), str(item["avoid"])))
    ledger_refs.sort(key=lambda item: (str(item["run_id"]), str(item["verdict"])))
    return {
        "schema": CONTEXT_SCHEMA, "goal": goal,
        "isolation_mode": "native_subcontext" if native_subcontext else "in_session_strategy_shift",
        "native_subcontext": bool(native_subcontext), "mounted_units": mounted,
        "units": mounted, "stripped_l3_ids": stripped, "conditional_avoid": avoid,
        "bottleneck_summary": bottleneck, "conditions": current, "ledger_refs": ledger_refs,
        "notes": [
            "Retrieved L3 recommendation steps are withheld to reduce anchoring.",
            "Deleting an L3 dictionary key does not remove already-visible history; only native_subcontext claims real isolation.",
            "Conditional avoidance is conditional evidence, not a permanent ban.",
        ],
    }


def evaluate_exploration_candidate(*, run_id: str, candidate_id: str,
                                   mutation_summary: str, conditions: Mapping[str, Any],
                                   compile_ok: bool, oracle_passed: bool,
                                   measurement_passed: bool,
                                   speedup_ratio: float | None = None,
                                   min_speedup_ratio: float = 1.05,
                                   noise_margin: float = 0.01,
                                   ablation_retained: bool | None = None,
                                   target_tier: str = "L4",
                                   failure_reason: str = "") -> dict[str, Any]:
    """Judge evidence, goal, and reuse independently; never refute a valid subtarget gain."""
    run_id = _text(run_id, "run_id")
    candidate_id = _text(candidate_id, "candidate_id")
    mutation_summary = _text(mutation_summary, "mutation_summary")
    current = _mapping(conditions, "conditions")
    if target_tier not in TIERS:
        raise ValueError(f"target_tier must be one of {sorted(TIERS)}")
    for name, value in (("compile_ok", compile_ok), ("oracle_passed", oracle_passed),
                        ("measurement_passed", measurement_passed)):
        if type(value) is not bool:
            raise ValueError(f"{name} must be a boolean")
    if ablation_retained is not None and type(ablation_retained) is not bool:
        raise ValueError("ablation_retained must be a boolean or None")
    ratio = _number(speedup_ratio, "speedup_ratio", optional=True)
    minimum = _number(min_speedup_ratio, "min_speedup_ratio")
    noise = _number(noise_margin, "noise_margin")
    if minimum <= 0 or noise < 0:
        raise ValueError("min_speedup_ratio must be positive and noise_margin nonnegative")
    if failure_reason is None:
        failure_reason = ""
    if not isinstance(failure_reason, str):
        raise ValueError("failure_reason must be text")

    if not compile_ok:
        validity, reusability = "compile_failed", "inconclusive"
    elif not oracle_passed:
        validity, reusability = "oracle_failed", "inconclusive"
    elif not measurement_passed:
        validity, reusability = "measurement_invalid", "inconclusive"
    else:
        validity, reusability = "valid", None
    if validity != "valid":
        attainment = "not_applicable"
    elif ratio is None:
        attainment = "not_applicable"
    elif ratio >= minimum:
        attainment = "attained"
    else:
        attainment = "not_attained"

    if validity == "valid" and reusability is None:
        if attainment == "attained":
            reusability = ("reusable_mutator" if ablation_retained is True
                           else "reusable_negative_boundary" if ablation_retained is False
                           else "reusable_observation")
        elif ratio is None:
            reusability = ("reusable_negative_boundary" if ablation_retained is False
                           else "reusable_observation")
        elif ratio < 1.0 - noise:
            reusability = "reusable_negative_boundary"
        elif ratio <= 1.0 + noise:
            reusability = "inconclusive"
        else:
            # A positive gain below target stays reusable evidence even when the
            # proposed mutator ablation did not retain the change.
            reusability = "reusable_observation"

    if validity != "valid":
        verdict = validity
    elif attainment == "attained":
        verdict = "attained"
    elif reusability == "reusable_negative_boundary":
        verdict = "refuted"
    elif reusability == "reusable_observation" and ratio is not None and ratio > 1.0 + noise:
        verdict = "subtarget_valid"
    elif reusability == "inconclusive":
        verdict = "inconclusive"
    else:
        verdict = "unconfirmed"

    if reusability == "reusable_negative_boundary":
        return_tier = "L1" if target_tier == "L4" else target_tier
        conditional_avoid = [{"conditions": current, "avoid": mutation_summary,
                              "reason": failure_reason or verdict}]
    elif reusability == "reusable_observation":
        return_tier = "L2" if target_tier in {"L3", "L4"} else target_tier
        conditional_avoid = []
    else:
        return_tier, conditional_avoid = target_tier, []
    distill_ready = reusability in {"reusable_mutator", "reusable_observation",
                                    "reusable_negative_boundary"}
    axes = {"evidence_validity": validity, "goal_attainment": attainment,
            "reusability": reusability}
    return {
        "schema": EVALUATION_SCHEMA, "run_id": run_id, "candidate_id": candidate_id,
        "mutation_summary": mutation_summary, "conditions": current, "axes": axes,
        **axes, "verdict": verdict, "distill_ready": distill_ready,
        "return_tier": return_tier, "conditional_avoid": conditional_avoid,
        "speedup_ratio": ratio, "min_speedup_ratio": minimum, "noise_margin": noise,
        "ablation_retained": ablation_retained, "target_tier": target_tier,
        "reason": failure_reason or (
            "valid gain is below the stated target but remains reusable evidence"
            if verdict == "subtarget_valid" else verdict),
    }


__all__ = ["assess_stagnation", "build_exploration_context",
           "evaluate_exploration_candidate"]
