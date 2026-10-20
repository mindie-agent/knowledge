"""[Role] Legacy exploration evaluation and recursive gate-evidence helpers.

[Invariants] The historical three-axis evaluator keeps its exact verdicts and
return tier while the authoritative four-gate evaluator lives in ``explore``.
Gate evidence is inspected recursively; only an explicit regression signal
fails, and a missing optional key never fabricates one.
[Timeouts & Locks] Pure in-memory validation and arithmetic; no waits or locks.
[Side-Effect Policy] Returns derived judgments only; never writes or promotes.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

EVALUATION_SCHEMA = "mindie-exploration-evaluation/1"
TIERS = {"L1", "L2", "L3", "L4"}


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


def walk(value, key):
    if isinstance(value, Mapping):
        if key in value:
            yield value[key]
        for child in value.values():
            yield from walk(child, key)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            yield from walk(child, key)


def flagged(value, key) -> bool:
    return any(found not in (None, False, 0, "0", "", [], {}) for found in walk(value, key))


def guard_regression(value) -> bool:
    rows = value.values() if isinstance(value, Mapping) else value or ()
    for row in rows:
        if not isinstance(row, Mapping):
            if row is True:
                return True
            continue
        if (row.get("regression") is True or row.get("regressed") is True
                or row.get("guard_ok") is False):
            return True
        if row.get("baseline_pass") is True and row.get("candidate_pass") is False:
            return True
        baseline, candidate, margin = (row.get("baseline_ms"), row.get("candidate_ms"),
                                       row.get("noise_margin", 0.01))
        if all(isinstance(item, (int, float)) and not isinstance(item, bool)
               for item in (baseline, candidate, margin)) and candidate > baseline * (1 + margin):
            return True
    return False


def legacy_evaluate(*, run_id, candidate_id, mutation_summary, conditions, compile_ok,
                    oracle_passed, measurement_passed, speedup_ratio=None,
                    min_speedup_ratio=1.05, noise_margin=0.01, ablation_retained=None,
                    target_tier="L4", failure_reason="", goal_type="speedup",
                    goal_met=None, diagnostic_gain=False):
    """Preserve the historical three-axis result byte-for-byte at its public keys."""
    run_id = _text(run_id, "run_id")
    candidate_id = _text(candidate_id, "candidate_id")
    mutation_summary = _text(mutation_summary, "mutation_summary")
    current = _mapping(conditions, "conditions")
    if target_tier not in TIERS:
        raise ValueError(f"target_tier must be one of {sorted(TIERS)}")
    if goal_type not in {"speedup", "correctness", "diagnose"}:
        raise ValueError("goal_type must be one of ['correctness', 'diagnose', 'speedup']")
    if goal_met is not None and type(goal_met) is not bool:
        raise ValueError("goal_met must be a boolean or None")
    if type(diagnostic_gain) is not bool:
        raise ValueError("diagnostic_gain must be a boolean")
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
    failure_reason = "" if failure_reason is None else failure_reason
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
    elif goal_met is not None:
        attainment = "attained" if goal_met else "not_attained"
    elif goal_type == "speedup":
        if ratio is None:
            attainment = "not_applicable"
        elif ratio >= minimum:
            attainment = "attained" if ablation_retained is True else "not_applicable"
        else:
            attainment = "not_attained"
    elif goal_type == "correctness":
        attainment = "attained" if oracle_passed else "not_applicable"
    else:
        attainment = "attained" if diagnostic_gain else "not_attained"
    if validity == "valid" and reusability is None:
        if attainment == "attained" and goal_type == "speedup":
            reusability = "reusable_mutator" if target_tier == "L4" else "reusable_observation"
        elif goal_type == "speedup" and ratio is not None and ratio >= minimum:
            reusability = "inconclusive" if ablation_retained is None else "reusable_negative_boundary"
        elif attainment == "attained":
            reusability = "reusable_mutator" if ablation_retained is True else "reusable_observation"
        elif ratio is None:
            reusability = "reusable_negative_boundary" if ablation_retained is False else "reusable_observation"
        elif ratio < 1.0 - noise:
            reusability = "reusable_negative_boundary"
        elif ratio <= 1.0 + noise:
            reusability = "inconclusive"
        else:
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
        return_tier = "L2" if target_tier == "L4" else target_tier
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
    checks = {"G0": compile_ok, "G1": oracle_passed and measurement_passed,
              "G2": ratio is not None and ratio >= minimum,
              "G3": ablation_retained is not False}
    gates = {name: {"passed": passed, "ok": passed,
                    "status": "passed" if passed else "failed"}
             for name, passed in checks.items()}
    reached = next((name for name in ("G0", "G1", "G2", "G3") if not gates[name]["passed"]),
                   "PASSED")
    outcome = {"G0": "compile_failed", "G1": "correctness_failed",
               "G2": "sub_target_valid_evidence",
               "G3": "ablation_or_hygiene_failed"}.get(reached, "qualified_improvement")
    return {"schema": EVALUATION_SCHEMA, "run_id": run_id, "candidate_id": candidate_id,
            "mutation_summary": mutation_summary, "conditions": current, "axes": axes,
            **axes, "verdict": verdict, "distill_ready": distill_ready,
            "return_tier": return_tier, "conditional_avoid": conditional_avoid,
            "speedup_ratio": ratio, "min_speedup_ratio": minimum, "noise_margin": noise,
            "ablation_retained": ablation_retained, "target_tier": target_tier,
            "goal_type": goal_type, "goal_met": goal_met, "diagnostic_gain": diagnostic_gain,
            "gates": gates, "gate_reached": reached, "outcome": outcome,
            "reason": failure_reason or (
                "valid gain is below the stated target but remains reusable evidence"
                if verdict == "subtarget_valid" else verdict)}


def enforce_direction_budget(
    directions: Sequence[Mapping[str, Any]],
    *,
    kb_dir_cap: int = 1,
    require_cold_direction: bool = True,
    roofline_status: str = "calibrated",
) -> dict[str, Any]:
    """Enforce GEAK's KB direction cap, cold profile-only control, and roofline filter.

    In AMD-AGI/GEAK (`kernel_lane.js`), allowing the planner to seed every direction
    from learned KB cards regressed geomean speedup by 22.7% (`4.45x -> 3.44x`).
    At most `kb_dir_cap` directions per round may retain `learned_refs`, at least
    one direction must be planned purely from the L1/L2 profile (`learned_refs=[]`),
    and uncalibrated roofline hypotheses are filtered when `roofline_status != "calibrated"`.
    """
    if type(kb_dir_cap) is not int or kb_dir_cap < 0:
        raise ValueError("kb_dir_cap must be a nonnegative integer")
    calibrated = str(roofline_status or "").strip().lower() == "calibrated"
    filtered_roofline_ids: list[str] = []
    surviving: list[dict[str, Any]] = []
    for index, raw in enumerate(directions or ()):
        row = _mapping(raw, f"directions[{index}]")
        ident = str(row.get("direction_id") or row.get("id") or f"dir-{index}")
        hypothesis = str(row.get("hypothesis") or row.get("summary") or "")
        roofline_claim = bool(row.get("requires_roofline")) or (
            not calibrated
            and any(token in hypothesis.lower() for token in ("roofline", "% of peak", "peak hbm", "peak compute"))
        )
        if not calibrated and roofline_claim:
            filtered_roofline_ids.append(ident)
            continue
        refs = [str(ref) for ref in (row.get("learned_refs") or row.get("kb_refs") or ()) if str(ref).strip()]
        surviving.append({**row, "direction_id": ident, "learned_refs": refs, "kb_cap_stripped": False})
    kb_used = 0
    kb_cap_bound = False
    for row in surviving:
        if not row["learned_refs"]:
            continue
        if kb_used < kb_dir_cap:
            kb_used += 1
        else:
            row["stripped_learned_refs"] = list(row["learned_refs"])
            row["learned_refs"] = []
            row["kb_cap_stripped"] = True
            kb_cap_bound = True
    cold_enforced = False
    if require_cold_direction and surviving and all(bool(row["learned_refs"]) for row in surviving):
        last = surviving[-1]
        last["stripped_learned_refs"] = list(last["learned_refs"])
        last["learned_refs"] = []
        last["cold_direction_forced"] = True
        cold_enforced = True
        kb_cap_bound = True
    for row in surviving:
        row["is_cold_direction"] = not bool(row["learned_refs"])
    return {
        "directions": surviving,
        "kb_dir_cap": kb_dir_cap,
        "require_cold_direction": bool(require_cold_direction),
        "roofline_status": str(roofline_status or "calibrated"),
        "kb_cap_bound": kb_cap_bound,
        "cold_direction_enforced": cold_enforced,
        "cold_direction_count": sum(1 for row in surviving if row["is_cold_direction"]),
        "kb_seeded_count": sum(1 for row in surviving if not row["is_cold_direction"]),
        "filtered_uncalibrated_roofline_ids": filtered_roofline_ids,
    }


def build_citation_feedback_record(
    evaluation: Mapping[str, Any],
    *,
    cited_unit_ids: Sequence[str] = (),
    incumbent_speedup: float = 1.0,
    min_improve_delta: float = 0.01,
    is_round_winner: bool = True,
    verification_void: bool = False,
) -> dict[str, Any]:
    """Build a survivor-bias-free citation feedback record anchored to incumbent speedup."""
    row = _mapping(evaluation, "evaluation")
    incumbent = max(1.0, float(_number(incumbent_speedup, "incumbent_speedup") or 1.0))
    delta = max(0.0, float(_number(min_improve_delta, "min_improve_delta") or 0.0))
    speedup = _number(row.get("speedup", row.get("speedup_ratio")), "speedup", optional=True)
    axes = row.get("axes") if isinstance(row.get("axes"), Mapping) else {}
    evidence_valid = (
        not verification_void
        and row.get("evidence_valid") is not False
        and axes.get("evidence_validity") not in {"compile_failed", "oracle_failed", "measurement_invalid"}
        and row.get("outcome") not in {"infra_failed", "measurement_invalid", "compile_failed", "correctness_failed"}
    )
    improved_over_incumbent = bool(
        evidence_valid and speedup is not None and speedup >= incumbent * (1.0 + delta) - 1e-9
    )
    reusable = bool(row.get("reusable_encoding", row.get("ablation_retained") is not False))
    became_winner = bool(is_round_winner and improved_over_incumbent and reusable)
    cited_then_verified: bool | None = None if not evidence_valid else bool(improved_over_incumbent)
    return {
        "run_id": row.get("run_id"),
        "candidate_id": row.get("candidate_id"),
        "cited_unit_ids": [str(uid) for uid in (cited_unit_ids or ()) if str(uid).strip()],
        "speedup": speedup,
        "incumbent_speedup": incumbent,
        "min_improve_delta": delta,
        "evidence_valid": evidence_valid,
        "improved_over_incumbent": improved_over_incumbent,
        "became_winner": became_winner,
        "reprofile_required": became_winner,
        "cited_then_verified": cited_then_verified,
        "record_in_feedback_ledger": True,
        "allow_new_card_distillation": bool(became_winner and row.get("distill_ready")),
    }

