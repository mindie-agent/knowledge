"""[Role] Consume the case-bound measurement identity in a Bench C4 v2 receipt.

[Invariants] Every timing sample belongs to one case/block, and comparison
identities bind those exact samples. Observed block envelopes are not confidence
intervals. The caller still requires selected, independently verified evidence.
[Timeouts & Locks] Pure bounded-input validation; no I/O, waits or locks.
[Side-Effect Policy] Returns admission eligibility without changing receipts.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics

from .units import SHA256


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def _close(value, expected):
    return type(value) in (int, float) and math.isfinite(value) and math.isclose(value, expected, rel_tol=1e-12)


def _groups(run):
    timing = run["timing"]
    if (timing["clock"] != "host_monotonic_ns" or not timing["synchronizer"]
            or type(timing["warmup_rounds"]) is not int or timing["warmup_rounds"] < 1):
        raise ValueError("missing timing clock, synchronization or warmup identity")
    samples = timing["samples"]
    durations = []
    for sample in samples:
        start, end = sample["started_ns"], sample["ended_ns"]
        buffers = timing["buffer_sha256"][sample["shape_sha256"]]
        if (type(start) is not int or type(end) is not int or not 0 <= start < end
                or sample["synchronized_before"] is not True or sample["synchronized_after"] is not True
                or type(sample["buffer_index"]) is not int or not 0 <= sample["buffer_index"] < len(buffers)
                or any(not SHA256.fullmatch(value) for value in buffers)):
            raise ValueError("invalid measured interval or buffer identity")
        durations.append((end - start) / 1e6)
    if not durations or run["samples_ms"] != durations:
        raise ValueError("derived milliseconds differ from measured intervals")
    indices, keys, cases = [], set(), {}
    for group in run["sample_groups"]:
        case, block = group["case_sha256"], group["block_id"]
        selected = group["source_sample_indices"]
        if (not SHA256.fullmatch(case) or not block or (case, block) in keys
                or len(selected) < 2 or any(type(index) is not int or not 0 <= index < len(samples) for index in selected)
                or not group["source_ref"] or not group["grouping_derivation_ref"]
                or not SHA256.fullmatch(group["source_sha256"])
                or not SHA256.fullmatch(group["grouping_derivation_sha256"])
                or any(samples[index]["shape_sha256"] != case for index in selected)):
            raise ValueError("case/block provenance differs from measured samples")
        keys.add((case, block))
        indices.extend(selected)
        cases.setdefault(case, []).append([durations[index] for index in selected])
    if sorted(indices) != list(range(len(samples))) or any(len(blocks) < 2 for blocks in cases.values()):
        raise ValueError("case blocks must cover all samples and repeated blocks")
    workload = run["conditions"]["workload"]
    if (len(workload) != len(cases) or {item["workload_case_sha256"] for item in workload} != set(cases)
            or any(item["samples_per_shape"] != sum(map(len, cases[item["workload_case_sha256"]])) for item in workload)):
        raise ValueError("recorded workload differs from measured cases")
    return cases


def _comparison(value, reference, candidate, groups):
    if (value["estimator"] != "geomean_of_per_case_arithmetic_mean_ratios"
            or value["clock"] != "host_monotonic_ns"
            or value["reference_run_id"] != reference["run_id"] or value["candidate_run_id"] != candidate["run_id"]
            or value["reference_timing_sha256"] != _digest(reference["timing"])
            or value["candidate_timing_sha256"] != _digest(candidate["timing"])):
        raise ValueError("comparison identity does not bind the selected runs")
    left, right = groups[reference["run_id"]], groups[candidate["run_id"]]
    rows = value["cases"]
    if (set(left) != set(right) or len(rows) != len(left)
            or {row["case_sha256"] for row in rows} != set(left)
            or reference["timing"]["warmup_rounds"] != candidate["timing"]["warmup_rounds"]):
        raise ValueError("comparison case sets or warmup differ")
    ratios, lower = [], []
    for row in rows:
        case = row["case_sha256"]
        block_means = []
        means = []
        for side, blocks, run in (("reference", left[case], reference), ("candidate", right[case], candidate)):
            values = [sample for block in blocks for sample in block]
            per_block = [statistics.mean(block) for block in blocks]
            if (row[side + "_samples"] != len(values)
                    or not _close(row[side + "_mean_ms"], statistics.mean(values))
                    or len(row[side + "_block_means_ms"]) != len(per_block)
                    or any(not _close(actual, expected) for actual, expected in zip(row[side + "_block_means_ms"], per_block))):
                raise ValueError("reported case statistics differ from raw samples")
            block_means.append(per_block)
            means.append(statistics.mean(values))
        if (row["reference_samples"] != row["candidate_samples"]
                or len(reference["timing"]["buffer_sha256"][case]) != len(candidate["timing"]["buffer_sha256"][case])):
            raise ValueError("comparison sampling conditions differ")
        ratio = means[0] / means[1]
        bound = min(block_means[0]) / max(block_means[1])
        if (not _close(row["speedup_ratio"], ratio) or not _close(row["observed_block_ratio_lower"], bound)
                or not _close(row["observed_block_ratio_upper"], max(block_means[0]) / min(block_means[1]))):
            raise ValueError("reported ratio or observed envelope differs from measured blocks")
        ratios.append(ratio)
        lower.append(bound)
    speedup = math.exp(statistics.mean(map(math.log, ratios)))
    if not _close(value["speedup_geomean"], speedup):
        raise ValueError("aggregate estimator differs from case ratios")
    return speedup, lower


def verified_v2(receipt, selected, final):
    """Validate comparison identity, raw coverage and the recorded decision bounds."""
    try:
        groups = {row["run_id"]: _groups(row) for row in selected}
        baseline, full, candidate = selected
        speedup, bounds = _comparison(final["baseline_comparison"], baseline, candidate, groups)
        _, retention = _comparison(final["retention_comparison"], full, candidate, groups)
        minimum, loss = receipt["min_speedup"], receipt["max_relative_loss"]
        return (final["uncertainty_verified"] is True
                and _close(final["final_speedup_geomean"], speedup)
                and final["final_timing_sha256"] == _digest(candidate["timing"])
                and type(minimum) in (int, float) and math.isfinite(minimum) and minimum > 1
                and type(loss) in (int, float) and math.isfinite(loss) and 0 <= loss < 1
                and all(bound >= minimum for bound in bounds)
                and all(bound >= 1 - loss for bound in retention))
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return False


RETENTION_SCHEMA = "mindie-heldout-retention/1"
RETENTION_BUCKETS = ("both_pass", "opt_regression", "both_fail",
                     "opt_improvement", "evaluation_error")


def _case_milliseconds(value, label):
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError(f"{label} must be a positive finite millisecond value")
    return float(value)


def classify_heldout_cases(cases, *, noise_margin: float = 0.0) -> dict:
    """Classify frozen hidden inputs into the five retention buckets.

    Only a both_pass case with two valid paired timings contributes a speedup
    ratio. Guard workloads are measured separately: a local gain never masks a
    guard regression, and an unavailable measurement is evaluation_error rather
    than a zero or a fabricated pass.
    """
    if (isinstance(noise_margin, bool) or not isinstance(noise_margin, (int, float))
            or not math.isfinite(noise_margin) or noise_margin < 0):
        raise ValueError("noise_margin must be a nonnegative finite number")
    rows, seen = [], set()
    for index, raw in enumerate(cases or ()):
        if not hasattr(raw, "get"):
            raise ValueError(f"cases[{index}] must be an object")
        case_id = raw.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip() or case_id in seen:
            raise ValueError(f"cases[{index}].case_id must be unique nonempty text")
        seen.add(case_id)
        baseline_ok = raw.get("baseline_pass")
        candidate_ok = raw.get("candidate_pass")
        if type(baseline_ok) is not bool or type(candidate_ok) is not bool:
            raise ValueError(f"{case_id}: baseline_pass and candidate_pass must be booleans")
        guard = raw.get("guard", False)
        if type(guard) is not bool:
            raise ValueError(f"{case_id}: guard must be a boolean")
        error = raw.get("error")
        if error is not None and (not isinstance(error, str) or not error.strip()):
            raise ValueError(f"{case_id}: error must be nonempty text when supplied")
        ratio, comparable, bucket = None, False, None
        if error:
            bucket = "evaluation_error"
        elif baseline_ok and candidate_ok:
            bucket = "both_pass"
            try:
                baseline_ms = _case_milliseconds(raw.get("baseline_ms"), f"{case_id}.baseline_ms")
                candidate_ms = _case_milliseconds(raw.get("candidate_ms"), f"{case_id}.candidate_ms")
            except ValueError as exc:
                bucket, error = "evaluation_error", str(exc)
            else:
                if baseline_ms is not None and candidate_ms is not None:
                    ratio, comparable = baseline_ms / candidate_ms, True
        elif baseline_ok:
            bucket = "opt_regression"
        elif candidate_ok:
            bucket = "opt_improvement"
        else:
            bucket = "both_fail"
        rows.append({"case_id": case_id, "bucket": bucket, "guard": guard,
                     "baseline_pass": baseline_ok, "candidate_pass": candidate_ok,
                     "comparable": comparable, "speedup_ratio": ratio,
                     "error": error})
    counts = {bucket: 0 for bucket in RETENTION_BUCKETS}
    for row in rows:
        counts[row["bucket"]] += 1
    retained, regressed = counts["both_pass"], counts["opt_regression"]
    denominator = retained + regressed
    ratios = [row["speedup_ratio"] for row in rows
              if row["bucket"] == "both_pass" and row["comparable"]]
    main_ratios = [row["speedup_ratio"] for row in rows
                   if row["bucket"] == "both_pass" and row["comparable"] and not row["guard"]]
    guard_ratios = [row["speedup_ratio"] for row in rows
                    if row["bucket"] == "both_pass" and row["comparable"] and row["guard"]]
    guards = [row for row in rows if row["guard"]]
    guard_ok = (all(row["bucket"] == "both_pass" and row["comparable"]
                    and row["speedup_ratio"] >= 1 - noise_margin for row in guards)
                if guards else None)
    return {
        "schema": RETENTION_SCHEMA,
        "buckets": counts,
        "total": len(rows),
        "cases": rows,
        "correctness_retention": (retained / denominator if denominator else None),
        "both_pass": retained,
        "opt_regression": regressed,
        "evaluation_errors": counts["evaluation_error"],
        "comparable_cases": len(ratios),
        "speedup_geomean": (math.exp(statistics.mean(map(math.log, ratios)))
                            if ratios else None),
        "main_speedup_geomean": (math.exp(statistics.mean(map(math.log, main_ratios)))
                                 if main_ratios else None),
        "guard_speedup_geomean": (math.exp(statistics.mean(map(math.log, guard_ratios)))
                                  if guard_ratios else None),
        "guards_measured": bool(guards),
        "guard_ok": guard_ok,
        "noise_margin": float(noise_margin),
        "complete": bool(rows) and counts["evaluation_error"] == 0,
    }


__all__ = ["RETENTION_BUCKETS", "RETENTION_SCHEMA", "classify_heldout_cases",
           "verified_v2"]

