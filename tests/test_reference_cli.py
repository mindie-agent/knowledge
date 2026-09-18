"""Actual maintainer CLI invocations: files, evidence, bounds and error paths."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def reference_inputs(tmp_path):
    notes = tmp_path / "notes"
    code = tmp_path / "code"
    notes.mkdir()
    code.mkdir()
    (code / "model.py").write_text("def replay(value):\n    return value + 1\n", encoding="utf-8")
    (notes / "acl.md").write_text(f"# ACL graph\n\nHCCL 910B graph replay reference.\n\n[Source]({(code / 'model.py').as_uri()})\n", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"backend": "off", "state_root": str(tmp_path / "state"),
                                  "layers": {"shared": {"enabled": False}, "candidate": {"enabled": False}, "project": str(notes)}}), encoding="utf-8")
    return {"root": tmp_path, "notes": notes, "code": code, "config": config, "state": tmp_path / "state"}


def run_cli(inputs, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith("MINDIE_KNOWLEDGE_")}
    # A deliberately missing config must never fall into the user's own state.
    env["MINDIE_KNOWLEDGE_STATE"] = str(inputs["state"])
    env["MINDIE_KNOWLEDGE_LAYERS"] = "project"
    env["MINDIE_KNOWLEDGE_PROJECT_ROOTS"] = str(inputs["notes"])
    env["MINDIE_DIAGNOSTICS_ROOT"] = str(inputs["root"] / "diagnostics")
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run([sys.executable, "-X", "utf8", "-m", "mindie_knowledge", *map(str, args)],
                          cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)


def decoded(result):
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_catalog_cli_reuses_snapshot_without_starting_vectors(reference_inputs):
    inputs = reference_inputs
    first = decoded(run_cli(inputs, "catalog", "--config", inputs["config"]))
    second = decoded(run_cli(inputs, "catalog", "--config", inputs["config"]))
    assert first["parsed"] == 1
    assert second["parsed"] == 0
    assert second["unchanged"] == 1
    assert not (inputs["state"] / "local.json").exists()
    assert not (inputs["state"] / "embedding-cache").exists()
    assert not (inputs["state"] / "backend").exists()


def test_evaluate_default_stdout_stays_compact_and_full_rows_are_retained(reference_inputs):
    inputs = reference_inputs
    decoded(run_cli(inputs, "catalog", "--config", inputs["config"]))
    cases = inputs["root"] / "cases.json"
    cases.write_text(json.dumps([{"id": str(index), "query": (f"ACL graph {index} " * 180), "no_evidence": False}
                                 for index in range(40)]), encoding="utf-8")
    result = run_cli(inputs, "evaluate", "--config", inputs["config"], "--cases", cases)
    report = decoded(result)
    assert len(result.stdout.encode("utf-8")) <= 16_384
    assert report["summary"]["queries"] == 40
    artifact = Path(report["output"])
    assert artifact.is_file()
    complete = json.loads(artifact.read_text(encoding="utf-8"))
    assert len(complete["cases"]) == 40
    assert complete["cases"][0]["query"] == "ACL graph 0 " * 179 + "ACL graph 0"


def test_evaluate_explicit_artifact_and_baseline_execute_real_query(reference_inputs):
    inputs = reference_inputs
    decoded(run_cli(inputs, "catalog", "--config", inputs["config"]))
    cases = inputs["root"] / "cases.json"
    cases.write_text(json.dumps([{"id": "hccl", "query": "HCCL 910B", "relevant": {"viking://resources/project/acl.md": 3}}]), encoding="utf-8")
    output = inputs["root"] / "report.json"
    decoded(run_cli(inputs, "evaluate", "--config", inputs["config"], "--cases", cases, "--output", output))
    baseline = inputs["root"] / "baseline.json"
    baseline.write_bytes(output.read_bytes())
    report = decoded(run_cli(inputs, "evaluate", "--config", inputs["config"], "--cases", cases, "--output", output, "--baseline", baseline))
    assert report["summary"]["recall"] == 1
    assert report["summary"]["evidence_accuracy"] == 1
    assert report["comparison"]["recall"] == {"baseline": 1, "current": 1}


@pytest.mark.parametrize("kind", ["invalid-shape", "missing"])
def test_bad_explicit_config_does_not_fall_back_or_emit_traceback(reference_inputs, kind):
    inputs = reference_inputs
    path = inputs["root"] / "bad.json"
    if kind == "invalid-shape":
        path.write_text('{"layers":"invalid"}', encoding="utf-8")
    result = run_cli(inputs, "catalog", "--config", path)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert json.loads(result.stdout).get("reason")
    assert not (inputs["state"] / "reference-catalog.sqlite3").exists()


def test_map_compare_requires_an_object_and_requested_changes_need_before(reference_inputs):
    inputs = reference_inputs
    before = inputs["root"] / "invalid-map.json"
    before.write_text("[]", encoding="utf-8")
    state = inputs["root"] / "map-state"
    result = run_cli(inputs, "code-map", "--root", inputs["code"], "--state", state, "--before", before)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert json.loads(result.stdout).get("reason")
    output = inputs["root"] / "changes.json"
    result = run_cli(inputs, "code-map", "--root", inputs["code"], "--state", state, "--changes-output", output)
    assert result.returncode != 0
    assert not output.exists()


def test_code_map_changes_and_relations_cli_use_actual_source_evidence(reference_inputs):
    inputs = reference_inputs
    root = inputs["root"]
    before, after, changes = (root / name for name in ("before.json", "after.json", "changes.json"))
    common = ("code-map", "--root", inputs["code"], "--state", root / "map-state")
    decoded(run_cli(inputs, *common, "--output", before))
    source = inputs["code"] / "model.py"
    source.write_text("def replay(value):\n    return value + 2\n", encoding="utf-8")
    report = decoded(run_cli(inputs, *common, "--before", before, "--changes-output", changes, "--output", after))
    assert report["changes"]["changed"] == 1
    assert json.loads(changes.read_text(encoding="utf-8"))["changed"] == ["model.py"]
    decoded(run_cli(inputs, "catalog", "--config", inputs["config"]))
    relations = root / "relations.json"
    report = decoded(run_cli(inputs, "relations", "--config", inputs["config"], "--ref", "viking://resources/project/acl.md",
                             "--code-map", after, "--changes", changes, "--output", relations))
    assert report["missing_refs"] == []
    assert relations.is_file()
    assert report["documents"], report
    assert report["documents"][0]["document"] == "viking://resources/project/acl.md"


@pytest.mark.parametrize("cases", [
    [{"id": str(index), "query": "HCCL"} for index in range(501)],
    [{"id": "long", "query": "x" * 4001}],
    [{"id": "nested", "query": {"unexpected": "HCCL"}}],
    [{"id": "labels", "query": "HCCL", "relevant": ["invalid"]}],
])
def test_evaluation_rejects_out_of_budget_or_malformed_cases_before_search(reference_inputs, cases):
    inputs = reference_inputs
    path = inputs["root"] / "cases.json"
    path.write_text(json.dumps(cases), encoding="utf-8")
    result = run_cli(inputs, "evaluate", "--config", inputs["config"], "--cases", path)
    assert result.returncode == 1
    assert json.loads(result.stdout)["status"] == "incomplete"
    assert "Traceback" not in result.stderr
    assert not (inputs["state"] / "evaluations").exists()


def test_evaluation_deadline_preserves_artifact_without_scoring_unexecuted_queries(reference_inputs):
    inputs = reference_inputs
    path = inputs["root"] / "cases.json"
    path.write_text(json.dumps([{"id": "pending", "query": "HCCL", "no_evidence": True}]), encoding="utf-8")
    result = run_cli(inputs, "evaluate", "--config", inputs["config"], "--cases", path, "--max-seconds", "0.000000001")
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["status"] == "incomplete"
    assert report["remaining_queries"] == 1
    assert report["summary"]["queries"] == 0
    assert report["summary"]["no_evidence_abstention"] is None
    full = json.loads(Path(report["output"]).read_text(encoding="utf-8"))
    assert full["cases"] == []
    assert full["complete"] is False


def test_catalog_cli_reports_real_busy_and_parse_failure_as_not_ready(reference_inputs):
    from mindie_knowledge.distribution.sync import SwitchLock
    inputs = reference_inputs
    lock = SwitchLock(inputs["state"] / "reference-catalog.lock")
    lock.acquire()
    try:
        result = run_cli(inputs, "catalog", "--config", inputs["config"])
    finally:
        lock.release()
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "busy"
    assert json.loads(result.stdout)["ready"] is False
    (inputs["notes"] / "invalid.md").write_bytes(b"# HCCL\n\xff\xfe\x00")
    result = run_cli(inputs, "catalog", "--config", inputs["config"])
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "partial"
    assert json.loads(result.stdout)["ready"] is False


def test_map_navigation_stdout_is_bounded_with_full_graph_retained(reference_inputs):
    inputs = reference_inputs
    source = inputs["code"] / "model.py"
    source.write_text("\n".join(f"def replay_{index}(value):\n    return value + {index}\n" for index in range(100)), encoding="utf-8")
    result = run_cli(inputs, "code-map", "--root", inputs["code"], "--state", inputs["root"] / "map-state",
                     "--symbol", "replay", "--limit", 1000)
    report = decoded(result)
    assert len(result.stdout.encode("utf-8")) <= 16_384
    full = json.loads(Path(report["output"]).read_text(encoding="utf-8"))
    assert len([node for node in full["nodes"] if node["kind"] == "function"]) == 100
    assert report["stdout_truncated"] is True
    navigation = json.loads(Path(report["report_output"]).read_text(encoding="utf-8"))
    assert len(navigation["matches"]) == 100


def test_malformed_nested_map_and_nonfinite_time_limit_are_rejected(reference_inputs):
    inputs = reference_inputs
    path = inputs["root"] / "bad.json"
    path.write_text(json.dumps({"source_id": "fixture", "root": str(inputs["code"]), "files": [None]}), encoding="utf-8")
    result = run_cli(inputs, "relations", "--config", inputs["config"], "--ref", "viking://resources/project/acl.md", "--code-map", path)
    assert result.returncode == 1
    assert json.loads(result.stdout)["reason"]
    assert "Traceback" not in result.stderr
    result = run_cli(inputs, "code-map", "--root", inputs["code"], "--state", inputs["root"] / "map-state", "--max-seconds", "nan")
    assert result.returncode == 1
    assert not (inputs["root"] / "map-state").exists()
