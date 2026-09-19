from __future__ import annotations

import json
from pathlib import Path

from tests.acceptance_release.cases import REQUIRED_MODEL, REQUIRED_REASONING
from tests.acceptance_release.runner import complete, main, plan, start, status, validate_record


def _record(case: str, **overrides):
    payload = {
        "schema": "mindie-acceptance-record/1",
        "case": case,
        "model": REQUIRED_MODEL,
        "reasoning": REQUIRED_REASONING,
        "auto_retry": False,
        "retries": 0,
        "calls": 1,
        "outcome": "completed",
        "foreground": {"input_tokens": 10, "output_tokens": 5, "cached_input_tokens": 2, "wall_seconds": 1.5, "calls": 1},
        "background": {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "wall_seconds": 0, "calls": 0},
        "product_judge": {"verdict": "unknown", "reason": "not used as effect proof"},
    }
    payload.update(overrides)
    return payload


def test_phases_and_model_pin(tmp_path):
    root = tmp_path / "run"
    planned = plan(root, wall_seconds=30, max_calls=3)
    assert planned["required_model"] == REQUIRED_MODEL
    assert planned["required_reasoning"] == REQUIRED_REASONING
    assert planned["auto_retry"] is False
    assert planned["summary"]["phases"]["planned"] >= 1
    launch = start(root, "abc-effect")
    assert launch["invoked_model"] is False
    assert launch["phase"] == "started"
    record_path = tmp_path / "ok.json"
    record_path.write_text(json.dumps(_record("abc-effect")), encoding="utf-8")
    done = complete(root, "abc-effect", record_path)
    assert done["valid_record"] is True
    assert done["manifest_matches_required"] is True
    assert done["verification_layer"] == "executor_manifest"
    assert done["required_model_verified"] is False
    assert done["required_reasoning_verified"] is False
    assert done["native_codex_verified"] is False
    assert done["phase"] == "completed"
    summary = status(root)["summary"]
    assert summary["helpful_is_not_effect_success"]
    assert summary["denominator_includes_timeout_discard_no_output"]


def test_wrong_model_and_retry_are_invalid(tmp_path):
    root = tmp_path / "run"
    plan(root)
    start(root, "abc-effect")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_record("abc-effect", model="gpt-4")), encoding="utf-8")
    done = complete(root, "abc-effect", bad)
    assert done["valid_record"] is False
    assert done["phase"] == "invalid"
    start(root, "success-no-contribution")
    retry = tmp_path / "retry.json"
    retry.write_text(json.dumps(_record("success-no-contribution", retries=1)), encoding="utf-8")
    done = complete(root, "success-no-contribution", retry)
    assert done["valid_record"] is False


def test_timeout_stays_in_denominator(tmp_path):
    root = tmp_path / "run"
    plan(root, wall_seconds=60, max_calls=2)
    start(root, "abc-effect")
    record = _record("abc-effect", outcome="timeout", calls=2)
    path = tmp_path / "timeout.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    done = complete(root, "abc-effect", path)
    assert done["valid_record"] is True
    assert done["in_denominator"] is True
    assert done["failed_but_counted"] is True


def test_complete_without_start_fails(tmp_path):
    root = tmp_path / "run"
    plan(root)
    path = tmp_path / "ok.json"
    path.write_text(json.dumps(_record("abc-effect")), encoding="utf-8")
    assert main(["complete", "--run-dir", str(root), "--case", "abc-effect", "--record", str(path)]) == 2


def test_validate_record_caps():
    limits = {"wall_seconds": 10, "max_model_calls": 1}
    problems = validate_record(
        _record("abc-effect", calls=4, foreground={"input_tokens": 1, "output_tokens": 1, "wall_seconds": 1, "calls": 4}),
        limits=limits,
        ident="abc-effect",
    )
    assert any("call cap" in item for item in problems)
