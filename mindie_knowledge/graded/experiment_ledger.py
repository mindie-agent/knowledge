"""Source fingerprint, raw paired-trial check, 4-gate RSI judgment, and pre-device ledger gate.

[Role]
Identifies the exact source under test (`fingerprint`), validates raw paired trials
before trusting a speedup (`check_trials`), evaluates the 4-stage RSI candidate
gates and three orthogonal axes (`judge`), renders `PROGRESS.md` while preserving
launch-time `in_flight_runs` recovery state (`append`, `render_progress_file`),
and blocks device submissions that repeat a settled source or exceed run /
card-second budgets (`gate`). Runs with zero `mindie-bench` runtime dependency.

[Invariants]
- Missing or incomplete evidence (`compile_ok=False`, missing/failed trials,
  non-finite paired ratios, execution-failure `run_status`, unverified correctness,
  or missing/unchanged source fingerprint) sets `measurement_passed=False` and
  always yields `verdict="inconclusive"`, outranking `oracle_passed=False` and
  failed `hidden_input_summary.generalization_ok`.
- Any positive speedup above noise (`speedup > 1 + noise_margin`) must pass the
  Gate 3 ablation check before becoming `supported` or `subtarget_valid`.
- `append` commits the JSONL ledger entry first, folds `supersedes_exp_id`
  revisions in-place, resolves cumulative speedups over explicit baseline/source
  lineage, preserves `in_flight_runs` / recovery sections in `PROGRESS.md`, and
  reports `progress_unconfirmed=True` + `progress_warning` if `PROGRESS.md` fails.

[Timeouts & Locks]
- `fingerprint` bounds every `git` call (`15s`) and the total scan (`60s`),
  streams diffs through a temporary file, and records untracked files `> 512 MiB` by size.
- `append` and `render_progress_file` hold `<ledger>.lock` via `StartLock` (`5.0s`).

[Side-Effect Policy]
- Only `append` and `render_progress_file` write to disk (ledger JSONL + `PROGRESS.md`).
"""
from __future__ import annotations
import argparse, contextlib, hashlib, json, math, os, re, statistics, subprocess, sys, tempfile, time
from pathlib import Path
try:
    from .explore_support import flagged as _flagged, guard_regression as _guard_regression
except ImportError:
    from mindie_knowledge.graded.explore_support import flagged as _flagged, guard_regression as _guard_regression

SCHEMA_VERSION, EVALUATION_SCHEMA = "mindie-experiment-ledger/1", "mindie-rsi-evaluation/1"
PROGRESS_MARKER_START, PROGRESS_MARKER_END = "<!-- mindie-rsi-progress:start -->", "<!-- mindie-rsi-progress:end -->"
MANAGED_PROGRESS_HEADINGS = frozenset({"Goal", "Best Supported Candidate", "Active Hypotheses", "Refuted Directions", "Experiments To Complete"})
TASK_ARTIFACT_NAMES = frozenset({"rsi-loop.json", "rsi-loop.json.lock", "rsi-loop.json.tmp", "experiment-ledger.jsonl", "experiment-ledger.jsonl.lock", "experiment-ledger.jsonl.tmp", "PROGRESS.md", "PROGRESS.md.lock", "PROGRESS.md.tmp"})
RECOVERY_TOKEN_RE = re.compile(r"(?i)(?:\b(?:in_flight_runs|slot_id|job_id|run_id|host_endpoint|manifest_path)\b|\b(?:endpoint|manifest)\b[`'\"]?\s*[:=])")
SETTLED = frozenset({"supported", "subtarget_valid", "refuted"})
INCONCLUSIVE_STATUSES = frozenset({"compile_failed", "infra_failed", "measurement_invalid", "timeout", "interrupted", "unknown"})
LEARN_MUTABLE_KEYS = frozenset({"schema_version", "supersedes_exp_id", "lesson", "new_observation", "divergent_layer_identified", "_superseded_ids", "candidate_repo"})
GIT_TIMEOUT = 15.0  # source: per-call git bound used by InferRSI infra/targets/git.py
FINGERPRINT_TIMEOUT = 4 * GIT_TIMEOUT  # source: four git calls of a submodule-free fingerprint
LARGE_FILE_BYTES = 512 * 1024 * 1024  # source: git core.bigFileThreshold default (512 MiB)
LOCK_WAIT_SECONDS = 5.0  # source: knowledge AGENTS.md lock rule (bounded 5.0 s waits)
DEFAULT_TARGET, DEFAULT_NOISE_MARGIN = 1.05, 0.01  # source: default target speedup ratio & noise margin
ARGUMENT_LIMIT, BLOCKED = 1024 * 1024, 3  # source: 1 MiB stdin bound & gate exit code


def _git(repo: Path, *args: str, deadline: float, stdout=subprocess.PIPE) -> bytes:
    timeout = min(GIT_TIMEOUT, deadline - time.monotonic())
    if timeout <= 0:
        raise subprocess.TimeoutExpired(["git", *args], FINGERPRINT_TIMEOUT)
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    res = subprocess.run(["git", "-C", str(repo), *args], stdout=stdout, stderr=subprocess.PIPE, timeout=timeout, check=False, **options)
    if res.returncode:
        raise ValueError(res.stderr.decode("utf-8", errors="replace").strip() or f"git {args[0]} failed")
    return res.stdout

def _stream_digest(handle, *, deadline: float | None = None) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    for block in iter(lambda: handle.read(1 << 20), b""):  # streamed (Principle 18)
        if deadline is not None and time.monotonic() > deadline:
            raise subprocess.TimeoutExpired(["fingerprint"], FINGERPRINT_TIMEOUT)
        digest.update(block)
        size += len(block)
    return digest.hexdigest(), size

def _excluded_set(exclude_paths) -> set[Path]:
    out: set[Path] = set()
    for raw in exclude_paths or ():
        if raw:
            p = Path(raw).resolve()
            out.update((p, p.with_name(p.name + ".lock"), p.with_name(p.name + ".tmp")))
    return out

def _is_task_artifact(path: Path) -> bool:
    if path.name in TASK_ARTIFACT_NAMES:
        return True
    target = path.with_suffix("") if path.suffix in (".lock", ".tmp") else path
    if target.name in TASK_ARTIFACT_NAMES:
        return True
    if target.suffix in (".json", ".jsonl") and target.is_file():
        with target.open("rb") as fh:
            peek = fh.read(65536)
        if b'"mindie-rsi-loop/' in peek or b'"mindie-experiment-ledger/' in peek:
            return True
    return False

def _worktree_state(repo: Path, *, deadline: float, excluded_abs: set[Path] = frozenset()) -> tuple[str, int, list, list]:
    rel_excludes = [f":(exclude){p.relative_to(repo).as_posix()}" for p in excluded_abs if p.is_relative_to(repo)]
    rel_excludes.extend(f":(exclude)**/{name}" for name in TASK_ARTIFACT_NAMES)
    for raw in filter(None, _git(repo, "diff", "--name-only", "-z", "HEAD", deadline=deadline).split(b"\0")):
        rel = os.fsdecode(raw)
        if _is_task_artifact(repo / rel):
            rel_excludes.append(f":(exclude){rel}")
    with tempfile.TemporaryFile() as spool:
        _git(repo, "diff", "--binary", "HEAD", "--", ".", *rel_excludes, deadline=deadline, stdout=spool)
        spool.seek(0)
        diff_sha256, diff_bytes = _stream_digest(spool, deadline=deadline)
    untracked, large = [], []
    for raw in filter(None, _git(repo, "ls-files", "--others", "--exclude-standard", "-z", deadline=deadline).split(b"\0")):
        name, path = os.fsdecode(raw), repo / os.fsdecode(raw)
        if path.resolve() in excluded_abs or _is_task_artifact(path):
            continue
        if not path.is_file():
            untracked.append([name, "non-file"])
        elif path.stat().st_size > LARGE_FILE_BYTES:
            untracked.append([name, f"size:{path.stat().st_size}"])
            large.append(name)
        else:
            with path.open("rb") as handle:
                untracked.append([name, _stream_digest(handle, deadline=deadline)[0]])
        if time.monotonic() > deadline:
            raise subprocess.TimeoutExpired(["fingerprint", str(repo)], FINGERPRINT_TIMEOUT)
    return diff_sha256, diff_bytes, sorted(untracked), large

def fingerprint(repo, *, exclude_paths=()) -> dict:
    """Identify the exact source under test: HEAD, tracked diff, untracked files, submodules."""
    deadline, repo, excluded_abs = time.monotonic() + FINGERPRINT_TIMEOUT, Path(repo).resolve(), _excluded_set(exclude_paths)
    top = _git(repo, "rev-parse", "--show-toplevel", deadline=deadline)
    if Path(os.fsdecode(top).strip()).resolve() != repo:
        raise ValueError("repo must be the root of its Git checkout")
    head = _git(repo, "rev-parse", "HEAD", deadline=deadline).decode("ascii").strip()
    diff_sha256, diff_bytes, untracked, large = _worktree_state(repo, deadline=deadline, excluded_abs=excluded_abs)
    submodules = []
    for raw in _git(repo, "submodule", "status", "--recursive", deadline=deadline).splitlines():
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        commit, _, rest = line.lstrip("+-U ").partition(" ")
        path, sub_repo = rest.split(" (", 1)[0], repo / rest.split(" (", 1)[0]
        if not sub_repo.is_dir():
            submodules.append([path, commit, None])
        elif _git(sub_repo, "status", "--porcelain", deadline=deadline):
            sub_diff, _, sub_untracked, sub_large = _worktree_state(sub_repo, deadline=deadline, excluded_abs=excluded_abs)
            large.extend(f"{path}/{item}" for item in sub_large)
            submodules.append([path, commit, True, sub_diff, sub_untracked])
        else:
            submodules.append([path, commit, False])
    material = dict(head=head, diff_sha256=diff_sha256, untracked=untracked, submodules=sorted(submodules))
    digest = hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()
    result = dict(source_digest="sha256:" + digest, head=head,
                  dirty=bool(diff_bytes or untracked or any(s[2] for s in submodules)),
                  untracked_files=len(untracked), submodules=len(submodules))
    if large:
        result["unhashed_large_files"] = sorted(large)
    return result

def _number(value, label, *, strict=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or (value <= 0 if strict else value < 0):
        raise ValueError(f"{label} must be a {'positive' if strict else 'nonnegative'} number")
    return float(value)

def _optional_bool(value, label: str) -> bool | None:
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean or null")
    return value

def _metric(trial, label, index, problems):
    if not isinstance(trial, dict) or trial.get("status") != "completed":
        problems.append(f"{label} trial {index}: status {(trial.get('status') if isinstance(trial, dict) else 'not an object')!r}")
        return None
    val = trial.get("metric")
    if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or val <= 0:
        problems.append(f"{label} trial {index}: metric missing or not positive")
        return None
    return float(val)

def check_trials(evidence) -> dict:
    """Turn raw paired trials into the evidence flags, metrics, and ratio for RSI gates."""
    if not isinstance(evidence, dict):
        raise ValueError("evidence must be an object")
    repetitions, direction = evidence.get("repetitions"), evidence.get("direction")
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    if direction not in ("maximize", "minimize"):
        raise ValueError("direction must be maximize or minimize")
    problems, pairs = [], {}
    for label in ("baseline", "candidate"):
        trials = evidence.get(label)
        if not isinstance(trials, list):
            raise ValueError(f"{label} must be an array of trials")
        if len(trials) != repetitions:
            problems.append(f"{label}: expected {repetitions} trials, recorded {len(trials)}")
        pairs[label] = [_metric(t, label, i, problems) for i, t in enumerate(trials, 1)]
    base_src, cand_src = evidence.get("baseline_source_digest"), evidence.get("source_digest")
    if not cand_src or not base_src:
        problems.append("source fingerprint missing; the measured code is not identified")
    elif cand_src == base_src:
        problems.append("candidate source equals baseline source")
    correctness = evidence.get("correctness")
    if correctness not in ("passed", "failed"):
        problems.append(f"correctness was not verified ({correctness!r})")
    complete = [(b, c) for b, c in zip(pairs["baseline"], pairs["candidate"]) if b is not None and c is not None]
    ratios = []
    for idx, (b, c) in enumerate(complete, 1):
        r = c / b if direction == "maximize" else b / c
        if not math.isfinite(r) or r <= 0:
            problems.append(f"paired round {idx}: speedup ratio is not finite")
        else:
            ratios.append(r)
    ratio = statistics.median(ratios) if (ratios and len(ratios) == len(complete)) else None
    if ratio is not None and (not math.isfinite(ratio) or ratio <= 0):
        problems.append("median speedup ratio is not finite")
        ratio = None
    baseline_metric = statistics.median([b for b, _ in complete]) if complete else None
    candidate_metric = statistics.median([c for _, c in complete]) if complete else None
    status = evidence.get("run_status")
    if status in INCONCLUSIVE_STATUSES and status != "compile_failed":
        problems.insert(0, f"run_status {status!r} means evidence is missing")
    compile_ok = evidence.get("compile_ok") is not False and status != "compile_failed"
    if not compile_ok:
        problems.insert(0, "candidate did not build; nothing was measured")
    measurement_passed = bool(compile_ok and not problems and complete and ratio is not None and math.isfinite(ratio) and ratio > 0)
    return dict(compile_ok=compile_ok, oracle_passed=correctness != "failed" or not measurement_passed,
                measurement_passed=measurement_passed, speedup_ratio=ratio,
                baseline_metric=baseline_metric, candidate_metric=candidate_metric,
                direction=direction, paired_rounds=len(complete), problems=problems)

def _resolve_ablation_retained(ablation_retained, ablation_receipt) -> bool | None:
    retained = _optional_bool(ablation_retained, "ablation_retained")
    if ablation_receipt is None:
        return retained
    if not isinstance(ablation_receipt, dict):
        raise ValueError("ablation_receipt must be an object when given")
    status = ablation_receipt.get("status")
    pruned_verified = _optional_bool(ablation_receipt.get("pruned_combination_verified"), "ablation_receipt.pruned_combination_verified")
    receipt_retained = _optional_bool(ablation_receipt.get("ablation_retained"), "ablation_receipt.ablation_retained")
    if status == "candidate_for_sequential_pruning":
        if pruned_verified is False:
            return True
        if pruned_verified is True:
            if ablation_receipt.get("correctness_verified") is False or ablation_receipt.get("performance_verified") is False:
                return True
            return False if receipt_retained is None else receipt_retained
        return None
    if status in ("failed", "error", "pending", "incomplete", "inconclusive"):
        return None
    if receipt_retained is not None:
        if pruned_verified is True and receipt_retained is True:
            raise ValueError("conflicting ablation_receipt: pruned_combination_verified=True cannot also set ablation_retained=True")
        return receipt_retained
    return False if pruned_verified is True else retained

def _ablation_retained(evidence):
    raw_ablation = evidence.get("ablation")
    raw_val = (
        raw_ablation.get("passed", raw_ablation.get("ablation_retained"))
        if isinstance(raw_ablation, dict)
        else evidence.get("ablation_retained", evidence.get("ablation_ok", evidence.get("ablation_passed", raw_ablation)))
    )
    resolved = _resolve_ablation_retained(raw_val, evidence.get("ablation_receipt"))
    if resolved is not None or any(k in evidence for k in ("ablation_retained", "ablation_ok", "ablation_passed", "ablation", "ablation_receipt")):
        return resolved
    factors = evidence.get("factors")
    return True if not factors or len(factors) <= 1 else None

def _gate3_status(
    resolved_ablation: bool | None, *, hygiene_ok: bool | None = None,
    workspace_delta_bytes: int | float | None = None, max_workspace_delta_bytes: int = 4096,
    guard_workloads=None, reusable_scope: bool | None = None, require_reusable_scope: bool = False,
) -> tuple[bool | None, str, bool]:
    hygiene, reusable = _optional_bool(hygiene_ok, "hygiene_ok"), _optional_bool(reusable_scope, "reusable_scope")
    max_ws = int(_number(max_workspace_delta_bytes, "max_workspace_delta_bytes", strict=False))
    ws_ok = True if workspace_delta_bytes is None else (_number(workspace_delta_bytes, "workspace_delta_bytes", strict=False) <= max_ws)
    guard_ok = not _guard_regression(guard_workloads) if guard_workloads is not None else True
    scope_ok = not (require_reusable_scope and reusable is False)
    extra_failed = (hygiene is False) or (not ws_ok) or (not guard_ok) or (not scope_ok)
    if resolved_ablation is False:
        return False, "ablation_not_retained", extra_failed
    if not guard_ok:
        return False, "guard_workload_regressed", extra_failed
    if not ws_ok:
        return False, "workspace_delta_exceeded", extra_failed
    if hygiene is False:
        return False, "hygiene_failed", extra_failed
    if not scope_ok:
        return False, "reusable_scope_missing", extra_failed
    return (None, "ablation_pending", extra_failed) if resolved_ablation is None else (True, "", extra_failed)

def evaluate_rsi_axes(*, compile_ok: bool, oracle_passed: bool, measurement_passed: bool,
                      speedup: float | None, minimum: float, ablation_retained: bool | None,
                      noise_margin: float = DEFAULT_NOISE_MARGIN,
                      preserve_subtarget_evidence: bool = True) -> tuple[str, str, str, str, str, int]:
    """Evaluate three orthogonal RSI axes: Validity, Verdict/Outcome, Goal, Knowledge Reuse."""
    minimum, margin = _number(minimum, "min_speedup_ratio"), _number(noise_margin, "noise_margin", strict=False)
    if minimum < 1.0:
        raise ValueError("min_speedup_ratio must be >= 1.0")
    retained = _optional_bool(ablation_retained, "ablation_retained")
    if speedup is not None and (isinstance(speedup, bool) or not isinstance(speedup, (int, float))):
        raise ValueError("speedup must be a number or null")
    speedup = float(speedup) if speedup is not None else None
    valid_speedup = speedup is not None and math.isfinite(speedup) and speedup > 0
    reuse = "reusable_mechanism" if minimum >= DEFAULT_TARGET else "reusable_observation"
    if not compile_ok:
        return ("compile_failed", "inconclusive", "compile_failed", "not_applicable", "inconclusive", 1)
    if not measurement_passed or not valid_speedup:
        return ("measurement_invalid", "inconclusive", "measurement_invalid", "not_applicable", "inconclusive", 2)
    if not oracle_passed:
        return ("oracle_failed", "refuted", "oracle_failed", "not_applicable", "reusable_negative_boundary", 1)
    if speedup < 1.0 - margin:
        return ("valid", "refuted", "speedup_below_target", "not_attained", "reusable_negative_boundary", 4)
    if preserve_subtarget_evidence and 1.0 - margin <= speedup <= 1.0 + margin:
        return ("valid", "inconclusive", "speedup_within_noise", "not_attained", "inconclusive", 4)
    if speedup > 1.0 + margin:
        if retained is None:
            return ("valid", "inconclusive", "ablation_pending", "not_applicable", "inconclusive", 3)
        if not retained:
            return ("valid", "refuted", "ablation_not_retained", "not_applicable", "reusable_negative_boundary", 3)
        if speedup >= minimum:
            return ("valid", "supported", "verified", "attained", reuse, 4)
        if preserve_subtarget_evidence:
            return ("valid", "subtarget_valid", "speedup_below_target_valid_evidence", "not_attained", "reusable_observation", 4)
    return ("valid", "refuted", "speedup_below_target", "not_attained", "reusable_negative_boundary", 4)

def evaluate_rsi_candidate_gates(
    *, run_id: str, candidate_id: str, mutation_summary: str, conditions: dict | None = None,
    compile_ok: bool, oracle_passed: bool, measurement_passed: bool,
    speedup_ratio: float | None, min_speedup_ratio: float = DEFAULT_TARGET,
    ablation_retained: bool | None = True, failure_reason: str = "",
    hidden_input_summary: dict | None = None, ablation_receipt: dict | None = None,
    noise_margin: float = DEFAULT_NOISE_MARGIN, preserve_subtarget_evidence: bool = True,
    hygiene_ok: bool | None = None, workspace_delta_bytes: int | float | None = None,
    max_workspace_delta_bytes: int = 4096, guard_workloads=None,
    reusable_scope: bool | None = None, require_reusable_scope: bool = False,
) -> dict:
    """Evaluate the 4-stage RSI candidate verification funnel."""
    conds = dict(conditions or {})
    minimum, margin = _number(min_speedup_ratio, "min_speedup_ratio"), _number(noise_margin, "noise_margin", strict=False)
    if minimum < 1.0:
        raise ValueError("min_speedup_ratio must be >= 1.0")
    if speedup_ratio is not None and (isinstance(speedup_ratio, bool) or not isinstance(speedup_ratio, (int, float))):
        raise ValueError("speedup_ratio must be a number or null")
    raw_speedup = float(speedup_ratio) if speedup_ratio is not None else None
    valid_speedup = raw_speedup is not None and math.isfinite(raw_speedup) and raw_speedup > 0
    speedup = raw_speedup if valid_speedup else None
    valid_measurement = bool(compile_ok and measurement_passed and valid_speedup)
    hidden_failed = isinstance(hidden_input_summary, dict) and (
        hidden_input_summary.get("generalization_ok") is False or _flagged(hidden_input_summary, "opt_regression")
    )
    effective_oracle = bool(oracle_passed) and not hidden_failed
    resolved_ablation = _resolve_ablation_retained(ablation_retained, ablation_receipt)
    effective_ablation, gate3_reason, extra_failed = _gate3_status(
        resolved_ablation, hygiene_ok=hygiene_ok, workspace_delta_bytes=workspace_delta_bytes,
        max_workspace_delta_bytes=max_workspace_delta_bytes, guard_workloads=guard_workloads,
        reusable_scope=reusable_scope, require_reusable_scope=require_reusable_scope)
    validity, verdict, outcome, goal_attainment, knowledge_reuse, gate_reached = evaluate_rsi_axes(
        compile_ok=compile_ok, oracle_passed=effective_oracle, measurement_passed=valid_measurement,
        speedup=speedup, minimum=minimum, ablation_retained=effective_ablation,
        noise_margin=margin, preserve_subtarget_evidence=preserve_subtarget_evidence)
    if valid_measurement and effective_oracle and extra_failed and verdict in ("refuted", "inconclusive") and outcome in ("ablation_not_retained", "speedup_within_noise"):
        verdict, outcome, goal_attainment, knowledge_reuse, gate_reached = "refuted", gate3_reason, "not_applicable", "reusable_negative_boundary", 3
    gate1_reason = "compile_failed" if not compile_ok else (
        "deferred_to_gate2_measurement_guard" if (not valid_measurement and not effective_oracle)
        else ("hidden_input_generalization_failed" if hidden_failed else ("oracle_failed" if not oracle_passed else ""))
    )
    gates = {
        "gate1_compile_oracle": {"passed": bool(compile_ok and (effective_oracle or not valid_measurement)), "reason": gate1_reason, "oracle_verified": bool(compile_ok and valid_measurement and effective_oracle), "hidden_input_summary": dict(hidden_input_summary) if isinstance(hidden_input_summary, dict) else None},
        "gate2_measurement_guard": {"passed": valid_measurement, "reason": "" if valid_measurement else (failure_reason or "measurement_invalid")},
        "gate3_ablation": {"passed": bool(valid_measurement and effective_oracle and speedup is not None and speedup > 1.0 + margin and effective_ablation is True), "reason": gate3_reason, "ablation_receipt": dict(ablation_receipt) if isinstance(ablation_receipt, dict) else None},
        "gate4_distill_or_refute": {"passed": verdict in ("supported", "subtarget_valid"), "speedup_ratio": round(speedup, 6) if speedup is not None else None, "min_speedup_ratio": round(minimum, 6), "noise_margin": round(margin, 6), "goal_attained": goal_attainment == "attained"},
    }
    reason_text = failure_reason.strip() or (outcome if verdict != "supported" else "")
    conditional_avoid = [{"candidate_id": str(candidate_id), "run_id": str(run_id), "what_to_avoid": str(mutation_summary or candidate_id), "mutation_summary": str(mutation_summary), "conditions": conds, "reason": reason_text, "source_run_id": str(run_id)}] if (verdict == "refuted" and conds) else []
    return {
        "schema_version": SCHEMA_VERSION, "evaluation_schema": EVALUATION_SCHEMA, "run_id": str(run_id), "candidate_id": str(candidate_id), "mutation_summary": str(mutation_summary), "conditions": conds, "verdict": verdict, "outcome": outcome, "gate_reached": gate_reached,
        "axes": {"execution_validity": validity, "evidence_validity": validity, "goal_attainment": goal_attainment, "knowledge_reuse": knowledge_reuse, "reusability": knowledge_reuse},
        "speedup_ratio": round(speedup, 6) if speedup is not None else None, "min_speedup_ratio": round(minimum, 6), "noise_margin": round(margin, 6), "gates": gates, "distill_ready": verdict in ("supported", "subtarget_valid"), "conditional_avoid": conditional_avoid, "failure_reason": reason_text,
    }

def judge(evidence) -> dict:
    """Check raw trials, then evaluate the 4-stage RSI candidate verification gates."""
    checked = check_trials(evidence)
    entry = evaluate_rsi_candidate_gates(
        run_id=",".join(evidence.get("run_ids") or []), candidate_id=str(evidence.get("exp_id") or ""),
        mutation_summary=str(evidence.get("hypothesis") or ""), conditions=dict(evidence.get("scope_conditions") or {}),
        compile_ok=checked["compile_ok"], oracle_passed=checked["oracle_passed"],
        measurement_passed=checked["measurement_passed"], speedup_ratio=checked["speedup_ratio"],
        min_speedup_ratio=_number(evidence.get("target_speedup", DEFAULT_TARGET), "target_speedup"),
        noise_margin=_number(evidence.get("noise_margin", DEFAULT_NOISE_MARGIN), "noise_margin", strict=False),
        ablation_retained=_ablation_retained(evidence), failure_reason="; ".join(checked["problems"]),
        hidden_input_summary=evidence.get("hidden_input_summary"), ablation_receipt=evidence.get("ablation_receipt"),
        hygiene_ok=evidence.get("hygiene_ok", evidence.get("hygiene_passed")),
        workspace_delta_bytes=evidence.get("workspace_delta_bytes"),
        max_workspace_delta_bytes=evidence.get("max_workspace_delta_bytes", evidence.get("_max_workspace_delta_bytes", 4096)),
        guard_workloads=evidence.get("guard_workloads"), reusable_scope=evidence.get("reusable_scope"),
        require_reusable_scope=bool(evidence.get("require_reusable_scope", False)))
    entry.update(paired_rounds=checked["paired_rounds"], direction=checked["direction"])
    for k in ("source_digest", "baseline_source_digest", "baseline_exp_id"):
        if isinstance(evidence.get(k), str) and evidence[k].strip():
            entry[k] = evidence[k].strip()
    for k in ("baseline_metric", "candidate_metric"):
        if checked[k] is not None:
            entry[k] = round(checked[k], 6)
    if checked["problems"]:
        entry["evidence_problems"] = checked["problems"]
    return entry

def iter_ledger(path, *, task_id: str | None = None):
    """Stream JSONL ledger entries so callers retain only $O(\\text{Active State})$ in memory."""
    path = Path(path)
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8", newline="\n") as handle:
        for number, line in enumerate(handle, 1):
            if line.strip():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{number} is not valid JSON: {exc.msg}") from exc
                if task_id is None or (isinstance(rec, dict) and (not rec.get("task_id") or rec.get("task_id") == task_id)):
                    yield rec

def read_ledger(path) -> list[dict]:
    return list(iter_ledger(path))

@contextlib.contextmanager
def exclusive_lock(path):
    from mindie_knowledge.loop.locks import StartInProgress, StartLock
    lock = StartLock(str(path) + ".lock", wait=LOCK_WAIT_SECONDS)
    try:
        lock.acquire(write_record=False)
    except StartInProgress:
        raise TimeoutError(f"{path} is locked by another writer for more than {LOCK_WAIT_SECONDS:g}s") from None
    try:
        yield
    finally:
        lock.release()

def validate(record) -> dict:
    if not isinstance(record, dict):
        raise ValueError("record must be an object")
    for key in ("task_id", "exp_id", "source_digest", "verdict"):
        if not isinstance(record.get(key), str) or not record[key].strip():
            raise ValueError(f"{key} must be nonempty text")
    run_ids = record.get("run_ids")
    if not isinstance(run_ids, list) or any(not isinstance(r, str) or not r for r in run_ids):
        raise ValueError("run_ids must be an array of run_id strings")
    status = record.get("run_status")
    if status in INCONCLUSIVE_STATUSES and record["verdict"] != "inconclusive":
        raise ValueError(f"run_status {status!r} is missing evidence; verdict must be inconclusive, not " + record["verdict"])
    if record.get("card_seconds") is not None:
        _number(record["card_seconds"], "card_seconds", strict=False)
    if record.get("conditions") is not None and not isinstance(record["conditions"], dict):
        raise ValueError("conditions must be an object")
    return record

def fold(records) -> tuple[list[dict], dict]:
    """Apply `supersedes_exp_id` revisions in-place so experiment timeline order is preserved."""
    live, chain_seconds, last_raw_own, seen = {}, {}, {}, set()
    for raw_record in records:
        record, runs = dict(raw_record), set(raw_record.get("run_ids") or ())
        fresh, seen = bool(runs - seen), seen | runs
        own_val = float(record["card_seconds"]) if record.get("card_seconds") is not None else None
        prior, key = record.get("supersedes_exp_id"), record.get("exp_id") or record.get("candidate_id")
        carried, prev_raw = (chain_seconds.pop(prior), last_raw_own.pop(prior, None)) if (prior is not None and prior in live) else (None, None)
        if prior is not None and prior in live:
            aliases = [a for a in (live[prior].get("_superseded_ids") or ()) if a != key]
            if prior != key and prior not in aliases:
                aliases.append(prior)
            if aliases:
                record["_superseded_ids"] = aliases
            live = ({**live, key: record} if key == prior
                    else {(key if k == prior else k): (record if k == prior else v) for k, v in live.items() if k != key})
        else:
            live[key] = record
        seconds = (float(own_val or 0.0) if carried is None else (carried + float(own_val or 0.0)) if fresh
                   else carried if (own_val is None or (prev_raw is not None and own_val == prev_raw and carried > prev_raw) or own_val == carried) else own_val)
        if own_val is not None or carried is not None:
            record["card_seconds"] = seconds
        chain_seconds[key], last_raw_own[key] = seconds, (own_val if own_val is not None else prev_raw)
    return list(live.values()), dict(runs=len(seen), card_seconds=sum(chain_seconds.values()))

def _rank_supported_candidates(live: list[dict]) -> list[tuple[dict, float, float]]:
    """Compute cumulative speedup over explicit baseline/source lineage and rank kept candidates."""
    kept = [rec for rec in live if rec.get("verdict") in ("supported", "subtarget_valid")]
    by_exp, by_digest, non_kept_exps, non_kept_digests = {}, {}, set(), set()
    primary_ids = {(json.dumps(r.get("conditions") or {}, sort_keys=True), str(r.get("exp_id") or r.get("candidate_id") or "").strip()) for r in live}
    for rec in live:
        cond_key = json.dumps(rec.get("conditions") or {}, sort_keys=True)
        cid, src = str(rec.get("exp_id") or rec.get("candidate_id") or "").strip(), str(rec.get("source_digest") or "").strip()
        alias_ids = ([cid] if cid else []) + [old.strip() for old in (rec.get("_superseded_ids") or ()) if isinstance(old, str) and old.strip() and (cond_key, old.strip()) not in primary_ids]
        if rec.get("verdict") in ("supported", "subtarget_valid"):
            if src:
                by_digest.setdefault((cond_key, src), []).append(rec)
            for eid in alias_ids:
                by_exp.setdefault((cond_key, eid), []).append(rec)
        else:
            if src:
                non_kept_digests.add((cond_key, src))
            non_kept_exps.update((cond_key, eid) for eid in alias_ids)

    def step_of(rec: dict) -> float:
        raw = rec.get("speedup_ratio")
        return float(raw) if (not isinstance(raw, bool) and isinstance(raw, (int, float)) and math.isfinite(raw) and raw > 0) else 0.0

    def resolve_parent(rec: dict) -> tuple[str, dict | None]:
        cond_key, cid = json.dumps(rec.get("conditions") or {}, sort_keys=True), str(rec.get("exp_id") or rec.get("candidate_id") or "").strip()
        own_ids, cand_src = {cid, *(str(a).strip() for a in (rec.get("_superseded_ids") or ()))}, str(rec.get("source_digest") or "").strip()
        b_exp = rec.get("baseline_exp_id").strip() if isinstance(rec.get("baseline_exp_id"), str) else ""
        b_src = rec.get("baseline_source_digest").strip() if isinstance(rec.get("baseline_source_digest"), str) else ""
        if (b_exp and b_exp in own_ids) or (b_src and b_src == cand_src):
            return "invalid", None
        if b_exp:
            if (cond_key, b_exp) in non_kept_exps:
                return "invalid", None
            matches = [m for m in by_exp.get((cond_key, b_exp), []) if m is not rec]
            if len(matches) > 1:
                return "invalid", None
            if len(matches) == 1:
                p_src = str(matches[0].get("source_digest") or "").strip()
                return ("invalid", None) if (b_src and p_src and p_src != b_src) else ("ok", matches[0])
            return ("invalid", None) if (b_src and (by_digest.get((cond_key, b_src)) or (cond_key, b_src) in non_kept_digests)) else ("root", None)
        if b_src:
            matches = [m for m in by_digest.get((cond_key, b_src), []) if m is not rec]
            return ("invalid", None) if len(matches) > 1 else ("ok", matches[0]) if len(matches) == 1 else (("invalid", None) if (cond_key, b_src) in non_kept_digests else ("root", None))
        return "root", None

    def cumulative_of(rec: dict) -> float:
        seen_ids, curr, total = set(), rec, 1.0
        while curr is not None:
            rid, step = id(curr), step_of(curr)
            if step <= 0 or rid in seen_ids:
                return 0.0
            seen_ids.add(rid)
            total *= step
            kind, curr = resolve_parent(curr)
            if kind in ("invalid", "root"):
                return 0.0 if kind == "invalid" else total
        return total

    ranked = [(rec, step_of(rec), cum, str(rec.get("candidate_id") or rec.get("exp_id") or "")) for rec in kept if (cum := cumulative_of(rec)) > 0]
    ranked.sort(key=lambda item: (-round(item[2], 9), 0 if item[0].get("verdict") == "supported" else 1, -round(item[1], 9), item[3]))
    return [(rec, step, cum) for rec, step, cum, _ in ranked]

def _render_managed_progress_body(entries: list[dict], *, campaign_id: str, goal: str = "") -> str:
    live, _ = fold(e for e in entries if isinstance(e, dict) and (not e.get("task_id") or e.get("task_id") == campaign_id))
    ranked_kept, refuted, inconclusive = _rank_supported_candidates(live), [e for e in live if e.get("verdict") == "refuted"], [e for e in live if e.get("verdict") == "inconclusive"]
    lines = [PROGRESS_MARKER_START, "## Goal", "", goal or f"Optimize `{campaign_id}` under verified measurement and ablation gates.", "", "## Best Supported Candidate", ""]
    if ranked_kept:
        best, step_ratio, cum_ratio = ranked_kept[0]
        cid, cum_note = best.get("candidate_id") or best.get("exp_id"), (f", cumulative `{cum_ratio:.4f}x`" if abs(cum_ratio - step_ratio) > 1e-6 else "")
        lines.append(f"- candidate: `{cid}` (`{best.get('verdict')}`, `{step_ratio:.4f}x`{cum_note}) — {best.get('mutation_summary', '')}")
    else:
        lines.append("- None yet.")
    lines.extend(["", "## Active Hypotheses", ""])
    lines.extend([f"- `{item.get('candidate_id') or item.get('exp_id')}` ({item.get('verdict')}): {item.get('mutation_summary', '')} (`{s:.4f}x`{f', cumulative `{c:.4f}x`' if abs(c - s) > 1e-6 else ''})" for item, s, c in ranked_kept[:3]] or ["- Establish first verified candidate passing Gates 1-3."])
    lines.extend(["", "## Refuted Directions", ""])
    refuted_lines = []
    for item in refuted[-10:]:
        avoids = item.get("conditional_avoid") or ([{"candidate_id": item.get("candidate_id") or item.get("exp_id"), "run_id": item.get("run_id"), "conditions": item.get("conditions") or {}, "reason": item.get("failure_reason") or item.get("outcome") or "refuted"}] if item.get("conditions") else [])
        for avoid in avoids:
            target_id, run_ref = avoid.get("candidate_id") or item.get("candidate_id") or item.get("exp_id") or avoid.get("what_to_avoid"), avoid.get("run_id") or item.get("run_id") or "unknown"
            reason = avoid.get("reason") or item.get("failure_reason") or item.get("outcome") or "refuted"
            refuted_lines.append(f"- avoid {target_id} when {json.dumps(avoid.get('conditions', {}), sort_keys=True, ensure_ascii=False)}: {reason} (run `{run_ref}`)")
    lines.extend(refuted_lines or ["- None recorded."])
    lines.extend(["", "## Experiments To Complete", ""])
    lines.extend([f"- `{item.get('candidate_id') or item.get('exp_id')}`: outcome=`{item.get('outcome')}` ({item.get('failure_reason') or 're-verify measurement/ablation'})" for item in inconclusive[-5:]] or ["- None pending."])
    lines.append(PROGRESS_MARKER_END)
    return "\n".join(lines)

def _extract_embedded_recovery_paragraphs(section_body: str) -> list[str]:
    text = section_body.strip()
    if not text or not RECOVERY_TOKEN_RE.search(text):
        return []
    lines, segments, idx = text.splitlines(), [], 0
    while idx < len(lines):
        line, stripped = lines[idx], lines[idx].strip()
        if not stripped:
            idx += 1
        elif stripped.startswith("```"):
            buf, idx = [line], idx + 1
            while idx < len(lines) and not buf[-1].strip().startswith("```") or len(buf) == 1:
                if idx >= len(lines):
                    break
                buf.append(lines[idx])
                idx += 1
                if buf[-1].strip().startswith("```"):
                    break
            segments.append(("\n".join(buf).strip(), True))
        elif stripped.startswith(("{", "[")):
            buf, depth, idx = [line], line.count("{") + line.count("[") - line.count("}") - line.count("]"), idx + 1
            while idx < len(lines) and depth > 0:
                buf.append(lines[idx])
                depth += lines[idx].count("{") + lines[idx].count("[") - lines[idx].count("}") - lines[idx].count("]")
                idx += 1
            segments.append(("\n".join(buf).strip(), True))
        elif re.match(r"(?i)^in_flight_runs\b", stripped):
            buf, idx = [line], idx + 1
            while idx < len(lines) and lines[idx].strip() and not lines[idx].strip().startswith("```"):
                buf.append(lines[idx])
                idx += 1
            segments.append(("\n".join(buf).strip(), True))
        elif RECOVERY_TOKEN_RE.search(line):
            base_indent, buf, idx = len(line) - len(line.lstrip(" \t")), [line], idx + 1
            while idx < len(lines) and lines[idx].strip() and not lines[idx].strip().startswith(("```", "{", "[")) and (len(lines[idx]) - len(lines[idx].lstrip(" \t"))) > base_indent:
                buf.append(lines[idx])
                idx += 1
            segments.append(("\n".join(buf).strip(), True))
        else:
            segments.append((stripped, False))
            idx += 1
    rescued, s_idx = [], 0
    while s_idx < len(segments):
        seg_text, _ = segments[s_idx]
        if (seg_text.startswith("#") or seg_text.endswith(":")) and s_idx + 1 < len(segments) and (RECOVERY_TOKEN_RE.search(seg_text) or RECOVERY_TOKEN_RE.search(segments[s_idx + 1][0])):
            next_text, next_atomic = segments[s_idx + 1]
            rescued.append(f"{seg_text}{chr(10) if next_atomic and next_text.startswith(('```', '{', '[', '-')) else chr(10) * 2}{next_text}")
            s_idx += 2
        else:
            if RECOVERY_TOKEN_RE.search(seg_text):
                rescued.append(seg_text)
            s_idx += 1
    return [r for r in rescued if r]

def _merge_progress(existing_text: str | None, managed_body: str, *, campaign_id: str) -> str:
    title = f"# RSI Progress: {campaign_id}"
    if not existing_text or not existing_text.strip():
        return f"{title}\n\n{managed_body}\n"
    cleaned = "\n".join(line for line in existing_text.splitlines() if line.strip() not in (PROGRESS_MARKER_START, PROGRESS_MARKER_END)) + "\n"
    matches = list(re.finditer(r"(?m)^## +([^\n]+)\n", cleaned))
    preamble_lines = [ln for ln in (cleaned[:matches[0].start()] if matches else cleaned).splitlines() if not ln.strip().startswith("# RSI Progress:")]
    preamble_blocks, preserved_sections = [b.strip() for b in re.split(r"\n\s*\n", "\n".join(preamble_lines).strip()) if b.strip()], []
    for idx, match in enumerate(matches):
        heading, sec_end = match.group(1).strip(), matches[idx + 1].start() if idx + 1 < len(matches) else len(cleaned)
        if heading not in MANAGED_PROGRESS_HEADINGS:
            preserved_sections.append(cleaned[match.start():sec_end].strip())
        else:
            preamble_blocks.extend(r for r in _extract_embedded_recovery_paragraphs(cleaned[match.end():sec_end]) if r not in preamble_blocks)
    return "\n\n".join([title] + preamble_blocks + preserved_sections + [managed_body]) + "\n"

def render_progress(entries: list[dict], *, campaign_id: str, goal: str = "", existing_text: str | None = None) -> str:
    """Render `PROGRESS.md` for `campaign_id`, preserving recovery sections when present."""
    return _merge_progress(existing_text, _render_managed_progress_body(entries, campaign_id=campaign_id, goal=goal), campaign_id=campaign_id)

def _write_progress_atomic(progress_path: Path, entries: list[dict], *, campaign_id: str, goal: str = "") -> None:
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_progress(entries, campaign_id=campaign_id, goal=goal, existing_text=progress_path.read_text(encoding="utf-8") if progress_path.is_file() else None)
    temp_path = progress_path.with_name(progress_path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(rendered)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, progress_path)

def render_progress_file(path, progress=None, *, task_id: str, goal: str = "") -> dict:
    """Re-render `PROGRESS.md` from `experiment-ledger.jsonl` under the ledger lock."""
    path, progress_path = Path(path), Path(progress) if progress else Path(path).parent / "PROGRESS.md"
    with exclusive_lock(path):
        live, _ = fold(iter_ledger(path, task_id=task_id))
        _write_progress_atomic(progress_path, live, campaign_id=task_id, goal=goal)
    return dict(status="rendered", task_id=task_id, progress=str(progress_path))

def same_committed_experiment(existing: dict, candidate_record: dict) -> bool:
    keys = (set(existing) | set(candidate_record)) - LEARN_MUTABLE_KEYS
    return all(existing.get(k) == candidate_record.get(k) for k in keys)

def _merge_idempotent_retry(existing: dict, entry: dict) -> tuple[dict, bool]:
    if not same_committed_experiment(existing, entry):
        raise ValueError(f"exp_id {entry['exp_id']!r} already recorded with conflicting evidence")
    old_lesson = existing.get("lesson") if isinstance(existing.get("lesson"), dict) else {}
    new_lesson = entry.get("lesson") if isinstance(entry.get("lesson"), dict) else {}
    old_distill, new_distill, old_note, new_note = old_lesson.get("distill"), new_lesson.get("distill"), old_lesson.get("note"), new_lesson.get("note")
    if old_distill is not None and new_distill is not None and old_distill != new_distill:
        raise ValueError(f"exp_id {entry['exp_id']!r} already recorded with conflicting distill receipt")
    if old_note is not None and new_note is not None and old_note != new_note:
        raise ValueError(f"exp_id {entry['exp_id']!r} already recorded with conflicting note")
    for flag in ("new_observation", "divergent_layer_identified"):
        if flag in existing and flag in entry and existing[flag] != entry[flag]:
            raise ValueError(f"exp_id {entry['exp_id']!r} already recorded with conflicting {flag}")
    eff_distill = new_distill if new_distill is not None else old_distill
    eff_note = new_note if new_note is not None else old_note
    needs_revision = (eff_distill != old_distill or eff_note != old_note)
    merged = {k: v for k, v in existing.items() if k != "_superseded_ids"}
    merged["lesson"] = dict(distill=eff_distill, note=eff_note)
    for flag in ("new_observation", "divergent_layer_identified"):
        if flag in entry and existing.get(flag) != entry[flag]:
            merged[flag], needs_revision = entry[flag], True
    return merged, needs_revision

def append(path, record, *, progress=None, goal="", allow_idempotent_retry: bool = False) -> dict:
    """Validate, commit the ledger line, and atomically update `PROGRESS.md`."""
    entry = {k: v for k, v in dict(validate(record)).items() if k != "_superseded_ids"}
    entry.setdefault("schema_version", SCHEMA_VERSION)
    path, progress_path = Path(path), Path(progress) if progress else Path(path).parent / "PROGRESS.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    progress_warning, recovered, revised = None, False, False
    with exclusive_lock(path):
        live, _ = fold(r for r in iter_ledger(path, task_id=entry["task_id"]) if r.get("task_id") == entry["task_id"] and r.get("exp_id"))
        existing = next((r for r in reversed(live) if r.get("exp_id") == entry["exp_id"]), None)
        should_write = True
        if entry.get("supersedes_exp_id") is None and existing is not None:
            if not allow_idempotent_retry:
                raise ValueError(f"exp_id {entry['exp_id']!r} already recorded; set supersedes_exp_id to revise it")
            merged, needs_revision = _merge_idempotent_retry(existing, entry)
            record.clear()
            record.update(merged)
            recovered = True
            entry, revised, should_write = (dict(merged, supersedes_exp_id=entry["exp_id"]), True, True) if needs_revision else (merged, False, False)
        if should_write:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            live, _ = fold([*live, entry])
        try:
            _write_progress_atomic(progress_path, live, campaign_id=entry["task_id"], goal=goal)
        except TimeoutError as exc:
            progress_warning = f"TimeoutError: {exc}"  # Principle 9 & 14: explicit TimeoutError
        except (OSError, ValueError) as exc:
            progress_warning = f"{type(exc).__name__}: {exc}"
    result = dict(status="recorded", exp_id=entry["exp_id"], verdict=entry["verdict"], progress=str(progress_path))
    if recovered:
        result["recovered_append"] = True
    if revised:
        result["revised_append"] = True
    if progress_warning is not None:
        result.update(progress_unconfirmed=True, progress_warning=progress_warning)
    return result

def gate(path, *, task_id, source_digest, scope=None, max_runs=None, max_card_seconds=None):
    """Decide before a candidate goes to the device: submit, duplicate or budget_exhausted."""
    live, spent = fold(r for r in iter_ledger(path, task_id=task_id) if r.get("task_id") == task_id and r.get("exp_id"))
    spent.update(max_runs=max_runs, max_card_seconds=max_card_seconds)
    for record in reversed(live):
        settled = (record.get("verdict") in SETTLED
                   or (record.get("verdict") == "inconclusive" and record.get("outcome") == "speedup_within_noise"
                       and record.get("run_status") not in INCONCLUSIVE_STATUSES))
        if record.get("source_digest") == source_digest and settled and (record.get("conditions") or {}) == (scope or {}):
            return dict(decision="duplicate", spent=spent,
                        prior=dict(exp_id=record.get("exp_id"), verdict=record["verdict"], outcome=record.get("outcome"), run_ids=record.get("run_ids")),
                        reason="this exact source was already judged under the same conditions")
    if max_runs is not None and spent["runs"] >= max_runs:
        return dict(decision="budget_exhausted", spent=spent, reason=f"{spent['runs']} device runs recorded; budget is {max_runs}")
    if max_card_seconds is not None and spent["card_seconds"] >= max_card_seconds:
        return dict(decision="budget_exhausted", spent=spent, reason=f"{spent['card_seconds']:g} card-seconds recorded; budget is {max_card_seconds:g}")
    return dict(decision="submit", spent=spent)

def _stdin_json():
    raw = sys.stdin.buffer.read(ARGUMENT_LIMIT + 1)
    if len(raw) > ARGUMENT_LIMIT:
        raise ValueError("input exceeds 1 MiB")
    return json.loads(raw)

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="experiment_ledger.py")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("fingerprint").add_argument("repo")
    commands.add_parser("judge")
    for name, need_task in (("append", False), ("render", True)):
        sub = commands.add_parser(name)
        sub.add_argument("--ledger", required=True)
        if need_task:
            sub.add_argument("--task-id", required=True)
        sub.add_argument("--progress")
        sub.add_argument("--goal", default="")
    check = commands.add_parser("gate")
    for flag in ("--ledger", "--task-id", "--source-digest"):
        check.add_argument(flag, required=True)
    check.add_argument("--scope", type=json.loads)
    check.add_argument("--max-runs", type=int)
    check.add_argument("--max-card-seconds", type=float)
    args = parser.parse_args(argv)
    try:
        if args.command == "fingerprint":
            result = fingerprint(args.repo)
        elif args.command == "judge":
            result = judge(_stdin_json())
        elif args.command == "append":
            result = append(args.ledger, _stdin_json(), progress=args.progress, goal=args.goal)
        elif args.command == "render":
            result = render_progress_file(args.ledger, args.progress, task_id=args.task_id, goal=args.goal)
        else:
            result = gate(args.ledger, task_id=args.task_id, source_digest=args.source_digest,
                          scope=args.scope, max_runs=args.max_runs, max_card_seconds=args.max_card_seconds)
        code = BLOCKED if result.get("decision") in ("duplicate", "budget_exhausted") else 0
    except TimeoutError as exc:
        result, code = dict(status="error", error=f"TimeoutError: {exc}"), 1
    except (ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        result, code = dict(status="error", error=f"{type(exc).__name__}: {exc}"), 1
    sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return code

if __name__ == "__main__":
    raise SystemExit(main())
