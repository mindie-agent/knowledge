"""[Role] Resumable RSI loop `profile -> consult -> propose -> measure -> learn`.

A persisted step machine the Agent advances with ``python -I -m
mindie_knowledge.graded.rsi_loop`` `next` / `complete`; one report per round. MCP
steps stay with the Agent; `propose`/`measure` use `experiment` (fingerprint,
gate, judge) and `learn` asks `explore.assess_stagnation` whether to explore.

[Invariants]
- `complete` accepts only the current step; an out-of-order step is rejected
  without touching state, so a compacted session resumes with `next`.
- A candidate reaches the device only after `gate` returns `submit`; a duplicate
  stays at `propose`, an exhausted budget finishes the loop.
- `measure` re-fingerprints the candidate and refuses evidence whose source
  changed after the gate. Execution failures are always `inconclusive`.
- `learn` requires a lesson matching the judgment (supported: distill receipt or
  `defer_distill`, kept in `pending_distill`; refuted: scoped `conditional_avoid`;
  inconclusive: `repair`). Supported (including sub-target) becomes the baseline;
  the profile is reused while the baseline is unchanged. Each round writes
  `round-<n>.md`. `consult` may skip the query with a reported `skipped_reason`.

[Timeouts & Locks]
- State changes hold `<state>.lock` via `experiment.exclusive_lock` (bounded
  5.0s) and replace the state file atomically; fingerprints use the bounded
  `git` calls of `experiment`. No device, network or model call is made here.

[Side-Effect Policy]
- Writes only the state file, its lock, the ledger (via `experiment.append`)
  and Markdown reports under the report directory.
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

from . import experiment as ledger
from .explore import assess_stagnation

SCHEMA = "mindie-rsi-loop/1"
STEPS = ("profile", "consult", "propose", "measure", "learn")
# source: exit code for a blocked gate decision in experiment.py
BLOCKED = ledger.BLOCKED
# source: explore.assess_stagnation default consecutive_limit (SKILL.md stagnation gate)
STALL_WINDOW = 3

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
                '{"exp_id", "hypothesis", "candidate_repo", "scope_conditions": {...}}; the loop '
                "fingerprints it and runs the ledger gate. Exit 3 means do not submit."),
    "measure": ("Submit baseline and candidate through coordinator/remote-dev on the same device, "
                "alternating AB/BA, and register every run_id in PROGRESS.md in_flight_runs. If "
                "baseline and candidate share one checkout, leave it at the candidate code before "
                "completing: the loop re-fingerprints it. Complete with "
                '{"run_ids", "baseline": [{"status", "metric"}], "candidate": [...], '
                '"correctness": "passed|failed", "compile_ok", "run_status", "card_seconds"} '
                "plus any of ablation_ok, workspace_delta_bytes, guard_workloads, "
                "hidden_input_summary, noise_margin."),
    "learn": ("Keep the lesson. supported: call knowledge_distill and pass its receipt as "
              '{"distill": {...}}, or defer it to a milestone with {"defer_distill": true, '
              '"reason": "..."}. refuted: pass {"conditional_avoid": "<scoped negative '
              'result>"} (distill optional). inconclusive: pass {"repair": "<missing evidence '
              'to fix>"}.'),
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


def _locked(path: Path):
    return ledger.exclusive_lock(path)


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


def init(path, *, task_id, ledger_path, baseline_repo, direction, repetitions,
         target_speedup=ledger.DEFAULT_TARGET, max_rounds=None, max_runs=None,
         max_card_seconds=None, report_dir=None) -> dict:
    path = Path(path).resolve()
    if direction not in ("maximize", "minimize"):
        raise ValueError("direction must be maximize or minimize")
    baseline = ledger.fingerprint(baseline_repo)
    state = dict(
        schema=SCHEMA, task_id=task_id, ledger=str(Path(ledger_path).resolve()),
        report_dir=str(Path(report_dir).resolve() if report_dir
                       else path.parent / "rsi-reports"),
        direction=direction, repetitions=_positive(repetitions, "repetitions", integer=True),
        target_speedup=_positive(target_speedup, "target_speedup"),
        max_rounds=_positive(max_rounds, "max_rounds", integer=True, optional=True),
        max_runs=_positive(max_runs, "max_runs", integer=True, optional=True),
        max_card_seconds=_positive(max_card_seconds, "max_card_seconds", optional=True),
        baseline=dict(repo=str(Path(baseline_repo).resolve()),
                      source_digest=baseline["source_digest"], exp_id=None),
        round=1, step="profile", mode="reuse", status="running", finish_reason=None,
        current=dict(skipped=[]), rounds=[], pending_distill=[])
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path):
        if path.exists():
            raise ValueError(f"{path} already exists; use next to resume it")
        _save(path, state)
    return next_step(path)


def next_step(path) -> dict:
    state = _load(Path(path).resolve())
    if state["status"] != "running":
        return dict(status=state["status"], finish_reason=state["finish_reason"],
                    rounds=len(state["rounds"]), report_dir=state["report_dir"])
    guide = GUIDE[state["step"]].replace("<mode>", repr(state["mode"]))
    result = dict(status="running", round=state["round"], step=state["step"],
                  mode=state["mode"], guide=guide,
                  baseline_source_digest=state["baseline"]["source_digest"])
    if state["step"] == "learn":
        result["judgment"] = state["current"]["judgment"]["judgment"]
    if state["current"].get("skipped"):
        result["skipped_this_round"] = state["current"]["skipped"]
    if state["current"].get("profile_reused_from") is not None:
        result["profile_reused_from_round"] = state["current"]["profile_reused_from"]
    if state.get("restore_hint"):
        result["restore_hint"] = state["restore_hint"]
    if state.get("pending_distill"):
        result["pending_distill"] = state["pending_distill"]
    return result


# ------------------------------------------------------------------------- steps
def _profile(state, body):
    if not isinstance(body.get("bottleneck_summary"), dict) or not body["bottleneck_summary"]:
        raise ValueError("bottleneck_summary must be a nonempty object from the profiler")
    state["current"].update(bottleneck_summary=body["bottleneck_summary"],
                            profile_ref=_text(body, "profile_ref"))
    return "consult", {}


def _consult(state, body):
    if body.get("mode") != state["mode"]:
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
    hypothesis, exp_id = _text(body, "hypothesis"), _text(body, "exp_id")
    scope = body.get("scope_conditions")
    if scope is not None and not isinstance(scope, dict):
        raise ValueError("scope_conditions must be an object")
    repo = str(Path(_text(body, "candidate_repo")).resolve())
    digest = ledger.fingerprint(repo)["source_digest"]
    if digest == state["baseline"]["source_digest"]:
        raise ValueError("candidate source equals the baseline; change the code before proposing")
    state.pop("restore_hint", None)
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
                                factors=body.get("factors"), spent=decision["spent"])
    return "measure", dict(gate=decision)


def _measure(state, body):
    candidate = state["current"]["candidate"]
    now = ledger.fingerprint(candidate["repo"])["source_digest"]
    if now != candidate["source_digest"]:
        state["current"].pop("candidate")
        return "propose", dict(error="candidate source changed after the gate; the measured "
                                     "code is not the gated code. Propose it again.")
    run_ids = body.get("run_ids")
    if not isinstance(run_ids, list) or not run_ids \
            or any(not isinstance(r, str) or not r for r in run_ids):
        raise ValueError("run_ids must be a nonempty array of run_id strings")
    evidence = dict(repetitions=state["repetitions"], direction=state["direction"],
                    target_speedup=state["target_speedup"],
                    baseline=body.get("baseline"), candidate=body.get("candidate"),
                    correctness=body.get("correctness"), compile_ok=body.get("compile_ok"),
                    baseline_source_digest=state["baseline"]["source_digest"],
                    source_digest=candidate["source_digest"],
                    scope_conditions=candidate["scope_conditions"],
                    hypothesis=candidate["hypothesis"])
    for key in ledger.GATE_FIELDS + ("noise_margin",):
        if key in body:
            evidence[key] = body[key]
    verdict = ledger.judge(evidence)
    status = body.get("run_status") or "completed"
    if status in ledger.INCONCLUSIVE_STATUSES and verdict["judgment"] != "inconclusive":
        verdict = dict(verdict, judgment="inconclusive", outcome=status,
                       axes=dict(evidence_valid=False, goal_attained=False, reusable=False),
                       reasons=[f"run_status {status!r} means evidence is missing"]
                       + verdict["reasons"])
    seconds = body.get("card_seconds")
    if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                                or not math.isfinite(seconds) or seconds < 0):
        raise ValueError("card_seconds must be a nonnegative number")
    state["current"].update(
        run_ids=run_ids, run_status=status, card_seconds=seconds,
        receipt_refs=body.get("receipt_refs"),
        trials=dict(baseline=evidence["baseline"], candidate=evidence["candidate"],
                    correctness=evidence["correctness"], compile_ok=evidence["compile_ok"]),
        judgment=verdict)
    return "learn", dict(verdict=verdict)


def _stagnation(state) -> dict:
    live, _ = ledger.fold(r for r in ledger.read_ledger(state["ledger"])
                          if r.get("task_id") == state["task_id"])
    entries = []
    for r in live:
        # One entry per experiment: the engine dedups by run_id, and a reused
        # baseline run in run_ids must not fold several rounds into one.
        entry = dict(run_id=r["exp_id"], verdict=r["judgment"],
                     outcome=r.get("outcome") or r.get("run_status"),
                     hypothesis=r.get("hypothesis"),
                     hypothesis_verified=r["judgment"] == "supported")
        for key in ("new_observation", "divergent_layer_identified"):
            if key in r:
                entry[key] = r[key]
        entries.append(entry)
    return assess_stagnation(entries, consecutive_limit=STALL_WINDOW)


def _learn(state, body):
    current = state["current"]
    candidate, verdict = current["candidate"], current["judgment"]
    judgment = verdict["judgment"]
    lesson = {}
    if judgment == "supported":
        if isinstance(body.get("distill"), dict) and body["distill"]:
            lesson["distill"] = body["distill"]
        elif body.get("defer_distill") is True:
            lesson["defer_distill"] = _text(body, "reason")
        else:
            raise ValueError("a supported result needs the knowledge_distill receipt as distill, "
                             'or {"defer_distill": true, "reason": "..."}')
    elif judgment == "refuted":
        lesson["conditional_avoid"] = _text(body, "conditional_avoid")
        if isinstance(body.get("distill"), dict):
            lesson["distill"] = body["distill"]
    else:
        lesson["repair"] = _text(body, "repair")
    record = dict(task_id=state["task_id"], exp_id=candidate["exp_id"],
                  hypothesis=candidate["hypothesis"], run_ids=current["run_ids"],
                  source_digest=candidate["source_digest"],
                  baseline_source_digest=state["baseline"]["source_digest"],
                  judgment=judgment, run_status=current["run_status"],
                  unit_refs=current.get("unit_refs") or [],
                  receipt_refs=dict(current.get("receipt_refs") or {},
                                    profile=current.get("profile_ref")),
                  speedup=verdict.get("speedup"), factors=candidate.get("factors"),
                  outcome=verdict.get("outcome"), axes=verdict.get("axes"),
                  conditional_avoid=lesson.get("conditional_avoid"))
    for key in ("new_observation", "divergent_layer_identified"):
        if type(body.get(key)) is bool:
            record[key] = body[key]
    if candidate.get("scope_conditions") is not None:
        record["scope_conditions"] = candidate["scope_conditions"]
    if current.get("card_seconds") is not None:
        record["card_seconds"] = current["card_seconds"]
    ledger.append(state["ledger"], record)
    stagnation = _stagnation(state)
    current.update(lesson=lesson, stagnation=dict(status=stagnation.get("status"),
                                                  reason=stagnation.get("reason")))
    previous = dict(state["baseline"])
    pending = state.setdefault("pending_distill", [])
    if "defer_distill" in lesson:
        pending.append(dict(round=state["round"], exp_id=candidate["exp_id"],
                            reason=lesson["defer_distill"]))
    if judgment == "supported":
        state["baseline"] = dict(repo=candidate["repo"], source_digest=candidate["source_digest"],
                                 exp_id=candidate["exp_id"])
        state.pop("restore_hint", None)
    elif candidate["repo"] == previous["repo"]:
        state["restore_hint"] = (f"the candidate shared the baseline checkout {previous['repo']}; "
                                 f"restore it to baseline source {previous['source_digest']} "
                                 "before the next change")
    report = _write_report(state, dict(current, round=state["round"], mode=state["mode"],
                                       baseline=previous))
    state["rounds"].append(dict(round=state["round"], exp_id=candidate["exp_id"],
                                judgment=judgment, speedup=verdict.get("speedup"),
                                report=report, stagnation=stagnation.get("status")))
    _write_index(state)
    state["mode"] = "explore" if stagnation.get("stalled") else "reuse"
    if state["max_rounds"] is not None and state["round"] >= state["max_rounds"]:
        _finish(state, f"max_rounds {state['max_rounds']} reached")
        return None, dict(report=report)
    state["round"] += 1
    if judgment != "supported" and current.get("profile_ref"):
        # The baseline did not change, so its profile still holds (GEAK reprofile rule).
        state["current"] = dict(skipped=[], bottleneck_summary=current["bottleneck_summary"],
                                profile_ref=current["profile_ref"],
                                profile_reused_from=current.get("profile_reused_from")
                                or state["round"] - 1)
        upcoming = "consult"
    else:
        state["current"] = dict(skipped=[])
        upcoming = "profile"
    extra = dict(report=report, next_mode=state["mode"])
    if state.get("restore_hint"):
        extra["restore_hint"] = state["restore_hint"]
    return upcoming, extra


HANDLERS = dict(profile=_profile, consult=_consult, propose=_propose,
                measure=_measure, learn=_learn)


def _finish(state, reason):
    state.update(status="finished", finish_reason=reason, step=None)
    _write_index(state)


def complete(path, step, body) -> tuple[dict, int]:
    path = Path(path).resolve()
    if not isinstance(body, dict):
        raise ValueError("step evidence must be a JSON object")
    with _locked(path):
        state = _load(path)
        if state["status"] != "running":
            raise ValueError(f"loop is {state['status']}: {state['finish_reason']}")
        if step != state["step"]:
            raise ValueError(f"current step is {state['step']!r} (round {state['round']}); "
                             f"{step!r} is out of order. Run next to resume.")
        upcoming, extra = HANDLERS[step](state, body)
        if upcoming is not None:
            state["step"] = upcoming
        _save(path, state)
    code = BLOCKED if extra.get("blocked") else 1 if extra.get("error") else 0
    return dict(next_step(path), completed=step, **extra), code


# ------------------------------------------------------------------------ report
def _cell(value) -> str:
    return "—" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def _metric(trial):
    return trial.get("metric") if isinstance(trial, dict) and trial.get("status") == "completed" \
        else None


def render_round(state, data) -> str:
    verdict, candidate = data["judgment"], data["candidate"]
    lines = [f"# RSI round {data['round']} — {state['task_id']}", "",
             f"**Judgment: {verdict['judgment']}**"
             + (f" (median paired speedup {verdict['speedup']:.4g}, target "
                f"{verdict['target_speedup']:.4g})" if verdict.get("speedup") else "")
             + (f"; outcome `{verdict['outcome']}`" if verdict.get("outcome") else ""), "",
             "## Where the time went", "",
             f"- Profile: `{_cell(data.get('profile_ref'))}`"
             + (f" (reused from round {data['profile_reused_from']}; baseline unchanged)"
                if data.get("profile_reused_from") else ""),
             f"- Bottleneck: `{json.dumps(data.get('bottleneck_summary'), ensure_ascii=False)}`",
             f"- Knowledge mode: {data['mode']}; cards used: "
             + (", ".join(f"`{r['unit_id']}@{r['revision']}`" for r in data.get("unit_refs") or [])
                or "none")
             + (f" (query skipped: {_cell(data['knowledge_skipped'])})"
                if data.get("knowledge_skipped") else ""), "",
             "## What changed", "",
             f"- Experiment `{candidate['exp_id']}`: {candidate['hypothesis']}",
             f"- Baseline source `{data['baseline']['source_digest']}`",
             f"- Candidate source `{candidate['source_digest']}`",
             f"- Scope: `{json.dumps(candidate.get('scope_conditions'), ensure_ascii=False)}`"]
    for skipped in data.get("skipped") or ():
        lines.append(f"- Not submitted `{skipped['exp_id']}` ({skipped['decision']}): "
                     f"{_cell(skipped.get('reason'))}")
    trials = data["trials"]
    lines += ["", "## Measurements", "",
              f"Runs: {', '.join(f'`{r}`' for r in data['run_ids'])}; run status "
              f"`{data['run_status']}`; correctness `{_cell(trials['correctness'])}`; "
              f"card-seconds {_cell(data.get('card_seconds'))}", "",
              "| Pair | Baseline | Candidate | Ratio |", "| --- | --- | --- | --- |"]
    baseline = trials["baseline"] if isinstance(trials["baseline"], list) else []
    measured = trials["candidate"] if isinstance(trials["candidate"], list) else []
    for index in range(max(len(baseline), len(measured))):
        base = _metric(baseline[index]) if index < len(baseline) else None
        cand = _metric(measured[index]) if index < len(measured) else None
        ratio = None
        if base and cand:
            ratio = f"{(cand / base if state['direction'] == 'maximize' else base / cand):.4g}"
        lines.append(f"| {index + 1} | {_cell(base)} | {_cell(cand)} | {_cell(ratio)} |")
    lines += ["", "## Why this judgment", ""]
    lines += [f"- {reason}" for reason in verdict["reasons"]] or \
        ["- Evidence complete; the knowledge engine gate accepted the gain."]
    lesson = data.get("lesson") or {}
    lines += ["", "## Lesson kept", ""]
    if "distill" in lesson:
        lines.append(f"- knowledge_distill: `{json.dumps(lesson['distill'], ensure_ascii=False)}`")
    if "defer_distill" in lesson:
        lines.append(f"- knowledge_distill deferred: {lesson['defer_distill']}")
    if "conditional_avoid" in lesson:
        lines.append(f"- Avoid under this scope: {lesson['conditional_avoid']}")
    if "repair" in lesson:
        lines.append(f"- Repair before retrying: {lesson['repair']}")
    stagnation = data.get("stagnation") or {}
    lines += ["", "## Next", "",
              f"- Stagnation check: {_cell(stagnation.get('status'))} — "
              f"{_cell(stagnation.get('reason'))}"]
    return "\n".join(lines) + "\n"


def _write_report(state, data) -> str:
    directory = Path(state["report_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"round-{data['round']}.md"
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(render_round(state, data), encoding="utf-8", newline="\n")
    os.replace(temporary, target)
    return str(target)


def _write_index(state) -> str:
    directory = Path(state["report_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    lines = [f"# RSI loop — {state['task_id']}", "",
             f"Status: {state['status']} ({_cell(state['finish_reason'])}); "
             f"final baseline `{state['baseline']['source_digest']}` "
             f"(from `{_cell(state['baseline']['exp_id'])}`)", "",
             "| Round | Experiment | Judgment | Speedup | Report |", "| --- | --- | --- | --- | --- |"]
    for item in state["rounds"]:
        speed = f"{item['speedup']:.4g}" if item.get("speedup") else None
        lines.append(f"| {item['round']} | `{item['exp_id']}` | {item['judgment']} | "
                     f"{_cell(speed)} | [round-{item['round']}.md](round-{item['round']}.md) |")
    if state.get("pending_distill"):
        lines += ["", "## Deferred knowledge_distill", ""]
        lines += [f"- Round {item['round']} `{item['exp_id']}`: {item['reason']}"
                  for item in state["pending_distill"]]
    target = directory / "index.md"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return str(target)


def finish(path, reason) -> dict:
    path = Path(path).resolve()
    with _locked(path):
        state = _load(path)
        if state["status"] == "running":
            _finish(state, reason)
            _save(path, state)
    return dict(next_step(path), index=str(Path(state["report_dir"]) / "index.md"))


# --------------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mindie_knowledge.graded.rsi_loop")
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
    start.add_argument("--report-dir")
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
                          max_card_seconds=args.max_card_seconds, report_dir=args.report_dir)
        elif args.command == "next":
            result = next_step(args.state)
        elif args.command == "complete":
            result, code = complete(args.state, args.step, ledger._stdin_json())
        else:
            result = finish(args.state, args.reason)
    except (ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        result, code = dict(status="error", error=f"{type(exc).__name__}: {exc}"), 1
    sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
