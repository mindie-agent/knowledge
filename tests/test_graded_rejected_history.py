"""Real local history, organizer process and static contract admission tests."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import jsonschema
import pytest

from graded_fixture import decision, request, unit
from mindie_knowledge.graded.distill import (
    apply_decision, build_prompt, decision_schema, prepare_request, validate_decision,
)
from mindie_knowledge.graded.impact import ImpactCompletenessError
from mindie_knowledge.graded.store import GradedStore, MAX_HISTORY_BYTES
from mindie_knowledge.loop.engine import Engine
from test_graded_runtime import inputs, scanner


def prepared(store, **kwargs):
    return prepare_request(
        inputs(), session_digest="a" * 64, task_state="verified_milestone",
        redactor_executable=scanner(), redaction_key=store.redaction_key(),
        graded_store=GradedStore(store), **kwargs,
    )


def test_history_queries_newest_five_exact_topic_target_and_preserves_negative_constraints(store):
    history = GradedStore(store)
    for number in range(8):
        history.record_candidate(
            topic="graph", target_signature="a3-capture", unit_id="unit-graph-probe",
            gate_reason=f"observer effect in run {number}", status="rolled_back" if number == 7 else "rejected",
            missing_counterexample_conditions=["uninstrumented request order"],
            candidate_summary=f"Add dump point {number}",
        )
    history.record_candidate(topic="graph", target_signature="different-target", unit_id="unit-other",
                             gate_reason="unrelated target")
    history.record_candidate(topic="different-topic", target_signature="a3-capture", unit_id="unit-other",
                             gate_reason="unrelated topic")
    req = prepared(store, topic="graph", target_signature="a3-capture")
    rows = req["rejected_history"]
    assert [row["gate_reason"] for row in rows] == [f"observer effect in run {n}" for n in range(7, 2, -1)]
    assert rows[0]["status"] == "rolled_back"
    assert rows[0]["unit_id"] == "unit-graph-probe"
    assert rows[0]["missing_counterexample_conditions"] == ["uninstrumented request order"]
    payload = json.loads(build_prompt(req).split("\n\n", 1)[1])
    assert payload["request"]["rejected_history"] == rows
    assert len(json.dumps(rows).encode()) <= 5 * MAX_HISTORY_BYTES
    # A caller cannot increase the store query's hard prompt-size limit.
    assert len(history.recent_rejected(topic="graph", target_signature="a3-capture", limit=100)) == 5


def test_history_is_redacted_and_bound_to_request_digest(store):
    GradedStore(store).record_candidate(
        topic="graph", unit_id="unit-graph-probe",
        gate_reason="failed request to 192.168.20.21 password=synthetic-passphrase",
    )
    req = prepared(store, topic="graph")
    assert "192.168.20.21" not in json.dumps(req)
    assert "synthetic-passphrase" not in json.dumps(req)
    assert req["redaction_rules"]
    changed = deepcopy(req)
    changed["rejected_history"][0]["gate_reason"] = "approved instead"
    with pytest.raises(ValueError, match="prepared evidence request changed"):
        build_prompt(changed)


@pytest.mark.parametrize("signal", ["shared_contracts", "cross_component_parameters", "body", "evidence"])
def test_shared_contract_without_impact_is_rejected_even_when_candidate_omits_signal(signal):
    req = request()
    value = decision(req)
    row = value["units"][0]
    if signal == "body":
        row["markdown_body"] = "Change the shared runtime contract for drain timing."
    elif signal == "evidence":
        from mindie_knowledge.graded.distill import _sha
        req["sanitized_inputs"][0]["content"] = "Change cross-component parameters for drain timing."
        req["request_digest"] = _sha({key: val for key, val in req.items() if key != "request_digest"})
    else:
        row["frontmatter"][signal] = ["drain-timeout"]
    with pytest.raises(ImpactCompletenessError, match="cross_component_impact_incomplete"):
        validate_decision(value, req)


@pytest.mark.parametrize("coverage", [
    {"impact_surface": ["remote-dev", "coordinator"]},
    {"validation_scope": ["remote worker and coordinator lease drain"]},
    {"reverification_commands": ["uv run pytest tests/test_shared_drain.py"]},
])
def test_shared_contract_with_inspectable_scope_or_command_is_admitted(coverage):
    req = request()
    value = decision(req)
    value["units"][0]["frontmatter"].update(shared_contracts=["worker-drain"], **coverage)
    jsonschema.validate(value, decision_schema())
    admitted = validate_decision(value, req)
    assert all(admitted["units"][0]["frontmatter"][key] == val for key, val in coverage.items())
    # Existing local cards have no mandatory impact declaration.
    jsonschema.validate(decision(req), decision_schema())
    assert validate_decision(decision(req), req)["action"] == "propose_graded_units"


def test_static_rejection_survives_and_next_evidence_request_reads_it(store):
    req = prepared(store, topic="graph", target_signature="a3-capture")
    value = decision(request())
    source = req["sanitized_inputs"][0]
    value["units"][0]["frontmatter"]["provenance"].update(
        source_type=source["kind"], source_locator=source["source_locator"], source_digest=source["source_digest"])
    value["units"][0]["frontmatter"]["shared_contracts"] = ["worker-drain"]
    with pytest.raises(ImpactCompletenessError):
        apply_decision(store, req, value)
    assert not store.graded.available()
    next_req = prepared(store, topic="graph", target_signature="a3-capture")
    rejected = next_req["rejected_history"][0]
    assert rejected["unit_id"] == "unit-fixture-probe"
    assert rejected["gate_reason"].startswith("cross_component_impact_incomplete:")
    assert "impact_surface" in rejected["missing_counterexample_conditions"][0]


def test_create_collision_records_target_without_overwriting_active_unit(store):
    baseline = unit()
    store.graded.install([baseline])
    req = request()
    value = decision(req, unit(body="A different candidate method.",
                               provenance=baseline.metadata["provenance"]))
    with pytest.raises(ValueError, match="create conflicts with existing stable unit ID"):
        apply_decision(store, req, value)
    rows = GradedStore(store).recent_rejected(topic="serving/graph", target_signature=baseline.id)
    assert rows[0]["unit_id"] == baseline.id
    assert rows[0]["gate_reason"] == f"create conflicts with existing stable unit ID {baseline.id}"
    assert store.graded._load().units[baseline.id].content_sha256 == baseline.content_sha256


def test_worker_freezes_history_before_paid_call_and_next_request_consumes_rejection(store, tmp_path, monkeypatch):
    calls = tmp_path / "calls.txt"
    monkeypatch.setenv("MINDIE_GRADED_FIXTURE_CALLS", str(calls))
    engine = Engine(
        store, redactor_executable=scanner(),
        summary_command=[sys.executable, str(Path(__file__).parent / "fixtures/graded_worker.py")],
    )
    selected = inputs()
    selected[0]["content"] = "Change shared runtime contracts for drain timing."
    first = engine.distill_selected(selected, session_digest="b" * 64, task_state="completed",
                                   topic="drain", target_signature="lease-drain")
    assert first["status"] == "local_application_failed"
    assert first["model_execution"] == "completed"
    assert first["model_replayed"] is False
    assert "cross_component_impact_incomplete" in first["local_error"]["message"]
    assert calls.read_text() == "1"
    assert len(GradedStore(store).recent_rejected(topic="drain", target_signature="lease-drain")) == 1
    repeated = engine.distill_selected(selected, session_digest="b" * 64, task_state="completed",
                                      topic="drain", target_signature="lease-drain")
    assert repeated["attempt_id"] == first["attempt_id"]
    assert repeated["model_execution"] == "completed"
    assert repeated["model_replayed"] is False
    assert calls.read_text() == "1"
    next_result = engine.distill_selected(selected, session_digest="c" * 64, task_state="completed",
                                         topic="drain", target_signature="lease-drain")
    assert next_result["attempt_id"] != first["attempt_id"]
    assert calls.read_text() == "2"
    rows = store.db.execute("SELECT value FROM state WHERE key GLOB 'graded:related:*' ORDER BY rowid").fetchall()
    original, later = [json.loads(row[0]) for row in rows]
    assert original["rejected_history"] == []
    assert later["rejected_history"][0]["unit_id"] == "unit-fixture-native"
    assert later["rejected_history"][0]["gate_reason"].startswith("cross_component_impact_incomplete:")


def test_sensitive_scope_keeps_matching_after_redaction_and_distinct_candidates_survive(store):
    scope = {"topic": "graph at 192.168.20.21", "target_signature": "/Users/private-person/probe"}
    req = prepared(store, **scope)
    value = decision(request())
    row = value["units"][0]
    row["frontmatter"]["provenance"]["source_digest"] = req["sanitized_inputs"][0]["source_digest"]
    row["frontmatter"]["shared_contracts"] = ["drain"]
    with pytest.raises(ImpactCompletenessError):
        apply_decision(store, req, value)
    other = deepcopy(value)
    other["units"][0]["markdown_body"] += "A distinct proposed change.\n"
    with pytest.raises(ImpactCompletenessError):
        apply_decision(store, req, other)
    with pytest.raises(ImpactCompletenessError):
        apply_decision(store, req, deepcopy(other))
    next_req = prepared(store, **scope)
    assert len(next_req["rejected_history"]) == 2
    assert "192.168.20.21" not in json.dumps(next_req)
    assert "private-person" not in json.dumps(next_req)


def test_multiunit_rejection_attributes_only_the_incomplete_candidate(store):
    req = request()
    value = decision(req)
    bad = decision(req, unit("unit-incomplete-impact", shared_contracts=["lease-drain"]))["units"][0]
    value["units"].append(bad)
    with pytest.raises(ImpactCompletenessError):
        apply_decision(store, req, value)
    assert GradedStore(store).recent_rejected(
        topic="serving/graph", target_signature="unit-fixture-probe") == []
    assert GradedStore(store).recent_rejected(
        topic="serving/graph", target_signature="unit-incomplete-impact")[0]["unit_id"] == "unit-incomplete-impact"


def test_explicit_topic_history_remains_visible_with_related_target(store):
    from mindie_knowledge.graded.worker import _related_snapshot
    store.graded.install([unit()])
    GradedStore(store).record_candidate(topic="graph", unit_id="unit-fixture-probe",
                                        gate_reason="missing uninstrumented counterexample")
    context = _related_snapshot(
        store, inputs(), session_digest="d" * 64, task_state="completed",
        related_refs=["unit-fixture-probe"], topic="graph")
    assert context["related_units"]
    assert context["rejected_history"][0]["gate_reason"] == "missing uninstrumented counterexample"
    assert context["_history_scope"] == {"topic": "graph"}
