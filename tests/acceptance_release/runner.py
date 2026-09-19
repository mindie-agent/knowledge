"""Independent acceptance runner. Does not start Codex, Grok, Kimi, or any model.

Plan, start, complete and valid are separate phases. Foreground and background
tokens/time are separate. Auto-retry is forbidden. The required model string is
``gpt-5.6-luna`` with reasoning ``max``; complete() checks the executor record
rather than trusting the plan text.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cases import (
    CASES,
    DEFAULT_MAX_CALLS,
    DEFAULT_WALL_SECONDS,
    REQUIRED_MODEL,
    REQUIRED_REASONING,
    case_by_id,
)
from .evidence import audit, load_artifacts
from .metrics import summarize_run

SCHEMA = "mindie-acceptance-run/1"
RECORD_SCHEMA = "mindie-acceptance-record/1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def run_dir(root: Path) -> Path:
    return Path(root).expanduser().resolve()


def run_record_path(root: Path) -> Path:
    return run_dir(root) / "run.json"


def case_dir(root: Path, ident: str) -> Path:
    return run_dir(root) / "cases" / ident


def load_run(root: Path) -> dict[str, Any]:
    path = run_record_path(root)
    record = _read_json(path)
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        raise ValueError("acceptance run is missing or incompatible")
    if record.get("required_model") != REQUIRED_MODEL or record.get("required_reasoning") != REQUIRED_REASONING:
        raise ValueError("run does not pin gpt-5.6-luna / max")
    return record


def save_run(root: Path, record: dict[str, Any]) -> None:
    _write_json(run_record_path(root), record)


def plan(root: Path, *, wall_seconds: int = DEFAULT_WALL_SECONDS, max_calls: int = DEFAULT_MAX_CALLS) -> dict[str, Any]:
    root = run_dir(root)
    root.mkdir(parents=True, exist_ok=True)
    if wall_seconds < 1 or max_calls < 1:
        raise ValueError("limits must be positive")
    record = {
        "schema": SCHEMA,
        "created_at": utc_now(),
        "phase": "planned",
        "required_model": REQUIRED_MODEL,
        "required_reasoning": REQUIRED_REASONING,
        "auto_retry": False,
        "max_retries": 0,
        "limits": {"wall_seconds": wall_seconds, "max_model_calls": max_calls},
        "note": (
            "This runner does not invoke a model. Codex must execute requires_model "
            "cases with gpt-5.6-luna and reasoning max, then pass the record to complete."
        ),
        "cases": [
            {
                "id": item["id"],
                "family": item["family"],
                "requires_model": item["requires_model"],
                "phase": "planned",
                "windows": item["windows"],
                "npu": item["npu"],
            }
            for item in CASES
        ],
    }
    save_run(root, record)
    for item in CASES:
        _write_json(
            case_dir(root, item["id"]) / "plan.json",
            {
                "schema": SCHEMA,
                "case": item,
                "required_model": REQUIRED_MODEL,
                "required_reasoning": REQUIRED_REASONING,
                "auto_retry": False,
                "limits": record["limits"],
                "phase": "planned",
                "at": utc_now(),
            },
        )
    return status(root)


def start(root: Path, ident: str) -> dict[str, Any]:
    record = load_run(root)
    case = case_by_id(ident)
    launch = {
        "schema": SCHEMA,
        "phase": "started",
        "case": ident,
        "at": utc_now(),
        "required_model": REQUIRED_MODEL,
        "required_reasoning": REQUIRED_REASONING,
        "auto_retry": False,
        "max_retries": 0,
        "limits": record["limits"],
        "requires_model": case["requires_model"],
        "invoked_model": False,
        "instruction": (
            "Executor must set model=gpt-5.6-luna and reasoning=max in the actual "
            "run record. Do not retry. Stop on timeout, discard, or no output."
        ),
    }
    _write_json(case_dir(root, ident) / "started.json", launch)
    _set_phase(record, ident, "started")
    save_run(root, record)
    return launch


def _set_phase(record: dict[str, Any], ident: str, phase: str) -> None:
    for item in record["cases"]:
        if item["id"] == ident:
            item["phase"] = phase
            return
    raise KeyError(ident)


def _usage_ok(blob: dict[str, Any]) -> bool:
    if not isinstance(blob, dict):
        return False
    for key in ("input_tokens", "output_tokens", "wall_seconds", "calls"):
        value = blob.get(key)
        if value is None:
            continue
        if not isinstance(value, (int, float)) or value < 0:
            return False
    return True


def validate_record(record: dict[str, Any], *, limits: dict[str, Any], ident: str) -> list[str]:
    problems = []
    if not isinstance(record, dict):
        return ["executor record must be a JSON object"]
    if record.get("schema") != RECORD_SCHEMA:
        problems.append(f"schema must be {RECORD_SCHEMA}")
    if record.get("case") != ident:
        problems.append("record.case does not match the started case")
    if record.get("model") != REQUIRED_MODEL:
        problems.append(
            f"model must be {REQUIRED_MODEL!r} (recorded {record.get('model')!r})"
        )
    if record.get("reasoning") != REQUIRED_REASONING:
        problems.append(
            f"reasoning must be {REQUIRED_REASONING!r} (recorded {record.get('reasoning')!r})"
        )
    if record.get("auto_retry") is True or int(record.get("retries") or 0) > 0:
        problems.append("auto-retry is forbidden; record has retries")
    if not _usage_ok(record.get("foreground") or {}):
        problems.append("foreground usage missing or invalid")
    if not _usage_ok(record.get("background") or {}):
        problems.append("background usage missing or invalid")
    fg_calls = (record.get("foreground") or {}).get("calls") or 0
    bg_calls = (record.get("background") or {}).get("calls") or 0
    total_calls = record.get("calls")
    if total_calls is None:
        total_calls = fg_calls + bg_calls
    try:
        total_calls = int(total_calls)
    except (TypeError, ValueError):
        problems.append("calls must be an integer")
        total_calls = 10**9
    if total_calls > int(limits["max_model_calls"]):
        problems.append("model call cap exceeded")
    wall = (record.get("foreground") or {}).get("wall_seconds")
    if isinstance(wall, (int, float)) and wall > int(limits["wall_seconds"]):
        problems.append("foreground wall-clock cap exceeded")
    outcome = record.get("outcome")
    if outcome not in {"completed", "failed", "timeout", "discarded", "no_output"}:
        problems.append("outcome must be completed|failed|timeout|discarded|no_output")
    return problems


def complete(root: Path, ident: str, record_path: Path) -> dict[str, Any]:
    run = load_run(root)
    started = case_dir(root, ident) / "started.json"
    if not started.is_file():
        raise ValueError("complete requires start; phases are not collapsed")
    raw = _read_json(Path(record_path))
    problems = validate_record(raw, limits=run["limits"], ident=ident)
    outcome = raw.get("outcome") if isinstance(raw, dict) else None
    failed_outcomes = {"failed", "timeout", "discarded", "no_output"}
    valid = not problems
    # Timeout/discard/no_output stay in the denominator as completed-failed, not vanished.
    phase = "invalid"
    if valid:
        phase = "completed"
    declared_model = raw.get("model") if isinstance(raw, dict) else None
    declared_reasoning = raw.get("reasoning") if isinstance(raw, dict) else None
    receipt = {
        "schema": SCHEMA,
        "phase": phase,
        "case": ident,
        "at": utc_now(),
        "valid_record": valid,
        "problems": problems,
        "outcome": outcome,
        "in_denominator": True,
        "verification_layer": "executor_manifest",
        "manifest_model": declared_model,
        "manifest_reasoning": declared_reasoning,
        "manifest_matches_required": declared_model == REQUIRED_MODEL
        and declared_reasoning == REQUIRED_REASONING,
        "required_model_verified": False,
        "required_reasoning_verified": False,
        "native_codex_verified": False,
        "auto_retry_declared_zero": isinstance(raw, dict)
        and raw.get("auto_retry") is not True
        and int(raw.get("retries") or 0) == 0,
        "foreground": raw.get("foreground") if isinstance(raw, dict) else None,
        "background": raw.get("background") if isinstance(raw, dict) else None,
        "product_judge": raw.get("product_judge") if isinstance(raw, dict) else None,
        "note": (
            "This runner only validates a supplied executor JSON (schema, declared "
            "model/reasoning strings, caps, no retry). required_model_verified stays "
            "false here. Root verifies native Codex turn_context (gpt-5.6-luna / max, "
            "thread id, immutable hashes, usage). Helpful and quality numbers are not "
            "independent effect evidence."
        ),
    }
    _write_json(case_dir(root, ident) / "completed.json", receipt)
    _write_json(case_dir(root, ident) / "executor-record.json", raw)
    _set_phase(run, ident, phase)
    save_run(root, run)
    if outcome in failed_outcomes and valid:
        receipt["failed_but_counted"] = True
    return receipt


def evidence_check(root: Path, ident: str, artifacts_path: Path | None = None) -> dict[str, Any]:
    run = load_run(root)
    completed = case_dir(root, ident) / "completed.json"
    record = None
    executor = case_dir(root, ident) / "executor-record.json"
    if executor.is_file():
        record = _read_json(executor)
    artifacts = load_artifacts(artifacts_path) if artifacts_path else {}
    product_judge = (record or {}).get("product_judge") if isinstance(record, dict) else None
    result = audit(ident, artifacts=artifacts, product_judge=product_judge, record=record)
    valid = bool(completed.is_file() and _read_json(completed).get("valid_record"))
    if not valid and case_by_id(ident)["requires_model"]:
        result["evidence_verdict"] = result["evidence_verdict"] if artifacts else "unknown"
        result["reasons"] = list(result.get("reasons") or []) + [
            "model case has no valid executor record; not counted as effect success"
        ]
        result["effect_success"] = False
    if valid and result["evidence_verdict"] not in {"contradicts"}:
        _set_phase(run, ident, "valid" if result["evidence_verdict"] == "supports" else "unknown")
    elif valid:
        _set_phase(run, ident, "invalid")
    save_run(root, run)
    _write_json(case_dir(root, ident) / "evidence.json", result)
    return result


def status(root: Path) -> dict[str, Any]:
    record = load_run(root)
    detailed = []
    for item in record["cases"]:
        folder = case_dir(root, item["id"])
        entry = dict(item)
        for name in ("plan", "started", "completed", "evidence"):
            path = folder / f"{name}.json"
            if path.is_file():
                entry[name] = _read_json(path)
        detailed.append(entry)
    return {
        "schema": SCHEMA,
        "required_model": REQUIRED_MODEL,
        "required_reasoning": REQUIRED_REASONING,
        "auto_retry": False,
        "limits": record["limits"],
        "summary": summarize_run(record["cases"]),
        "cases": detailed,
        "invokes_model": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.acceptance_release",
        description="Independent MindIE knowledge acceptance runner (no model invocation).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    plan_p = sub.add_parser("plan")
    plan_p.add_argument("--run-dir", required=True, type=Path)
    plan_p.add_argument("--wall-seconds", type=int, default=DEFAULT_WALL_SECONDS)
    plan_p.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)
    start_p = sub.add_parser("start")
    start_p.add_argument("--run-dir", required=True, type=Path)
    start_p.add_argument("--case", required=True)
    complete_p = sub.add_parser("complete")
    complete_p.add_argument("--run-dir", required=True, type=Path)
    complete_p.add_argument("--case", required=True)
    complete_p.add_argument("--record", required=True, type=Path)
    evidence_p = sub.add_parser("evidence")
    evidence_p.add_argument("--run-dir", required=True, type=Path)
    evidence_p.add_argument("--case", required=True)
    evidence_p.add_argument("--artifacts", type=Path)
    status_p = sub.add_parser("status")
    status_p.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            result = plan(args.run_dir, wall_seconds=args.wall_seconds, max_calls=args.max_calls)
        elif args.command == "start":
            result = start(args.run_dir, args.case)
        elif args.command == "complete":
            result = complete(args.run_dir, args.case, args.record)
        elif args.command == "evidence":
            result = evidence_check(args.run_dir, args.case, args.artifacts)
        else:
            result = status(args.run_dir)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "complete" and not result.get("valid_record"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
