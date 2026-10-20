"""Local Store, scanner and native-process boundaries; no real model/NPU claims."""
from pathlib import Path
import json
import subprocess
import sys
import threading

import pytest

from graded_fixture import OBSERVED, decision, request, unit
from mindie_knowledge.graded.distill import apply_decision, prepare_request, result_view
from mindie_knowledge.graded.units import GradedUnit
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.materials.references import ReadReferenceError


def scanner():
    matches = list((Path.home() / ".cache/mindie-agent/gitleaks/8.30.1").glob("*/gitleaks"))
    if not matches:
        pytest.skip("installed pinned scanner not present; no download in component tests")
    return str(matches[0])


def inputs():
    return [{"kind": "authorized_transcript", "outcome": "observed", "source_locator": "fixture@abcdef1:probe.txt",
             "content": "Fixture graph probe; no hardware claim.", "evidence_refs": ["fixture-observation"], "repetitions": 1}]


def test_install_query_explain_move_update_and_withdrawal(store):
    original = unit()
    store.graded.install([original])
    found = store.query("graph", conditions=OBSERVED)["results"][0]
    assert store.explain(found["ref"])["content"] == original.body
    moved = GradedUnit.from_parts({**original.metadata, "domain_path": "ops/probes"}, original.body, "ops/probes/moved.md")
    store.graded.install([moved])
    assert store.explain(found["ref"])["frontmatter"]["domain_path"] == "ops/probes"
    assert store.explain(found["ref"])["revision"] == 1
    with pytest.raises(ReadReferenceError):
        store.explain(found["ref"].replace("vllm-ascend", "other-domain"))
    changed = GradedUnit.from_parts(moved.metadata, moved.body + "Later correction.\n", moved.path)
    store.graded.install([changed])
    current = store.query("graph", conditions=OBSERVED)["results"][0]
    assert current["revision"] == 2 and current["ref"] != found["ref"]
    with pytest.raises(ReadReferenceError, match="replaced"):
        store.explain(found["ref"])
    store.graded.install([])
    assert store.query("graph", conditions=OBSERVED)["results"] == []
    with pytest.raises(ReadReferenceError, match="absent"):
        store.explain(found["unit_ref"])


def test_failed_graph_install_preserves_current_and_current_data_remains_visible(store):
    original = unit()
    store.graded.install([original])
    bad = unit("unit-bad", parents=["unit-missing"])
    with pytest.raises(ValueError, match="missing inherited"):
        store.graded.install([bad])
    assert store.query("graph", conditions=OBSERVED)["results"][0]["id"] == original.id


def test_c6_local_application_uses_planner_and_is_idempotent(store):
    req = request()
    first = apply_decision(store, req, decision(req))
    assert first["admitted_unit_ids"] == ["unit-fixture-probe"]
    again = apply_decision(store, req, decision(req))
    assert again["admitted_unit_ids"] == []
    assert again["publication_plan"]["status"] == "unchanged"
    changed = unit(conditions={"soc": ["Ascend910C"]})
    proposed = apply_decision(store, req, decision(req, changed, "merge_into_existing"))
    assert proposed["admitted_unit_ids"] == []
    assert proposed["publication_plan"]["reviews"]
    assert store.query("graph", conditions=OBSERVED)["results"][0]["conditions"] == unit().metadata["conditions"]


def test_preparation_redacts_selected_content_before_organizer(store):
    selected = inputs()
    selected[0]["content"] += " host=192.168.20.21 path=/Users/private-person/session.jsonl"
    req = prepare_request(selected, session_digest="a" * 64, task_state="verified_milestone",
                          redactor_executable=scanner(), redaction_key=store.redaction_key())
    assert "192.168.20.21" not in str(req)
    assert "private-person" not in str(req)
    assert "192.168.20.21" in selected[0]["content"]
    assert req["redaction_rules"]


def test_nested_json_evidence_redacts_paths_without_corrupting_measurements(store, monkeypatch):
    from mindie_knowledge.loop import transcript_redaction
    source = dict(source_ref='/Volumes/data/temp/mindie-003-execution/run1/performance_evidence.json',
                  grouping_derivation_ref='/Users/private-person/code/mindie-bench/verifier/measure.py',
                  timing=dict(started_ns=2171894295782027, ended_ns=2171894296069612))
    selected = inputs()
    selected[0]['content'] = json.dumps(source, indent=2)
    calls = []
    run = transcript_redaction.run_argv

    def observed_run(*args, **kwargs):
        calls.append(args[0])
        return run(*args, **kwargs)

    monkeypatch.setattr(transcript_redaction, 'run_argv', observed_run)
    request = prepare_request(selected, session_digest='e' * 64, task_state='completed',
        redactor_executable=scanner(), redaction_key=store.redaction_key(),
        private_paths=('/Volumes/data/temp', '/Users/private-person'))
    cleaned = json.loads(request['sanitized_inputs'][0]['content'])
    assert cleaned['timing'] == source['timing']
    assert 'private-person' not in str(request) and '/Volumes/data/temp' not in str(request)
    assert len(calls) == 1


def test_structured_redaction_masks_embedded_credentials_and_keeps_typed_values(store):
    from mindie_knowledge.loop.transcript_redaction import redact_tree
    value = {'note': 'password=synthetic-passphrase', 'machine': 'aarch64',
             'nested': ['plain text', {'latency': 0.25, 'attempts': 3}]}
    cleaned, rules = redact_tree(value, executable=scanner(), key=store.redaction_key())
    assert cleaned['note'] == 'password=[REDACTED_SECRET]'
    assert cleaned['machine'] == 'aarch64'
    assert cleaned['nested'] == value['nested'] and rules
    assert value['note'] == 'password=synthetic-passphrase'


def test_native_c6_call_then_local_failure_reuses_saved_result(store, tmp_path, monkeypatch):
    calls = tmp_path / "calls.txt"
    monkeypatch.setenv("MINDIE_GRADED_FIXTURE_CALLS", str(calls))
    engine = Engine(store, redactor_executable=scanner(),
                    summary_command=[sys.executable, str(Path(__file__).parent / "fixtures/graded_worker.py")])
    original = store.graded.install

    def fail_local(*args, **kwargs):
        raise OSError("controlled local write failure after native return")

    monkeypatch.setattr(store.graded, "install", fail_local)
    failed = engine.distill_selected(inputs(), session_digest="b" * 64, task_state="verified_milestone")
    assert failed["status"] == "local_application_failed"
    assert failed["model_execution"] == "completed" and failed["model_status"] == "returned"
    assert "local write failure" in failed["local_error"]["message"]
    assert failed["result"]["units"][0]["target_unit_id"] == "unit-fixture-native"
    assert failed["result_admission"] == "local_application_pending"
    assert result_view(failed)[1] is False
    assert calls.read_text() == "1"
    saved = store.db.execute("SELECT status,error FROM material_summary_attempts").fetchone()
    assert tuple(saved) == ("returned", "apply_failed")
    monkeypatch.setattr(store.graded, "install", original)
    result = engine.distill_selected(inputs(), session_digest="b" * 64, task_state="verified_milestone")
    assert result["status"] == "applied" and calls.read_text() == "1"
    again = engine.distill_selected(inputs(), session_digest="b" * 64, task_state="verified_milestone")
    assert again["replay"] == "existing_local_receipt" and calls.read_text() == "1"
    found = store.query("graph", conditions={"soc": "Ascend910B"})["results"][0]
    assert found["id"] == "unit-fixture-native"
    assert store.explain(found["ref"])["content"] == "Fixture graph probe; no hardware claim.\n"


def test_in_flight_observed_only_is_noop_without_a_native_worker(store):
    observed = inputs()
    assert observed[0]["outcome"] == "observed"
    engine = Engine(store, redactor_executable=scanner())
    result = engine.distill_selected(observed, session_digest="c" * 64, task_state="in_flight")
    assert result["status"] == "noop" and result["decision"]["drop_reason"] == "in_flight_incomplete"
    assert store.db.execute("SELECT count(*) FROM material_summary_attempts").fetchone()[0] == 0


def test_native_return_survives_local_receipt_failure_without_replay(store, tmp_path, monkeypatch):
    from mindie_knowledge.materials.summarizer import SummaryLedger
    calls = tmp_path / "calls.txt"
    monkeypatch.setenv("MINDIE_GRADED_FIXTURE_CALLS", str(calls))
    engine = Engine(store, redactor_executable=scanner(),
                    summary_command=[sys.executable, str(Path(__file__).parent / "fixtures/graded_worker.py")])

    def fail_record(*args, **kwargs):
        raise OSError("controlled receipt storage failure")

    monkeypatch.setattr(SummaryLedger, "record", fail_record)
    result = engine.distill_selected(inputs(), session_digest="d" * 64, task_state="verified_milestone")
    assert result["status"] == "local_receipt_failed" and result["recording_failed"]
    assert result["model_execution"] == "completed" and result["model_status"] == "returned"
    assert result["result"]["units"][0]["target_unit_id"] == "unit-fixture-native"
    assert result["result_admission"] == "pending_local_redaction"
    assert result_view(result)[1] is False
    assert calls.read_text() == "1"
    repeated = engine.distill_selected(inputs(), session_digest="d" * 64, task_state="verified_milestone")
    assert repeated["status"] == "invoking" and repeated["model_replayed"] is False
    assert calls.read_text() == "1"


@pytest.mark.parametrize("native_status", ["failed", "outcome_unknown"])
def test_native_failure_with_receipt_fault_stays_failed_and_never_replays(store, tmp_path, monkeypatch, native_status):
    from mindie_knowledge.materials.summarizer import SummaryLedger
    calls = tmp_path / "calls.txt"
    monkeypatch.setenv("MINDIE_GRADED_FIXTURE_CALLS", str(calls))
    monkeypatch.setenv("MINDIE_GRADED_FIXTURE_STATUS", native_status)
    engine = Engine(store, redactor_executable=scanner(),
                    summary_command=[sys.executable, str(Path(__file__).parent / "fixtures/graded_worker.py")])

    def fail_record(*args, **kwargs):
        raise OSError("controlled receipt storage failure")

    monkeypatch.setattr(SummaryLedger, "record", fail_record)
    result = engine.distill_selected(inputs(), session_digest="f" * 64, task_state="completed")
    assert result["status"] == native_status and result["model_status"] == native_status
    assert result_view(result)[1] is True
    assert result["local_error"]["stage"] == "receipt" and "result" not in result
    repeated = engine.distill_selected(inputs(), session_digest="f" * 64, task_state="completed")
    assert repeated["status"] == "invoking" and result_view(repeated)[1] is True
    assert calls.read_text() == "1"


def test_generation_revocation_cancels_before_worker_even_with_rpc_cancellation(store, monkeypatch):
    from mindie_knowledge.graded import worker
    from mindie_knowledge.loop.process import MaintenanceCancelled
    engine = Engine(store, redactor_executable=scanner(),
                    summary_command=[sys.executable, str(Path(__file__).parent / "fixtures/graded_worker.py")])
    original = worker.prepare_request

    def prepare_then_revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        engine.operations.revoke()
        return result

    monkeypatch.setattr(worker, "prepare_request", prepare_then_revoke)
    with pytest.raises(MaintenanceCancelled, match="before start"):
        engine.distill_selected(inputs(), session_digest="e" * 64, task_state="verified_milestone",
                                cancel=threading.Event())
    assert store.db.execute("SELECT count(*) FROM material_summary_attempts").fetchone()[0] == 0


def test_cli_selected_evidence_uses_live_native_lease_and_real_rpc(store, tmp_path, monkeypatch):
    from conftest import admission_token, make_admission, write_settings
    from mindie_knowledge.loop.activation import Admission
    from mindie_knowledge.loop.cli import connection_path
    from mindie_knowledge.loop.lifecycle import config_fingerprint
    from mindie_knowledge.loop.transport import Service

    project = tmp_path / "project"
    project.mkdir()
    settings = tmp_path / "community.json"
    write_settings(settings, consent_choice="contribute", roots=[project])
    admission_path = make_admission(tmp_path, project_root=project)
    admission = Admission(admission_path)
    calls = tmp_path / "native-calls.txt"
    monkeypatch.setenv("MINDIE_GRADED_FIXTURE_CALLS", str(calls))
    config_path = tmp_path / "engine.json"
    config = {"root": str(tmp_path), "domain": store.domain,
              "redactor_executable": scanner(), "community_config": str(settings),
              "transcript_adapter": str(Path(__file__).parent / "transcript_double.py"),
              "summary_command": [sys.executable, str(Path(__file__).parent / "fixtures/graded_worker.py")],
              "admission_path": str(admission_path)}
    config_path.write_text(json.dumps(config))
    engine = Engine(store, settings_path=settings, admission=admission,
                    redactor_executable=scanner(), summary_command=config["summary_command"])
    service = Service(engine, admission=admission, config_path=config_path,
                      config_fingerprint=config_fingerprint(config_path, config))
    connection = connection_path(config)
    connection.parent.mkdir(parents=True, exist_ok=True)
    connection.write_text(json.dumps(service.connection))
    thread = threading.Thread(target=service.http.serve_forever, daemon=True)
    thread.start()
    payload = {"inputs": inputs(), "task_state": "verified_milestone", "_session_id": "manual-A",
               "_activation": admission_token(admission_path)}

    def run(value):
        return subprocess.run([sys.executable, "-m", "mindie_knowledge.loop.cli", "distill", "--config", str(config_path)],
                              input=json.dumps(value), text=True, capture_output=True,
                              cwd=Path(__file__).resolve().parents[1], timeout=10)

    try:
        rejected = run({**payload, "_activation": "wrong-native-lease"})
        assert rejected.returncode == 2 and not calls.exists()
        accepted = run(payload)
        assert accepted.returncode == 0, accepted.stderr
        assert json.loads(accepted.stdout)["status"] == "applied" and calls.read_text() == "1"
        admission.deactivate("manual-A")
        revoked = run(payload)
        assert revoked.returncode == 2 and calls.read_text() == "1"
        found = service.call("query", {"query": "graph", "conditions": {"soc": "Ascend910B"},
                                      "domain_path": "wrong-route", "token_budget": 16,
                                      "_session_id": "manual-A", "_session_verified": True})
        assert found["results"][0]["id"] == "unit-fixture-native"
        assert found["body_token_estimate"] <= 16
    finally:
        service.http.shutdown()
        service.http.server_close()
        thread.join(timeout=2)
