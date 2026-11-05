"""Ledger-backed RSI optimization loop: composable actions with experiment-ledger authority.

[Role]
Coordinates `profile`, `consult`, `propose`, `measure`, `learn`, and one-shot `record`
over `experiment-ledger.jsonl`. MCP steps stay with the Agent; `propose` fingerprints
and gates through `experiment_ledger`, `measure` judges through `experiment_ledger.judge`
(`evaluate_rsi_candidate_gates`), `learn` / `record` atomically commit through
`experiment_ledger.append` (ledger line + `PROGRESS.md` under `<ledger>.lock`),
and `_stagnation` asks `assess_stagnation` whether the next round must explore.

[Invariants]
- `experiment-ledger.jsonl` is the authoritative fact store: `init` reconciles
  existing ledger history (`baseline`, `rounds`, `pending_distill`, `mode`), and
  nonlinear pre-measurement transitions (`profile`, `consult`, `propose`, or one-shot
  `record`) never self-lock the session, while `measure` and `learn` still require
  their respective gated candidate / judged entry prerequisites.
- A candidate reaches the device only after `gate` returns `submit`; a duplicate
  stays at `propose`, an exhausted budget finishes the loop.
- `measure` re-fingerprints the candidate and refuses evidence whose source
  changed after the gate. An execution-failure `run_status` is always `inconclusive`.
- `supported` / `subtarget_valid` moves the baseline; otherwise the next round
  reuses the profile and starts at `consult`.

[Timeouts & Locks]
- State changes hold `<state>.lock` (`experiment_ledger.exclusive_lock`, `5.0s`),
  and ledger commits hold `<ledger>.lock` (`5.0s`).

[Side-Effect Policy]
- Writes only the state file, its lock, and the ledger + `PROGRESS.md` through
  `experiment_ledger.append`.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

try:
    from . import experiment_ledger as ledger
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import experiment_ledger as ledger  # type: ignore[no-redef]

SCHEMA = "mindie-rsi-loop/2"
STEPS = ("profile", "consult", "propose", "measure", "learn", "record")
KEPT = frozenset({"supported", "subtarget_valid"})  # distill_ready RSI verdicts
BLOCKED = ledger.BLOCKED  # source: exit code for a blocked gate decision in experiment_ledger.py
STALL_WINDOW = 3  # source: knowledge engine assess_stagnation default window (SKILL.md)
MEASURE_OPTIONAL = (
    "ablation_retained", "ablation_ok", "ablation_passed", "ablation",
    "ablation_receipt", "hidden_input_summary", "noise_margin",
    "hygiene_ok", "hygiene_passed", "workspace_delta_bytes",
    "max_workspace_delta_bytes", "guard_workloads",
    "reusable_scope", "require_reusable_scope",
)
_same_committed_experiment = ledger.same_committed_experiment

GUIDE = {
    "profile": ("Hold an exclusive coordinator lease and run mindie_profiler with an explicit "
                "--time-window/--step-window/--full-range on the current baseline. Complete with "
                '{"bottleneck_summary": {...}, "profile_ref": "<receipt or path>"}.'),
    "consult": ("When /mindie-agent knowledge is enabled and an information gap remains, call "
                "knowledge_query(query=<bottleneck>, mode=<mode>) and read the cards you will "
                "rely on with knowledge_explain. In explore mode pass native_subcontext=True, "
                "ledger_entries and bottleneck_summary, and seed a fresh subagent only with "
                "exploration_context.subagent_fact_pack.prompt_brief. Complete with "
                '{"mode": <mode>, "unit_refs": [{"unit_id", "revision"}]}, or with '
                '{"mode": <mode>, "unit_refs": [], "skipped_reason": "<why no query>"}.'),
    "propose": ("Change the candidate checkout for exactly one hypothesis. Prefer a separate git "
                "worktree for the candidate; if it shares the baseline checkout, first restore "
                "that checkout to the baseline source. Complete with "
                '{"exp_id", "hypothesis", "candidate_repo", "scope_conditions": {...}, '
                '"factors": [...]}; the loop fingerprints it and runs the ledger gate. '
                "Exit 3 means do not submit."),
    "measure": ("Submit baseline and candidate through coordinator/remote-dev on the same device, "
                "alternating AB/BA, and register every run_id in PROGRESS.md in_flight_runs. If "
                "baseline and candidate share one checkout, leave it at the candidate code before "
                "completing: the loop re-fingerprints it. Complete with "
                '{"run_ids", "baseline": [{"status", "metric"}], "candidate": [...], '
                '"correctness": "passed|failed", "compile_ok", "run_status", "card_seconds"} '
                "plus any of ablation_retained, ablation_receipt (required verdict input when "
                "several factors changed), hidden_input_summary, noise_margin."),
    "learn": ("Optionally keep the lesson: pass the knowledge_distill receipt as "
              '{"distill": {...}} and/or a short {"note": "..."}; new_observation / '
              "divergent_layer_identified (booleans) count as progress for the stagnation "
              "check. A kept gain without distill stays in pending_distill."),
}


# ------------------------------------------------------------------------- state
def _load(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"{path} does not exist; run init first")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema") != SCHEMA:
        raise ValueError(f"{path} is not a {SCHEMA} state file")
    return state


def _save(path: Path, state: dict) -> None:
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _positive(value, label, *, integer=False, optional=False):
    if value is None and optional:
        return None
    if integer:
        if type(value) is not int or value < 1:
            raise ValueError(f"{label} must be a positive integer")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be a positive number")
    return float(value)


def _text(body, key) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be nonempty text")
    return value.strip()


def _state_paths(state: dict, state_path: Path | None = None) -> tuple[str, ...]:
    paths = [state["ledger"], state["progress"]]
    if state_path is not None:
        paths.append(str(state_path))
    elif state.get("_state_path"):
        paths.append(str(state["_state_path"]))
    return tuple(paths)


def _initial_baseline(state: dict, live: list[dict] | None = None) -> dict:
    init_base = state.get("initial_baseline")
    if isinstance(init_base, dict) and init_base.get("repo") and init_base.get("source_digest"):
        return dict(repo=init_base["repo"], source_digest=init_base["source_digest"], exp_id=None)
    if live:
        first_src = live[0].get("baseline_source_digest")
        if first_src:
            return dict(repo=state["baseline"]["repo"], source_digest=first_src, exp_id=None)
    return dict(repo=state["baseline"]["repo"], source_digest=state["baseline"]["source_digest"], exp_id=None)


def _expected_baseline(state: dict, live: list[dict]) -> dict:
    init_base = _initial_baseline(state, live)
    if not live:
        return init_base
    ranked = ledger._rank_supported_candidates(live)
    if ranked:
        best_rec, _, _ = ranked[0]
        return dict(
            repo=best_rec.get("candidate_repo") or init_base["repo"],
            source_digest=best_rec.get("source_digest") or init_base["source_digest"],
            exp_id=best_rec.get("exp_id") or best_rec.get("candidate_id"),
        )
    return init_base


def _ledger_out_of_sync(state: dict, live: list[dict]) -> bool:
    rounds = state.get("rounds") or []
    if len(live) != len(rounds):
        return True
    for r_state, rec in zip(rounds, live):
        if (r_state.get("exp_id") != rec.get("exp_id")
                or r_state.get("verdict") != rec.get("verdict")
                or r_state.get("outcome") != rec.get("outcome")
                or r_state.get("speedup_ratio") != rec.get("speedup_ratio")):
            return True
    if live and state.get("baseline") != _expected_baseline(state, live):
        return True
    expected_pending = []
    for idx, rec in enumerate(live, 1):
        verdict = rec.get("verdict")
        lesson = rec.get("lesson") if isinstance(rec.get("lesson"), dict) else {}
        if verdict in KEPT and lesson.get("distill") is None:
            expected_pending.append(dict(round=idx, exp_id=rec.get("exp_id"),
                                         reason=lesson.get("note") or "not distilled yet"))
    if (state.get("pending_distill") or []) != expected_pending:
        return True
    return False


def _sync_from_ledger(state: dict, *, preserve_step: bool = False) -> None:
    """Reconcile state with pre-existing `experiment-ledger.jsonl` records."""
    ledger_path = Path(state["ledger"])
    if not ledger_path.is_file():
        return
    live, _ = ledger.fold(r for r in ledger.iter_ledger(ledger_path, task_id=state["task_id"])
                          if r.get("task_id") == state["task_id"] and r.get("exp_id"))
    if not live:
        return
    stagnation = _stagnation(state)
    state["baseline"] = _expected_baseline(state, live)
    prior_stag = {
        r.get("exp_id"): r.get("stagnation")
        for r in (state.get("rounds") or [])
        if isinstance(r, dict) and r.get("exp_id")
    }
    rounds, pending = [], []
    for idx, rec in enumerate(live, 1):
        verdict = rec.get("verdict")
        exp_id = rec.get("exp_id")
        stag_status = stagnation.get("status") if idx == len(live) else (prior_stag.get(exp_id) or stagnation.get("status"))
        rounds.append(dict(round=idx, exp_id=exp_id, verdict=verdict,
                           outcome=rec.get("outcome"), speedup_ratio=rec.get("speedup_ratio"),
                           stagnation=stag_status))
        lesson = rec.get("lesson") if isinstance(rec.get("lesson"), dict) else {}
        if verdict in KEPT and lesson.get("distill") is None:
            pending.append(dict(round=idx, exp_id=exp_id,
                                reason=lesson.get("note") or "not distilled yet"))
    state["rounds"] = rounds
    state["round"] = len(rounds) + 1
    state["pending_distill"] = pending
    explore_disabled = os.environ.get("MINDIE_DISABLE_RSI_EXPLORE") == "1"
    state["mode"] = "explore" if (stagnation.get("stalled") and not explore_disabled) else "reuse"
    if state["max_rounds"] is not None and len(rounds) >= state["max_rounds"]:
        _finish(state, f"max_rounds {state['max_rounds']} reached")
    elif not preserve_step and state["status"] == "running" and state.get("step") != "learn":
        prof_ref = state["current"].get("profile_ref")
        summary = state["current"].get("bottleneck_summary")
        if live[-1].get("verdict") not in KEPT and prof_ref and summary:
            state["step"] = "consult"
            state["current"] = dict(
                skipped=[], bottleneck_summary=summary,
                profile_ref=prof_ref,
                profile_reused_from=state["current"].get("profile_reused_from") or len(rounds))
        else:
            state["step"] = "profile"
            state["current"] = dict(skipped=[])


def init(path, *, task_id, ledger_path, baseline_repo, direction, repetitions,
         target_speedup=ledger.DEFAULT_TARGET, max_rounds=None, max_runs=None,
         max_card_seconds=None, progress=None, goal=None) -> dict:
    path = Path(path).resolve()
    if direction not in ("maximize", "minimize"):
        raise ValueError("direction must be maximize or minimize")
    ledger_path = Path(ledger_path).resolve()
    progress_path = Path(progress).resolve() if progress else ledger_path.parent / "PROGRESS.md"
    baseline = ledger.fingerprint(baseline_repo, exclude_paths=(path, ledger_path, progress_path))
    initial_baseline = dict(repo=str(Path(baseline_repo).resolve()),
                            source_digest=baseline["source_digest"], exp_id=None)
    state = dict(
        schema=SCHEMA, task_id=task_id, ledger=str(ledger_path),
        progress=str(progress_path),
        goal=goal or f"Optimize {task_id}",
        direction=direction, repetitions=_positive(repetitions, "repetitions", integer=True),
        target_speedup=_positive(target_speedup, "target_speedup"),
        max_rounds=_positive(max_rounds, "max_rounds", integer=True, optional=True),
        max_runs=_positive(max_runs, "max_runs", integer=True, optional=True),
        max_card_seconds=_positive(max_card_seconds, "max_card_seconds", optional=True),
        initial_baseline=initial_baseline,
        baseline=dict(initial_baseline),
        round=1, step="profile", mode="reuse", status="running", finish_reason=None,
        current=dict(skipped=[]), rounds=[], pending_distill=[])
    _sync_from_ledger(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    with ledger.exclusive_lock(path):
        if path.exists():
            raise ValueError(f"{path} already exists; use next to resume it")
        _save(path, state)
    return _render_step(state)


def _render_step(state: dict) -> dict:
    if state["status"] != "running":
        result = dict(status=state["status"], finish_reason=state["finish_reason"],
                      rounds=len(state["rounds"]), progress=state["progress"],
                      baseline=state["baseline"],
                      pending_distill=state.get("pending_distill") or [])
        if state.get("progress_unconfirmed"):
            result.update(progress_unconfirmed=True, progress_warning=state.get("progress_warning"),
                          receipt_unconfirmed=True, receipt_warning=state.get("progress_warning"))
        return result
    guide = GUIDE[state["step"]].replace("<mode>", repr(state["mode"]))
    result = dict(status="running", round=state["round"], step=state["step"],
                  mode=state["mode"], guide=guide, progress=state["progress"],
                  baseline_source_digest=state["baseline"]["source_digest"],
                  baseline=state["baseline"])
    if state["step"] == "learn":
        result["verdict"] = state["current"]["entry"]["verdict"]
    if state["current"].get("skipped"):
        result["skipped_this_round"] = state["current"]["skipped"]
    if state["current"].get("profile_reused_from") is not None:
        result["profile_reused_from_round"] = state["current"]["profile_reused_from"]
    if state.get("restore_hint"):
        result["restore_hint"] = state["restore_hint"]
    if state.get("pending_distill"):
        result["pending_distill"] = state["pending_distill"]
    if state.get("progress_unconfirmed"):
        result.update(progress_unconfirmed=True, progress_warning=state.get("progress_warning"),
                      receipt_unconfirmed=True, receipt_warning=state.get("progress_warning"))
    return result


def next_step(path) -> dict:
    state = _load(Path(path).resolve())
    if state["status"] == "running" and state["step"] != "learn":
        live, _ = ledger.fold(r for r in ledger.iter_ledger(state["ledger"], task_id=state["task_id"])
                              if r.get("task_id") == state["task_id"] and r.get("exp_id"))
        if _ledger_out_of_sync(state, live):
            _sync_from_ledger(state, preserve_step=state["step"] in ("consult", "propose", "measure"))
    return _render_step(state)


# ------------------------------------------------------------------------- steps
def _profile(state, body):
    if not isinstance(body.get("bottleneck_summary"), dict) or not body["bottleneck_summary"]:
        raise ValueError("bottleneck_summary must be a nonempty object from the profiler")
    state["current"].update(bottleneck_summary=body["bottleneck_summary"],
                            profile_ref=_text(body, "profile_ref"))
    state["current"].pop("profile_reused_from", None)
    return "consult", {}


def _consult(state, body):
    req_mode = body.get("mode")
    explore_disabled = os.environ.get("MINDIE_DISABLE_RSI_EXPLORE") == "1"
    if explore_disabled and req_mode == "explore":
        raise ValueError("RSI explore mode is disabled in this reuse-only profile; query knowledge in 'reuse' mode")
    if (
        req_mode == "explore"
        and state["mode"] == "reuse"
        and not explore_disabled
        and any(r.get("verdict") not in KEPT for r in state.get("rounds", ()))
    ):
        state["mode"] = "explore"
    if req_mode != state["mode"]:
        raise ValueError(f"this round must query knowledge in {state['mode']!r} mode")
    refs = body.get("unit_refs")
    if not isinstance(refs, list) or any(
            not isinstance(r, dict) or not r.get("unit_id") or type(r.get("revision")) is not int
            for r in refs):
        raise ValueError("unit_refs must be an array of {unit_id, revision} objects")
    skipped = body.get("skipped_reason")
    if skipped is not None and (not isinstance(skipped, str) or not skipped.strip()):
        raise ValueError("skipped_reason must be nonempty text when given")
    state["current"].update(unit_refs=refs, knowledge_mode=state["mode"],
                            knowledge_skipped=skipped and skipped.strip())
    return "propose", {}


def _propose(state, body):
    current = state["current"]
    if state["mode"] == "explore" and current.get("knowledge_mode") != "explore":
        raise ValueError("this round is in 'explore' mode after stagnation; complete 'consult' with mode='explore' before proposing")
    hypothesis, exp_id = _text(body, "hypothesis"), _text(body, "exp_id")
    scope = body.get("scope_conditions")
    if scope is not None and not isinstance(scope, dict):
        raise ValueError("scope_conditions must be an object")
    factors = body.get("factors")
    if factors is not None and (not isinstance(factors, list)
                                or any(not isinstance(f, str) or not f for f in factors)):
        raise ValueError("factors must be an array of factor names")
    repo = str(Path(_text(body, "candidate_repo")).resolve())
    digest = ledger.fingerprint(repo, exclude_paths=_state_paths(state))["source_digest"]
    if digest == state["baseline"]["source_digest"]:
        raise ValueError("candidate source equals the baseline; change the code before proposing")
    state.pop("restore_hint", None)
    for stale_key in ("entry", "trials", "run_ids", "run_status", "card_seconds", "receipt_refs"):
        current.pop(stale_key, None)
    decision = ledger.gate(state["ledger"], task_id=state["task_id"], source_digest=digest,
                           scope=scope, max_runs=state["max_runs"],
                           max_card_seconds=state["max_card_seconds"])
    attempt = dict(exp_id=exp_id, hypothesis=hypothesis, source_digest=digest,
                   decision=decision["decision"], reason=decision.get("reason"),
                   prior=decision.get("prior"))
    if decision["decision"] == "duplicate":
        current["skipped"].append(attempt)
        return "propose", dict(gate=decision, blocked=True)
    if decision["decision"] == "budget_exhausted":
        current["skipped"].append(attempt)
        _finish(state, "budget_exhausted: " + decision["reason"])
        return None, dict(gate=decision, blocked=True)
    current["candidate"] = dict(exp_id=exp_id, hypothesis=hypothesis, repo=repo,
                                source_digest=digest, scope_conditions=scope,
                                factors=factors, spent=decision["spent"])
    return "measure", dict(gate=decision)


def _measure(state, body):
    candidate = state["current"]["candidate"]
    now = ledger.fingerprint(candidate["repo"], exclude_paths=_state_paths(state))["source_digest"]
    if now != candidate["source_digest"]:
        state["current"].pop("candidate")
        return "propose", dict(error="candidate source changed after the gate; the measured "
                                     "code is not the gated code. Propose it again.")
    run_ids = body.get("run_ids")
    if not isinstance(run_ids, list) or not run_ids \
            or any(not isinstance(r, str) or not r for r in run_ids):
        raise ValueError("run_ids must be a nonempty array of run_id strings")
    seconds = body.get("card_seconds")
    if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                                or not math.isfinite(seconds) or seconds < 0):
        raise ValueError("card_seconds must be a nonnegative number")
    status = body.get("run_status") or "completed"
    base_info = state["current"].get("baseline_override") or state["baseline"]
    evidence = dict(repetitions=state["repetitions"], direction=state["direction"],
                    target_speedup=state["target_speedup"],
                    baseline=body.get("baseline"), candidate=body.get("candidate"),
                    correctness=body.get("correctness"), compile_ok=body.get("compile_ok"),
                    run_status=status,
                    baseline_source_digest=base_info["source_digest"],
                    source_digest=candidate["source_digest"],
                    scope_conditions=candidate["scope_conditions"],
                    run_ids=run_ids, exp_id=candidate["exp_id"],
                    hypothesis=candidate["hypothesis"], factors=candidate["factors"])
    for key in MEASURE_OPTIONAL:
        if key in body:
            evidence[key] = body[key]
    entry = ledger.judge(evidence)
    state["current"].update(
        run_ids=run_ids, run_status=status, card_seconds=seconds,
        receipt_refs=body.get("receipt_refs"),
        trials=dict(baseline=evidence["baseline"], candidate=evidence["candidate"],
                    correctness=evidence["correctness"], compile_ok=evidence["compile_ok"]),
        entry=entry)
    return "learn", dict(entry=entry)


def _route_signature(record: dict) -> tuple[str, tuple[str, ...]]:
    cond_key = json.dumps(record.get("conditions") or {}, sort_keys=True)
    units = tuple(sorted(
        json.dumps(u, sort_keys=True) for u in (record.get("unit_refs") or [])
        if isinstance(u, dict) and u.get("unit_id")
    ))
    return cond_key, units


def _stagnation(state) -> dict:
    from mindie_knowledge.graded.explore import assess_stagnation
    live, _ = ledger.fold(r for r in ledger.iter_ledger(state["ledger"], task_id=state["task_id"])
                          if r.get("task_id") == state["task_id"])
    if not live:
        return assess_stagnation([], consecutive_limit=STALL_WINDOW)
    target_sig = _route_signature(live[-1])
    same_route = []
    for r in reversed(live):
        if _route_signature(r) != target_sig:
            break
        same_route.append(r)
    same_route.reverse()
    entries = []
    for r in same_route:
        entry = dict(run_id=r["exp_id"], verdict=r["verdict"],
                     outcome=r.get("outcome") or r.get("run_status"),
                     hypothesis=r.get("mutation_summary"),
                     hypothesis_verified=r["verdict"] in KEPT)
        for key in ("new_observation", "divergent_layer_identified"):
            if key in r:
                entry[key] = r[key]
        entries.append(entry)
    return assess_stagnation(entries, consecutive_limit=STALL_WINDOW)


def _append_or_recover_learn(state: dict, record: dict) -> dict:
    return ledger.append(state["ledger"], record,
                         progress=state["progress"], goal=state["goal"],
                         allow_idempotent_retry=True)


def _learn(state, body):
    current = state["current"]
    candidate, entry = current["candidate"], current["entry"]
    base_override = current.pop("baseline_override", None)
    base_info = base_override or state["baseline"]
    distill = body.get("distill")
    if distill is not None and (not isinstance(distill, dict) or not distill):
        raise ValueError("distill must be the nonempty knowledge_distill receipt when given")
    note = body.get("note")
    if note is not None and (not isinstance(note, str) or not note.strip()):
        raise ValueError("note must be nonempty text when given")
    note = note and note.strip()
    record = dict(entry, task_id=state["task_id"], exp_id=candidate["exp_id"],
                  candidate_repo=candidate["repo"],
                  run_ids=current["run_ids"], source_digest=candidate["source_digest"],
                  baseline_source_digest=base_info["source_digest"],
                  run_status=current["run_status"], card_seconds=current.get("card_seconds"),
                  unit_refs=current.get("unit_refs") or [],
                  receipt_refs=dict(current.get("receipt_refs") or {},
                                    profile=current.get("profile_ref")),
                  factors=candidate.get("factors"), trials=current["trials"],
                  knowledge_skipped=current.get("knowledge_skipped"),
                  lesson=dict(distill=distill, note=note))
    if base_info.get("exp_id"):
        record["baseline_exp_id"] = base_info["exp_id"]
    for key in ("new_observation", "divergent_layer_identified"):
        if type(body.get(key)) is bool:
            record[key] = body[key]
    appended = _append_or_recover_learn(state, record)
    verdict = record["verdict"]
    committed_lesson = record.get("lesson") if isinstance(record.get("lesson"), dict) else {}
    effective_distill = committed_lesson.get("distill")
    effective_note = committed_lesson.get("note")
    stagnation = _stagnation(state)
    current["stagnation"] = dict(status=stagnation.get("status"), reason=stagnation.get("reason"))
    extra = dict(progress=state["progress"], stagnation=current["stagnation"], ledger_committed=True)
    if appended.get("recovered_append"):
        extra["recovered_append"] = True
    if appended.get("revised_append"):
        extra["revised_append"] = True
    if appended.get("progress_unconfirmed"):
        state["progress_unconfirmed"] = True
        state["progress_warning"] = appended["progress_warning"]
        extra.update(progress_unconfirmed=True, progress_warning=appended["progress_warning"],
                     receipt_unconfirmed=True, receipt_warning=appended["progress_warning"])
    else:
        state.pop("progress_unconfirmed", None)
        state.pop("progress_warning", None)
    if base_override is not None:
        prof_ref = current.get("profile_ref")
        summary = current.get("bottleneck_summary")
        reused = current.get("profile_reused_from")
        _sync_from_ledger(state)
        if state["status"] != "running":
            return None, extra
        if (verdict not in KEPT and prof_ref and summary
                and state["rounds"] and state["rounds"][-1].get("exp_id") == candidate["exp_id"]):
            state["current"] = dict(skipped=[], bottleneck_summary=summary,
                                    profile_ref=prof_ref,
                                    profile_reused_from=reused or len(state["rounds"]))
            upcoming = "consult"
        else:
            state["current"] = dict(skipped=[])
            upcoming = "profile"
        extra["next_mode"] = state["mode"]
        return upcoming, extra
    previous = dict(state["baseline"])
    if verdict in KEPT and effective_distill is None:
        state.setdefault("pending_distill", []).append(
            dict(round=state["round"], exp_id=candidate["exp_id"],
                 reason=effective_note or "not distilled yet"))
    if verdict in KEPT:
        state["baseline"] = dict(repo=candidate["repo"], source_digest=candidate["source_digest"],
                                 exp_id=candidate["exp_id"])
        state.pop("restore_hint", None)
    elif candidate["repo"] == previous["repo"]:
        state["restore_hint"] = (f"the candidate shared the baseline checkout {previous['repo']}; "
                                 f"restore it to baseline source {previous['source_digest']} "
                                 "before the next change")
    state["rounds"].append(dict(round=state["round"], exp_id=candidate["exp_id"],
                                verdict=verdict, outcome=record.get("outcome"),
                                speedup_ratio=record.get("speedup_ratio"),
                                stagnation=stagnation.get("status")))
    explore_disabled = os.environ.get("MINDIE_DISABLE_RSI_EXPLORE") == "1"
    state["mode"] = "explore" if (stagnation.get("stalled") and not explore_disabled) else "reuse"
    if state["max_rounds"] is not None and state["round"] >= state["max_rounds"]:
        _finish(state, f"max_rounds {state['max_rounds']} reached")
        return None, extra
    state["round"] += 1
    if verdict not in KEPT and current.get("profile_ref") and current.get("bottleneck_summary"):
        state["current"] = dict(skipped=[], bottleneck_summary=current["bottleneck_summary"],
                                profile_ref=current["profile_ref"],
                                profile_reused_from=current.get("profile_reused_from")
                                or state["round"] - 1)
        upcoming = "consult"
    else:
        state["current"] = dict(skipped=[])
        upcoming = "profile"
    extra["next_mode"] = state["mode"]
    return upcoming, extra


def _record(state, body):
    """Composable one-shot operation: propose + measure + learn without multi-step ceremony."""
    if "bottleneck_summary" in body and "profile_ref" in body:
        _profile(state, body)
    if "unit_refs" in body:
        _consult(state, dict(mode=body.get("mode", state["mode"]),
                             unit_refs=body["unit_refs"],
                             skipped_reason=body.get("skipped_reason")))
    exp_id = _text(body, "exp_id")
    live, spent = ledger.fold(r for r in ledger.iter_ledger(state["ledger"], task_id=state["task_id"])
                              if r.get("task_id") == state["task_id"] and r.get("exp_id"))
    existing = next((r for r in reversed(live) if r.get("exp_id") == exp_id), None)
    if existing is not None:
        hypothesis = _text(body, "hypothesis")
        scope, factors = body.get("scope_conditions"), body.get("factors")
        repo = str(Path(_text(body, "candidate_repo")).resolve())
        digest = ledger.fingerprint(repo, exclude_paths=_state_paths(state))["source_digest"]
        state["current"]["baseline_override"] = dict(
            repo=state["baseline"]["repo"],
            source_digest=existing.get("baseline_source_digest") or state["baseline"]["source_digest"],
            exp_id=existing.get("baseline_exp_id"),
        )
        if "profile_ref" not in body and isinstance(existing.get("receipt_refs"), dict):
            state["current"].setdefault("profile_ref", existing["receipt_refs"].get("profile"))
        if "unit_refs" not in body:
            state["current"].setdefault("unit_refs", existing.get("unit_refs") or [])
            state["current"].setdefault("knowledge_skipped", existing.get("knowledge_skipped"))
        state["current"]["candidate"] = dict(
            exp_id=exp_id, hypothesis=hypothesis, repo=repo,
            source_digest=digest, scope_conditions=scope, factors=factors, spent=spent)
        prop_extra = dict(gate=dict(decision="submit", recovered=True, spent=spent))
    else:
        upcoming, prop_extra = _propose(state, body)
        if prop_extra.get("blocked") or upcoming is None:
            return upcoming, prop_extra
    upcoming, meas_extra = _measure(state, body)
    if meas_extra.get("error"):
        return upcoming, meas_extra
    upcoming, learn_extra = _learn(state, body)
    return upcoming, dict(**prop_extra, **meas_extra, **learn_extra)


HANDLERS = dict(profile=_profile, consult=_consult, propose=_propose,
                measure=_measure, learn=_learn, record=_record)

ALLOWED_STEPS_FROM = {
    "profile": frozenset({"profile", "consult", "propose", "record"}),
    "consult": frozenset({"profile", "consult", "propose", "record"}),
    "propose": frozenset({"profile", "consult", "propose", "record"}),
    "measure": frozenset({"propose", "measure", "record"}),
    "learn": frozenset({"learn"}),
}


def _finish(state, reason):
    state.update(status="finished", finish_reason=reason, step=None)


def complete(path, step, body) -> tuple[dict, int]:
    path = Path(path).resolve()
    if not isinstance(body, dict):
        raise ValueError("step evidence must be a JSON object")
    with ledger.exclusive_lock(path):
        state = _load(path)
        if state["status"] != "running":
            raise ValueError(f"loop is {state['status']}: {state['finish_reason']}")
        live, _ = ledger.fold(r for r in ledger.iter_ledger(state["ledger"], task_id=state["task_id"])
                              if r.get("task_id") == state["task_id"] and r.get("exp_id"))
        if _ledger_out_of_sync(state, live) and not (
            (step == "learn" and state["step"] == "learn")
            or (step == "record" and any(r.get("exp_id") == body.get("exp_id") for r in live))
        ):
            _sync_from_ledger(state, preserve_step=state["step"] in ("consult", "propose", "measure"))
            if state["status"] != "running":
                _save(path, state)
                return dict(_render_step(state), completed=step), 0
            if state["step"] == "learn":
                state["step"] = "profile"
        allowed = ALLOWED_STEPS_FROM.get(state["step"], frozenset({state["step"]}))
        if step not in allowed:
            raise ValueError(f"current step is {state['step']!r} (round {state['round']}); "
                             f"{step!r} is out of order. Run next to resume.")
        state["_state_path"] = str(path)
        try:
            upcoming, extra = HANDLERS[step](state, body)
        finally:
            state.pop("_state_path", None)
        if upcoming is not None:
            state["step"] = upcoming
        ledger_committed = bool(extra.pop("ledger_committed", False))
        save_warning = None
        try:
            _save(path, state)
        except TimeoutError as exc:
            if not ledger_committed:
                raise
            save_warning = f"TimeoutError: {exc}"
        except OSError as exc:
            if not ledger_committed:
                raise
            save_warning = f"{type(exc).__name__}: {exc}"
        if save_warning is not None:
            extra.update(receipt_unconfirmed=True, receipt_warning=save_warning,
                         state_unconfirmed=True, state_warning=save_warning)
        rendered = _render_step(state)
    code = BLOCKED if extra.get("blocked") else 1 if extra.get("error") else 0
    return dict(rendered, completed=step, **extra), code


def finish(path, reason) -> dict:
    path = Path(path).resolve()
    with ledger.exclusive_lock(path):
        state = _load(path)
        if state["status"] == "running":
            _finish(state, reason)
            _save(path, state)
    return next_step(path)


# --------------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="rsi_loop.py")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("init")
    start.add_argument("--state", required=True)
    start.add_argument("--task-id", required=True)
    start.add_argument("--ledger", required=True)
    start.add_argument("--baseline-repo", required=True)
    start.add_argument("--direction", required=True, choices=("maximize", "minimize"))
    start.add_argument("--repetitions", required=True, type=int)
    start.add_argument("--target-speedup", type=float, default=ledger.DEFAULT_TARGET)
    start.add_argument("--max-rounds", type=int)
    start.add_argument("--max-runs", type=int)
    start.add_argument("--max-card-seconds", type=float)
    start.add_argument("--progress")
    start.add_argument("--goal")
    commands.add_parser("next").add_argument("--state", required=True)
    done = commands.add_parser("complete")
    done.add_argument("step", choices=STEPS)
    done.add_argument("--state", required=True)
    stop = commands.add_parser("finish")
    stop.add_argument("--state", required=True)
    stop.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    code = 0
    try:
        if args.command == "init":
            result = init(args.state, task_id=args.task_id, ledger_path=args.ledger,
                          baseline_repo=args.baseline_repo, direction=args.direction,
                          repetitions=args.repetitions, target_speedup=args.target_speedup,
                          max_rounds=args.max_rounds, max_runs=args.max_runs,
                          max_card_seconds=args.max_card_seconds, progress=args.progress,
                          goal=args.goal)
        elif args.command == "next":
            result = next_step(args.state)
        elif args.command == "complete":
            result, code = complete(args.state, args.step, ledger._stdin_json())
        else:
            result = finish(args.state, args.reason)
    except (ValueError, OSError, TimeoutError, subprocess.TimeoutExpired,
            json.JSONDecodeError) as exc:
        result, code = dict(status="error", error=f"{type(exc).__name__}: {exc}"), 1
    sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
