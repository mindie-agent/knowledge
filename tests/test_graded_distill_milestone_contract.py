"""Behavioral C6 in-flight milestone contract through the real organizer request.

The organizer used here is a deterministic subprocess. It consumes the real
prepared worker request, validates it with the shared runtime, builds the real
prompt/schema, and emits a decision derived from the request. It never calls a
model, so these tests verify the production request/prompt/admission data
contract, not model behavior or prompt-following quality.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from mindie_knowledge.graded import distill
from mindie_knowledge.loop.engine import Engine

ROOT = Path(__file__).resolve().parents[1]

ORGANIZER = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, __ROOT__)
from mindie_knowledge.graded import distill
from mindie_knowledge.materials.summarizer import canonical, digest, outcome, policy_identity

identity = policy_identity(model="deterministic-test-organizer", effort="none",
                           implementation={"fixture": "in-flight-milestone-contract-v1"})
identity["capabilities"] = ["mindie-distill-request/1"]
identity["fingerprint"] = digest({key: value for key, value in identity.items() if key != "fingerprint"})
if "--identity" in sys.argv:
    print(canonical(identity))
    raise SystemExit(0)

request = distill.validate_request(json.load(sys.stdin))
gate = distill.value_gate(request)
prompt = distill.build_prompt(request)
start = prompt.find('{"admission"')
if start < 0:
    start = prompt.find('{"output_schema"')
payload = json.loads(prompt[start:]) if start >= 0 else {}
sidecar = {"task_state": request["task_state"], "gate": gate, "prompt": prompt,
           "payload": payload, "sanitized_inputs": request["sanitized_inputs"]}
Path(os.environ["MINDIE_TEST_ORGANIZER_SIDECAR"]).write_text(
    json.dumps(sidecar, indent=2), encoding="utf-8")

calls = Path(os.environ["MINDIE_TEST_ORGANIZER_CALLS"])
calls.write_text(str(int(calls.read_text()) + 1) if calls.exists() else "1", encoding="utf-8")

if os.environ.get("MINDIE_TEST_ORGANIZER_NOOP"):
    reason = os.environ["MINDIE_TEST_ORGANIZER_NOOP"]
    print(canonical(outcome(request, status="returned",
                            result=distill.noop_decision(request, reason),
                            model_calls=1, usage_known=False, elapsed_ms=1)))
    raise SystemExit(0)

inputs = request["sanitized_inputs"]
source = next(item for item in inputs if item["outcome"] != "in_flight")
body = source["content"].strip() + "\n\nVerified partial milestone recorded for the selected finding.\n"
frontmatter = {
    "schema": "mindie-graded-unit/1",
    "id": "unit-milestone-contract",
    "tier": "L2",
    "category": "sop",
    "evidence_strength": "single_observation",
    "title": "Verified partial milestone",
    "domain_path": "serving/graph",
    "inherits_from": [],
    "conditions": {"soc": ["Ascend910B"]},
    "triggers": {"error_signatures": [], "keywords": ["milestone"]},
    "provenance": {
        "source_type": "session_distill",
        "source_locator": source["source_locator"],
        "source_digest": source["source_digest"],
        "evidence_refs": source["evidence_refs"],
        "supersedes": [],
    },
}
decision = {
    "schema_version": "mindie-distill-decision/1",
    "session_digest": request["session_digest"],
    "evidence_inputs": request["evidence_inputs"],
    "action": "propose_graded_units",
    "drop_reason": None,
    "units": [{
        "target_unit_id": frontmatter["id"],
        "target_tier": frontmatter["tier"],
        "category": frontmatter["category"],
        "evidence_strength": frontmatter["evidence_strength"],
        "domain_path": frontmatter["domain_path"],
        "operation": "create",
        "frontmatter": frontmatter,
        "markdown_body": body,
    }],
}
print(canonical(outcome(request, status="returned", result=decision, model_calls=1,
                        usage_known=False, elapsed_ms=1)))
'''.replace("__ROOT__", repr(str(ROOT)))


def _scanner(tmp_path):
    path = tmp_path / "fake-gitleaks"
    path.write_text("#!/bin/sh\ncat >/dev/null\nprintf '[]'\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _verified():
    return {
        "kind": "authorized_transcript",
        "outcome": "verified",
        "source_locator": "fixture@abcdef1:milestone.md",
        "content": (
            "Verified partial milestone: the graph-capture probe reproduced the same "
            "boundary result in two completed observations. The wider task is still "
            "running other branches, but this selected finding is finished. " * 4
        ).strip(),
        "evidence_refs": ["run-verified-1", "run-verified-2"],
        "repetitions": 2,
    }


def _short_verified():
    return {
        "kind": "authorized_transcript",
        "outcome": "verified",
        "source_locator": "fixture@abcdef1:short-milestone.md",
        "content": "Graph capture matched its recorded boundary.",
        "evidence_refs": ["run-short-verified-1"],
        "repetitions": 1,
    }


def _trivial_verified_qa():
    return {
        "kind": "authorized_transcript",
        "outcome": "verified",
        "source_locator": "fixture@abcdef1:trivial-qa.md",
        "content": "Graph capture works.",
        "evidence_refs": [],
        "repetitions": 1,
    }


def _pending():
    return {
        "kind": "authorized_transcript",
        "outcome": "in_flight",
        "source_locator": "fixture@abcdef1:pending.md",
        "content": "Still-running probe; no completed measurement or verified result yet.",
        "evidence_refs": ["run-pending-1"],
        "repetitions": 1,
    }


def _observed():
    return {
        "kind": "authorized_transcript",
        "outcome": "observed",
        "source_locator": "fixture@abcdef1:observed.md",
        "content": "Single preliminary observation; the wider task is still running.",
        "evidence_refs": ["run-observed-1"],
        "repetitions": 1,
    }


def _request(store, tmp_path, inputs, *, task_state):
    return distill.prepare_request(inputs, session_digest="a" * 64, task_state=task_state,
                                   redactor_executable=_scanner(tmp_path),
                                   redaction_key=store.redaction_key())


def _prompt_payload(prompt):
    start = prompt.find('{"admission"')
    if start < 0:
        start = prompt.find('{"output_schema"')
    assert start >= 0, "organizer prompt has no structured payload"
    return json.loads(prompt[start:])


def _engine(store, tmp_path, monkeypatch):
    calls = tmp_path / "organizer-calls.txt"
    sidecar = tmp_path / "organizer-sidecar.json"
    monkeypatch.setenv("MINDIE_TEST_ORGANIZER_CALLS", str(calls))
    monkeypatch.setenv("MINDIE_TEST_ORGANIZER_SIDECAR", str(sidecar))
    engine = Engine(store, redactor_executable=_scanner(tmp_path),
                    summary_command=[sys.executable, "-c", ORGANIZER])
    return engine, calls, sidecar


def _decision_for(request, *, cite_pending=False):
    inputs = request["sanitized_inputs"]
    source = next(item for item in inputs
                  if (item["outcome"] == "in_flight") == cite_pending)
    frontmatter = {
        "schema": "mindie-graded-unit/1",
        "id": "unit-milestone-contract",
        "tier": "L2",
        "category": "sop",
        "evidence_strength": "single_observation",
        "title": "Verified partial milestone",
        "domain_path": "serving/graph",
        "inherits_from": [],
        "conditions": {"soc": ["Ascend910B"]},
        "triggers": {"error_signatures": [], "keywords": ["milestone"]},
        "provenance": {
            "source_type": "session_distill",
            "source_locator": source["source_locator"],
            "source_digest": source["source_digest"],
            "evidence_refs": source["evidence_refs"],
            "supersedes": [],
        },
    }
    return {
        "schema_version": "mindie-distill-decision/1",
        "session_digest": request["session_digest"],
        "evidence_inputs": request["evidence_inputs"],
        "action": "propose_graded_units",
        "drop_reason": None,
        "units": [{
            "target_unit_id": frontmatter["id"],
            "target_tier": frontmatter["tier"],
            "category": frontmatter["category"],
            "evidence_strength": frontmatter["evidence_strength"],
            "domain_path": frontmatter["domain_path"],
            "operation": "create",
            "frontmatter": frontmatter,
            "markdown_body": source["content"].strip() + "\n\nVerified partial milestone.\n",
        }],
    }


def test_in_flight_verified_milestone_prompt_admission_matches_gate(store, tmp_path):
    request = _request(store, tmp_path, [_verified(), _pending()], task_state="in_flight")
    assert distill.value_gate(request) is None

    prompt = distill.build_prompt(request)
    payload = _prompt_payload(prompt)
    assert payload["admission"] == {
        "task_state": "in_flight",
        "admitted": True,
        "drop_reason": None,
        "in_flight_milestone_admitted": True,
        "stop_event_is_completion": False,
        "in_flight_inputs_are_evidence": False,
    }
    assert payload["request"]["task_state"] == "in_flight"
    assert payload["request"]["sanitized_inputs"] == request["sanitized_inputs"]
    assert payload["request"]["request_digest"] == request["request_digest"]


def test_short_referenced_verified_milestone_is_admitted_without_pending_progress(store, tmp_path):
    short = _short_verified()
    assert len(short["content"].encode()) < 120 and short["evidence_refs"]
    alone = _request(store, tmp_path, [short], task_state="in_flight")
    with_pending = _request(store, tmp_path, [short, _pending()], task_state="in_flight")

    assert distill.value_gate(alone) is None
    assert distill.value_gate(with_pending) is None
    payload = _prompt_payload(distill.build_prompt(alone))
    assert payload["admission"]["admitted"] is True
    assert payload["admission"]["in_flight_milestone_admitted"] is True


def test_short_referenced_verified_milestone_applies_without_pending_progress(
        store, tmp_path, monkeypatch):
    short = _short_verified()
    engine, calls, sidecar = _engine(store, tmp_path, monkeypatch)
    result = engine.distill_selected([short], session_digest="e" * 64, task_state="in_flight")

    assert result["status"] == "applied"
    assert result["admitted_unit_ids"] == ["unit-milestone-contract"]
    assert result["model_calls"] == 1
    assert calls.read_text(encoding="utf-8") == "1"
    evidence = json.loads(sidecar.read_text(encoding="utf-8"))
    assert evidence["gate"] is None
    assert evidence["payload"]["admission"]["admitted"] is True
    assert evidence["payload"]["admission"]["in_flight_milestone_admitted"] is True
    found = store.query("milestone", conditions={"soc": "Ascend910B"})["results"]
    assert [row["id"] for row in found] == ["unit-milestone-contract"]
    provenance = store.explain("mindie://vllm-ascend/units/unit-milestone-contract@1")[
        "frontmatter"]["provenance"]
    assert provenance["source_locator"] == short["source_locator"]
    assert provenance["evidence_refs"] == short["evidence_refs"]


def test_trivial_unreferenced_verified_qa_is_filtered_before_the_worker(
        store, tmp_path, monkeypatch):
    item = _trivial_verified_qa()
    request = _request(store, tmp_path, [item], task_state="in_flight")
    assert len(item["content"].encode()) <= 512 and item["evidence_refs"] == []
    assert distill.value_gate(request) == "routine_qa_or_unverified"
    with pytest.raises(ValueError, match="requires noop: routine_qa_or_unverified"):
        distill.validate_decision(_decision_for(request), request)

    engine, calls, sidecar = _engine(store, tmp_path, monkeypatch)
    result = engine.distill_selected([item], session_digest="f" * 64, task_state="in_flight")
    assert result["status"] == "noop"
    assert result["decision"]["drop_reason"] == "routine_qa_or_unverified"
    assert not calls.exists() and not sidecar.exists()
    assert store.db.execute("SELECT count(*) FROM material_summary_attempts").fetchone()[0] == 0


@pytest.mark.parametrize("inputs", [[_pending()], [_observed()]])
def test_blocked_in_flight_request_prompt_exposes_the_noop_contract(store, tmp_path, inputs):
    request = _request(store, tmp_path, inputs, task_state="in_flight")
    assert distill.value_gate(request) == "in_flight_incomplete"

    prompt = distill.build_prompt(request)
    payload = _prompt_payload(prompt)
    assert payload["admission"]["admitted"] is False
    assert payload["admission"]["drop_reason"] == "in_flight_incomplete"
    assert payload["admission"]["in_flight_milestone_admitted"] is False
    assert payload["admission"]["stop_event_is_completion"] is False
    assert payload["admission"]["in_flight_inputs_are_evidence"] is False


def test_real_organizer_request_prompt_and_admission_apply_verified_milestone(
        store, tmp_path, monkeypatch):
    engine, calls, sidecar = _engine(store, tmp_path, monkeypatch)
    result = engine.distill_selected([_verified(), _pending()], session_digest="a" * 64,
                                     task_state="in_flight")

    assert result["status"] == "applied"
    assert result["admitted_unit_ids"] == ["unit-milestone-contract"]
    assert result["model_calls"] == 1
    assert calls.read_text(encoding="utf-8") == "1"
    evidence = json.loads(sidecar.read_text(encoding="utf-8"))
    assert evidence["task_state"] == "in_flight"
    assert evidence["gate"] is None
    assert evidence["payload"]["admission"]["admitted"] is True
    assert evidence["payload"]["admission"]["in_flight_milestone_admitted"] is True
    assert evidence["payload"]["request"]["task_state"] == "in_flight"
    assert evidence["payload"]["request"]["sanitized_inputs"] == evidence["sanitized_inputs"]
    found = store.query("milestone", conditions={"soc": "Ascend910B"})["results"]
    assert [row["id"] for row in found] == ["unit-milestone-contract"]
    explained = store.explain("mindie://vllm-ascend/units/unit-milestone-contract@1")
    provenance = explained["frontmatter"]["provenance"]
    assert provenance["source_locator"] == "fixture@abcdef1:milestone.md"
    assert provenance["evidence_refs"] == ["run-verified-1", "run-verified-2"]


@pytest.mark.parametrize("reason", ["already_covered", "routine_qa_or_unverified"])
def test_admitted_in_flight_milestone_may_noop_for_dedup_or_insufficient_value(
        store, tmp_path, monkeypatch, reason):
    engine, calls, sidecar = _engine(store, tmp_path, monkeypatch)
    monkeypatch.setenv("MINDIE_TEST_ORGANIZER_NOOP", reason)
    result = engine.distill_selected([_verified(), _pending()], session_digest="c" * 64,
                                     task_state="in_flight")

    assert result["status"] == "noop"
    assert result["decision"]["drop_reason"] == reason
    assert result["model_calls"] == 1
    assert calls.read_text(encoding="utf-8") == "1"
    evidence = json.loads(sidecar.read_text(encoding="utf-8"))
    assert evidence["payload"]["admission"]["admitted"] is True
    assert evidence["payload"]["admission"]["in_flight_milestone_admitted"] is True
    assert store.query("milestone", conditions={"soc": "Ascend910B"})["results"] == []


@pytest.mark.parametrize("inputs", [[_pending()], [_observed()]])
def test_in_flight_without_verified_milestone_is_noop_without_organizer_call(
        store, tmp_path, monkeypatch, inputs):
    engine, calls, sidecar = _engine(store, tmp_path, monkeypatch)
    result = engine.distill_selected(inputs, session_digest="b" * 64, task_state="in_flight")

    assert result["status"] == "noop"
    assert result["decision"]["drop_reason"] == "in_flight_incomplete"
    assert not calls.exists()
    assert not sidecar.exists()
    assert store.db.execute("SELECT count(*) FROM material_summary_attempts").fetchone()[0] == 0


def test_prompt_admission_does_not_relax_pending_provenance_gate(store, tmp_path):
    request = _request(store, tmp_path, [_verified(), _pending()], task_state="in_flight")
    payload = _prompt_payload(distill.build_prompt(request))
    assert payload["admission"]["admitted"] is True
    assert payload["admission"]["in_flight_milestone_admitted"] is True

    with pytest.raises(ValueError, match="selected evidence"):
        distill.validate_decision(_decision_for(request, cite_pending=True), request)
    assert distill.validate_decision(_decision_for(request), request)["action"] == "propose_graded_units"


def test_decision_envelope_keeps_the_unused_in_flight_kind(store, tmp_path):
    verified = {**_verified(), "kind": "ecosystem_repo"}
    pending = {**_pending(), "kind": "experiment_ledger"}
    request = _request(store, tmp_path, [verified, pending], task_state="in_flight")
    assert request["evidence_inputs"] == ["ecosystem_repo", "experiment_ledger"]

    payload = _prompt_payload(distill.build_prompt(request))
    assert payload["request"]["evidence_inputs"] == request["evidence_inputs"]

    decision = _decision_for(request)
    assert decision["evidence_inputs"] == request["evidence_inputs"]
    assert distill.validate_decision(decision, request)["action"] == "propose_graded_units"

    conflated = json.loads(json.dumps(decision))
    conflated["evidence_inputs"] = ["ecosystem_repo"]
    with pytest.raises(ValueError, match="bind the prepared session and selected source kinds"):
        distill.validate_decision(conflated, request)
