"""[Role] Code-enforced RSI experiment ledger: fingerprint, judge, dedup and budget gate.

Shared by every host plugin through ``python -I -m mindie_knowledge.graded.experiment``
(`fingerprint`, `judge`, `append`, `gate`); exit 0 ok, 1 error, 3 blocked gate.

[Invariants]
- A judgment is `inconclusive` whenever evidence is incomplete (missing or failed
  trials, compile/infra failure, unverified correctness, missing or unchanged
  source fingerprint); incomplete evidence outranks a failed gate, which outranks
  a pass. Complete evidence is judged by `explore.evaluate_exploration_candidate`
  in this package; this module does not reimplement it.
- `append` rejects records that contradict their own run status (an infra or
  compile failure recorded as `refuted`) or lack a source fingerprint.
- `gate` blocks a submission whose source fingerprint and scope (missing scope
  equals `{}`) already have a live supported/refuted record, or whose task
  exceeded the run or card-second budget. Superseded revisions are folded first
  and distinct runs are counted once. `inconclusive` never blocks a retry.

[Timeouts & Locks]
- `fingerprint` bounds each `git` call and the total, streams the tracked diff
  through a temporary file and identifies untracked files above 512 MiB by size.
- Appends hold an exclusive sidecar `<ledger>.lock` (`StartLock`, bounded
  `LOCK_WAIT_SECONDS = 5.0`); contention past the wait is a `TimeoutError`.

[Side-Effect Policy]
- Only `append` writes, and only one LF-terminated JSON line to the named ledger
  plus its lock file. No network, model or device call.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager

from ..loop.locks import StartInProgress, StartLock
from .explore import evaluate_exploration_candidate

SCHEMA = "mindie-experiment-record/1"
JUDGMENTS = frozenset({"supported", "refuted", "inconclusive"})
# Execution outcomes describe missing evidence, never a refuted hypothesis
# (host Skill references: compile errors, SSH drops, invalid windows).
INCONCLUSIVE_STATUSES = frozenset({
    "compile_failed", "infra_failed", "measurement_invalid", "timeout",
    "interrupted", "unknown",
})
SETTLED = frozenset({"supported", "refuted"})
# source: per-call git bound used by InferRSI src/inferrsi/infra/targets/git.py
GIT_TIMEOUT = 15.0
# source: four git calls of a submodule-free fingerprint, each within GIT_TIMEOUT;
# submodule probes share this total instead of adding 15 s each
FINGERPRINT_TIMEOUT = 4 * GIT_TIMEOUT
# source: git core.bigFileThreshold default (512 MiB); larger untracked files
# (profiling dumps, wheels) are identified by size instead of being hashed
LARGE_FILE_BYTES = 512 * 1024 * 1024
# source: explore_support.legacy_evaluate noise_margin default
DEFAULT_NOISE_MARGIN = 0.01
# source: stdin bound of the codex adapter knowledge_cli.py
ARGUMENT_LIMIT = 1024 * 1024
# source: explore.evaluate_exploration_candidate default target_speedup
DEFAULT_TARGET = 1.05
# source: exit code for a blocked gate decision; 1 stays reserved for errors
BLOCKED = 3
# source: consent_store.LOCK_WAIT_SECONDS and AGENTS.md bounded StartLock waits
LOCK_WAIT_SECONDS = 5.0
# Optional G1/G3 evidence accepted by `explore._gates`.
GATE_FIELDS = ("ablation_ok", "hygiene_ok", "workspace_delta_bytes", "guard_workloads",
               "hidden_input_summary", "reusable_scope")


# --------------------------------------------------------------------- fingerprint
def _git(repo: Path, *args: str, deadline=None, stdout=subprocess.PIPE) -> bytes:
    timeout = GIT_TIMEOUT
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise subprocess.TimeoutExpired(["git", *args], FINGERPRINT_TIMEOUT)
    result = subprocess.run(["git", "-C", str(repo), *args], stdout=stdout,
                            stderr=subprocess.PIPE, timeout=timeout, check=False)
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip()
                         or f"git {args[0]} failed")
    return result.stdout


def _stream_digest(handle) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    for block in iter(lambda: handle.read(1 << 20), b""):  # streamed (Principle 18)
        digest.update(block)
        size += len(block)
    return digest.hexdigest(), size


def _file_digest(path: Path) -> str:
    with path.open("rb") as handle:
        return _stream_digest(handle)[0]


def _diff_digest(repo: Path, deadline) -> tuple[str, bool]:
    """Hash `git diff --binary HEAD` through a temporary file, never holding it in memory."""
    with tempfile.TemporaryFile() as spool:
        _git(repo, "diff", "--binary", "HEAD", "--", ".", deadline=deadline, stdout=spool)
        spool.seek(0)
        digest, size = _stream_digest(spool)
    return digest, size > 0


def fingerprint(repo) -> dict:
    """Identify the exact source under test: HEAD, tracked diff, untracked files, submodules.

    Adapted from InferRSI `infra/targets/git.py:repository_identity` (Apache-2.0,
    see third_party/inferrsi).
    """
    deadline = time.monotonic() + FINGERPRINT_TIMEOUT
    repo = Path(repo).resolve()
    root = Path(os.fsdecode(_git(repo, "rev-parse", "--show-toplevel", deadline=deadline))
                .strip()).resolve()
    if root != repo:
        raise ValueError("repo must be the root of its Git checkout")
    head = _git(repo, "rev-parse", "HEAD", deadline=deadline).decode("ascii").strip()
    diff_sha256, diff_present = _diff_digest(repo, deadline)
    untracked, large = [], []
    for raw in _git(repo, "ls-files", "--others", "--exclude-standard", "-z",
                    deadline=deadline).split(b"\0"):
        if raw:
            name = os.fsdecode(raw)
            path = repo / name
            if not path.is_file():
                untracked.append([name, "non-file"])
            elif path.stat().st_size > LARGE_FILE_BYTES:
                untracked.append([name, f"size:{path.stat().st_size}"])
                large.append(name)
            else:
                untracked.append([name, _file_digest(path)])
            if time.monotonic() > deadline:
                raise subprocess.TimeoutExpired(["fingerprint", str(repo)], FINGERPRINT_TIMEOUT)
    submodules = []
    for raw in _git(repo, "submodule", "status", "--recursive", deadline=deadline).splitlines():
        line = raw.decode("utf-8", errors="replace").strip()
        if line:
            commit, _, rest = line.lstrip("+-U ").partition(" ")
            path = rest.split(" (", 1)[0]
            dirty = bool(_git(repo / path, "status", "--porcelain", deadline=deadline)) \
                if (repo / path).is_dir() else None
            submodules.append([path, commit, dirty])
    material = dict(head=head, diff_sha256=diff_sha256,
                    untracked=sorted(untracked), submodules=sorted(submodules))
    digest = hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()
    result = dict(source_digest="sha256:" + digest, head=head,
                  dirty=bool(diff_present or untracked or any(s[2] for s in submodules)),
                  untracked_files=len(untracked), submodules=len(submodules))
    if large:
        result["unhashed_large_files"] = sorted(large)
        result["note"] = ("untracked files above 512 MiB are identified by size only; move "
                          "profiling dumps out of the checkout or add them to .gitignore")
    return result


# ------------------------------------------------------------------------- judge
def _metric(trial, label, index, incomplete):
    if not isinstance(trial, dict):
        incomplete.append(f"{label} trial {index}: not an object")
        return None
    if trial.get("status") != "completed":
        incomplete.append(f"{label} trial {index}: status {trial.get('status')!r}")
        return None
    value = trial.get("metric")
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value <= 0:
        incomplete.append(f"{label} trial {index}: metric missing or not positive")
        return None
    return float(value)


def judge(evidence) -> dict:
    """Three-state judgment: inconclusive > refuted > supported, plus the three axes.

    The evidence-completeness order follows InferRSI `harness/evaluation.py:evaluate`
    (Apache-2.0, see third_party/inferrsi); complete evidence goes to `explore`'s
    gates. A valid gain below the target stays `supported` with `goal_attained=False`; a ratio
    inside `1 +/- noise_margin` is `inconclusive` (`speedup_within_noise`); only a
    correctness failure or a regression beyond the noise is `refuted`.
    """
    if not isinstance(evidence, dict):
        raise ValueError("evidence must be an object")
    incomplete, failed = [], []
    repetitions = evidence.get("repetitions")
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    direction = evidence.get("direction")
    if direction not in ("maximize", "minimize"):
        raise ValueError("direction must be maximize or minimize")
    target = evidence.get("target_speedup", DEFAULT_TARGET)
    if isinstance(target, bool) or not isinstance(target, (int, float)) \
            or not math.isfinite(target) or target <= 0:
        raise ValueError("target_speedup must be a positive finite number")
    noise = evidence.get("noise_margin", DEFAULT_NOISE_MARGIN)
    if isinstance(noise, bool) or not isinstance(noise, (int, float)) \
            or not math.isfinite(noise) or noise < 0:
        raise ValueError("noise_margin must be a nonnegative finite number")
    pairs = {}
    for label in ("baseline", "candidate"):
        trials = evidence.get(label)
        if not isinstance(trials, list):
            raise ValueError(f"{label} must be an array of trials")
        if len(trials) != repetitions:
            incomplete.append(f"{label}: expected {repetitions} trials, recorded {len(trials)}")
        pairs[label] = [_metric(t, label, i, incomplete) for i, t in enumerate(trials, 1)]
    if evidence.get("compile_ok") is False:
        incomplete.append("candidate did not build; nothing was measured")
    correctness = evidence.get("correctness")
    if correctness == "failed":
        failed.append("correctness check failed")
    elif correctness != "passed":
        incomplete.append(f"correctness was not verified ({correctness!r})")
    base_src, cand_src = evidence.get("baseline_source_digest"), evidence.get("source_digest")
    if not cand_src or not base_src:
        incomplete.append("source fingerprint missing; the measured code is not identified")
    elif cand_src == base_src:
        incomplete.append("candidate source equals baseline source; timing noise cannot "
                          "establish an improvement")
    speedup = None
    complete = [(b, c) for b, c in zip(pairs["baseline"], pairs["candidate"])
                if b is not None and c is not None]
    if complete:
        # Median of paired ratios, as MindIE Bench scores paired rounds.
        ratios = [c / b if direction == "maximize" else b / c for b, c in complete]
        speedup = statistics.median(ratios)
    engine, outcome, noisy = None, None, []
    if not incomplete and not failed:
        candidate = dict(compile_ok=True, correctness_ok=True, speedup=speedup,
                         conditions=dict(evidence.get("scope_conditions") or {}),
                         hypothesis=str(evidence.get("hypothesis") or ""))
        # G1 hidden retention and G3 ablation / workspace / guard evidence go to the
        # engine as given; an absent field is left absent rather than assumed passed.
        for key in GATE_FIELDS:
            if key in evidence:
                candidate[key] = evidence[key]
        engine = evaluate_exploration_candidate(candidate, target_speedup=float(target))
        verdict = engine["verdict"]
        if verdict == "reject_invalid_evidence":
            failed.append(f"{engine['reason']}: hidden-input or parity gate failed")
            outcome = engine["reason"]
        elif verdict in ("distill_candidate", "retain_verified_evidence"):
            outcome = "qualified_improvement" if verdict == "distill_candidate" \
                else engine["reason"]
        elif speedup > 1 + noise:
            outcome = "subtarget_valid"
        elif speedup >= 1 - noise:
            outcome = "speedup_within_noise"
            noisy.append(f"median paired speedup {speedup:.4g} is within the noise margin "
                         f"1 +/- {float(noise):g}; neither a gain nor a loss is shown")
        else:
            outcome = "regression"
            failed.append(f"median paired speedup {speedup:.4g} is a regression beyond the "
                          f"noise margin {float(noise):g}")
    elif failed:
        outcome = "correctness_failed"
    judgment = "inconclusive" if incomplete or noisy else "refuted" if failed else "supported"
    axes = dict(evidence_valid=not incomplete and not failed,
                goal_attained=bool(engine and engine["axes"]["goal_achieved"]
                                   and judgment == "supported"),
                reusable=bool(engine and engine["axes"]["reusable_encoding"]
                              and judgment == "supported"))
    reasons = incomplete + failed + noisy
    if outcome == "subtarget_valid":
        reasons.append(f"valid gain {speedup:.4g} is below target {float(target):.4g}; kept as "
                       "supported evidence with goal_attained=false")
    elif engine and outcome != "qualified_improvement" and judgment == "supported":
        reasons.append(f"{engine['reason']}: gain kept, not yet reusable as a mutator")
    return dict(judgment=judgment, outcome=outcome, axes=axes, reasons=reasons,
                speedup=speedup, target_speedup=float(target), noise_margin=float(noise),
                paired_rounds=len(complete), engine_verdict=engine and engine["verdict"],
                engine_gates=engine and {k: v["passed"] for k, v in engine["gates"].items()})


# ------------------------------------------------------------------------ ledger
def read_ledger(path) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        return []
    records = []
    with path.open("r", encoding="utf-8", newline="\n") as handle:
        for number, line in enumerate(handle, 1):
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{number} is not valid JSON: {exc.msg}") from exc
    return records


def validate(record) -> dict:
    if not isinstance(record, dict):
        raise ValueError("record must be an object")
    for key in ("task_id", "exp_id", "hypothesis", "source_digest"):
        if not isinstance(record.get(key), str) or not record[key].strip():
            raise ValueError(f"{key} must be nonempty text")
    run_ids = record.get("run_ids")
    if not isinstance(run_ids, list) or any(not isinstance(r, str) or not r for r in run_ids):
        raise ValueError("run_ids must be an array of run_id strings")
    judgment = record.get("judgment")
    if judgment not in JUDGMENTS:
        raise ValueError("judgment must be supported, refuted or inconclusive")
    status = record.get("run_status")
    if status in INCONCLUSIVE_STATUSES and judgment != "inconclusive":
        raise ValueError(f"run_status {status!r} is missing evidence; judgment must be "
                         "inconclusive, not " + judgment)
    seconds = record.get("card_seconds")
    if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                                or not math.isfinite(seconds) or seconds < 0):
        raise ValueError("card_seconds must be a nonnegative number")
    scope = record.get("scope_conditions")
    if scope is not None and not isinstance(scope, dict):
        raise ValueError("scope_conditions must be an object")
    return dict(record, schema=SCHEMA)


@contextmanager
def exclusive_lock(path):
    """Hold the sidecar `<path>.lock` exclusively, waiting at most LOCK_WAIT_SECONDS."""
    lock = StartLock(str(path) + ".lock", wait=LOCK_WAIT_SECONDS)
    try:
        lock.acquire(write_record=False)
    except StartInProgress:
        raise TimeoutError(f"{path} is locked by another writer for more than "
                           f"{LOCK_WAIT_SECONDS:g}s") from None
    try:
        yield
    finally:
        lock.release()


def append(path, record) -> dict:
    entry = validate(record)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(path):
        if entry.get("supersedes_exp_id") is None and any(
                r.get("task_id") == entry["task_id"] and r.get("exp_id") == entry["exp_id"]
                for r in read_ledger(path)):
            raise ValueError(f"exp_id {entry['exp_id']!r} already recorded; set "
                             "supersedes_exp_id to revise it")
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return dict(status="recorded", exp_id=entry["exp_id"], judgment=entry["judgment"])


def fold(records) -> tuple[list[dict], dict]:
    """Apply `supersedes_exp_id` revisions in ledger order.

    Returns the live records (latest revision per experiment, in order) and the
    spend: distinct `run_ids`, and card-seconds per revision chain. A revision that
    adds no new run restates the same device time, so its `card_seconds` (when
    given) replaces the chain's value instead of adding to it.
    """
    live, chain_seconds, seen = {}, {}, set()
    for record in records:
        runs = set(record.get("run_ids") or ())
        fresh = bool(runs - seen)
        seen |= runs
        own = record.get("card_seconds")
        prior = record.get("supersedes_exp_id")
        carried = None
        if prior is not None and prior in live:
            live.pop(prior)
            carried = chain_seconds.pop(prior)
        if carried is None:
            seconds = float(own or 0)
        elif fresh:
            seconds = carried + float(own or 0)
        else:
            seconds = carried if own is None else float(own)
        key = record.get("exp_id")
        live.pop(key, None)
        live[key], chain_seconds[key] = record, seconds
    return list(live.values()), dict(runs=len(seen), card_seconds=sum(chain_seconds.values()))


def _same_scope(recorded, scope) -> bool:
    return (recorded or {}) == (scope or {})


def gate(path, *, task_id, source_digest, scope=None, max_runs=None, max_card_seconds=None):
    """Decide before a candidate goes to the device: submit, duplicate or budget_exhausted.

    Mechanism follows vllm-evolve `bench/eval_pool.py` (HashCache + EvalBudget).
    """
    live, total = fold(r for r in read_ledger(path) if r.get("task_id") == task_id)
    runs, seconds = total["runs"], total["card_seconds"]
    spent = dict(runs=runs, card_seconds=seconds, max_runs=max_runs,
                 max_card_seconds=max_card_seconds)
    for record in reversed(live):
        if record.get("source_digest") == source_digest and record.get("judgment") in SETTLED \
                and _same_scope(record.get("scope_conditions"), scope):
            return dict(decision="duplicate", spent=spent,
                        prior=dict(exp_id=record.get("exp_id"), judgment=record["judgment"],
                                   run_ids=record.get("run_ids")),
                        reason="this exact source was already judged under the same scope")
    if max_runs is not None and runs >= max_runs:
        return dict(decision="budget_exhausted", spent=spent,
                    reason=f"{runs} device runs recorded; budget is {max_runs}")
    if max_card_seconds is not None and seconds >= max_card_seconds:
        return dict(decision="budget_exhausted", spent=spent,
                    reason=f"{seconds:g} card-seconds recorded; budget is {max_card_seconds:g}")
    return dict(decision="submit", spent=spent)


# --------------------------------------------------------------------------- CLI
def _stdin_json():
    raw = sys.stdin.buffer.read(ARGUMENT_LIMIT + 1)
    if len(raw) > ARGUMENT_LIMIT:
        raise ValueError("input exceeds 1 MiB")
    return json.loads(raw)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mindie_knowledge.graded.experiment")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("fingerprint").add_argument("repo")
    commands.add_parser("judge")
    commands.add_parser("append").add_argument("--ledger", required=True)
    check = commands.add_parser("gate")
    check.add_argument("--ledger", required=True)
    check.add_argument("--task-id", required=True)
    check.add_argument("--source-digest", required=True)
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
            result = append(args.ledger, _stdin_json())
        else:
            result = gate(args.ledger, task_id=args.task_id, source_digest=args.source_digest,
                          scope=args.scope, max_runs=args.max_runs,
                          max_card_seconds=args.max_card_seconds)
        code = BLOCKED if result.get("decision") in ("duplicate", "budget_exhausted") else 0
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        result, code = dict(status="error", error=f"{type(exc).__name__}: {exc}"), 1
    sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
