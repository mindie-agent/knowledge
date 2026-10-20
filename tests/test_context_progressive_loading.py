"""P0 context loading: shared closures, whole-response budgets, visible refs.

These tests exercise observable contracts only: which bodies are charged once,
what remains unresolved, and whether a body can be silently shortened.
"""
import json

import pytest

from graded_fixture import OBSERVED, unit
from mindie_knowledge.graded.index import RoutingIndex, response_token_estimate, token_estimate
from mindie_knowledge.graded.units import GradedUnit


def shared_workflows():
    premise = unit("unit-shared-contract", tier="L1", category="axiom",
                   evidence_strength="software_contract", domain_path="_common",
                   body="Keep one stable stream and one allocator for the measurement.",
                   triggers={"error_signatures": [], "keywords": ["contract"]})
    procedure = unit("unit-shared-procedure", tier="L2", category="sop", parents=[premise.id],
                     body="Warm the allocator before measuring latency, then record the version.",
                     triggers={"error_signatures": [], "keywords": ["procedure"]})
    left = unit("unit-workflow-left", tier="L3", category="workflow", parents=[procedure.id],
                body="Left graph capture workflow. " * 6)
    right = unit("unit-workflow-right", tier="L3", category="workflow", parents=[procedure.id],
                 body="Right graph capture workflow. " * 6)
    return premise, procedure, left, right


def test_two_l3_units_share_parent_bodies_once_in_query_and_closure():
    premise, procedure, left, right = shared_workflows()
    index = RoutingIndex([premise, procedure, left, right])
    found = index.query("graph capture", conditions=OBSERVED, limit=2, token_budget=16000)
    rows = found["results"]
    assert {row["id"] for row in rows} == {left.id, right.id}
    assert all(row["ready_to_apply"] for row in rows)

    first, second = rows
    first_parents = {parent["id"]: parent for parent in first["inherited_rules"]}
    second_parents = {parent["id"]: parent for parent in second["inherited_rules"]}
    assert set(first_parents) == {premise.id, procedure.id}
    assert set(second_parents) == {premise.id, procedure.id}
    assert first_parents[premise.id]["content"] == premise.body
    assert first_parents[procedure.id]["content"] == procedure.body
    for parent_id in (premise.id, procedure.id):
        assert "content" not in second_parents[parent_id]
        assert second_parents[parent_id]["deduplicated"] is True

    expected = sum(token_estimate(item.body) for item in (premise, procedure, left, right))
    assert found["body_token_estimate"] == expected
    assert found["usage"]["body_token_estimate"] == expected
    assert found["usage"]["estimator"] == "utf8_bytes_div_4_ceiling"
    assert len(found["cards"]) == 2
    assert found["cards"][0]["closure_token_estimate"] >= found["cards"][0]["body_token_estimate"]

    closure = index.explain_closure([left.id, right.id], conditions=OBSERVED, token_budget=16000)
    assert [item["id"] for item in closure["requires"]] == [premise.id, procedure.id]
    assert [item["id"] for item in closure["units"]] == [left.id, right.id]
    assert all(item["content"] == index.units[item["id"]].body
               for item in closure["requires"] + closure["units"])
    assert all(item["ready_to_apply"] for item in closure["requires"] + closure["units"])
    assert closure["usage"]["body_token_estimate"] == expected
    assert closure["unresolved"] == []


def test_narrow_budget_excludes_the_whole_unit_and_never_truncates_steps():
    big = unit("unit-big-workflow", tier="L3", category="workflow",
               body="STEP 1: capture the graph, then verify every queue.\n" * 200)
    index = RoutingIndex([big])
    narrow = index.explain_closure([big.id], conditions=OBSERVED, token_budget=60)
    assert narrow["units"] == []
    excluded = [item for item in narrow["unresolved"] if item["status"] == "budget_excluded"]
    assert len(excluded) == 1
    assert excluded[0]["id"] == big.id
    assert excluded[0]["required_tokens"] > 60
    assert narrow["usage"]["has_more"] is True
    assert "STEP 1" not in json.dumps(narrow, ensure_ascii=False)

    full = index.explain_closure([big.id], conditions=OBSERVED, token_budget=16000)
    assert [item["id"] for item in full["units"]] == [big.id]
    assert full["units"][0]["content"] == big.body
    assert "STEP 1" in full["units"][0]["content"]
    assert full["usage"]["has_more"] is False


def test_missing_facts_and_mismatch_are_discoverable_but_not_ready_to_apply():
    rule = unit("unit-condition-rule", body="Graph capture rule for decode workloads.")
    index = RoutingIndex([rule])
    partial = {"soc": "Ascend910B"}
    advised = index.query("graph capture", conditions=partial, include_unknown=True)
    row = advised["results"][0]
    assert row["match_status"] == "condition_unknown"
    assert row["ready_to_apply"] is False
    assert set(row["missing_conditions"]) == {"cann", "framework", "workload_scope"}
    assert advised["cards"][0]["ready_to_apply"] is False
    assert advised["cards"][0]["missing_conditions"] == row["missing_conditions"]
    assert index.query("graph capture", conditions=partial)["results"] == []
    assert index.query("graph capture", conditions=partial)["excluded_conditions"]["unknown"] == 1

    unknown = index.explain_closure([rule.id], conditions=partial)
    assert unknown["units"] == []
    assert unknown["unresolved"][0]["status"] == "condition_unknown"
    assert set(unknown["unresolved"][0]["missing_conditions"]) == {
        "cann", "framework", "workload_scope"}

    mismatch = index.explain_closure([rule.id], conditions={**OBSERVED, "soc": "Ascend910C"})
    assert mismatch["units"] == []
    assert mismatch["unresolved"][0]["status"] == "condition_mismatch"


def test_known_refs_track_exact_visible_revisions_after_compaction():
    premise = unit("unit-visible-premise", revision=2, body="Visible premise at revision two.")
    step = unit("unit-visible-step", revision=2, parents=[premise.id],
                body="Visible step at revision two.")
    index = RoutingIndex([premise, step])

    full = index.explain_closure([step.id], conditions=None, known_refs=[])
    assert [item["id"] for item in full["requires"]] == [premise.id]
    assert [item["id"] for item in full["units"]] == [step.id]

    visible = [f"{premise.id}@{premise.revision}",
               f"mindie://vllm-ascend/units/{step.id}@{step.revision}"]
    compacted = index.explain_closure([step.id], conditions=None, known_refs=visible)
    assert compacted["requires"] == [] and compacted["units"] == []

    parent_visible = index.explain_closure(
        [step.id], conditions=None, known_refs=[f"{premise.id}@{premise.revision}"])
    assert parent_visible["requires"] == []
    assert [item["id"] for item in parent_visible["units"]] == [step.id]

    # A bare id or the old revision cannot hide the current revision.
    unversioned = index.explain_closure([step.id], conditions=None, known_refs=[step.id])
    assert [item["revision"] for item in unversioned["units"]] == [2]
    outdated = index.explain_closure([step.id], conditions=None, known_refs=[f"{step.id}@1"])
    assert [item["revision"] for item in outdated["units"]] == [2]
    assert [item["id"] for item in outdated["requires"]] == [premise.id]

    # Compaction clears visibility, so the complete closure is read again.
    after_compaction = index.explain_closure([step.id], conditions=None, known_refs=[])
    assert [item["id"] for item in after_compaction["requires"]] == [premise.id]
    assert [item["id"] for item in after_compaction["units"]] == [step.id]


def test_software_contract_l1_and_archive_or_supersede_visibility():
    contract = unit("unit-software-contract", tier="L1", category="axiom",
                    evidence_strength="software_contract", domain_path="software/contract",
                    body="Software contract: CANN 8.1 binds the capture path version.",
                    triggers={"error_signatures": [], "keywords": ["contract"]})
    assert contract.status == "active" and contract.superseded_by is None
    baseline = RoutingIndex([contract]).query(
        "unrelatedtoken", conditions=OBSERVED, domain_path="software/contract")
    assert [row["id"] for row in baseline["results"]] == [contract.id]
    assert baseline["results"][0]["route"] == "foundation_baseline"
    assert [card["id"] for card in baseline["foundation_cards"]] == [contract.id]

    archived = unit("unit-archived-rule", status="archived", body="Archived graph capture rule.")
    replacement = unit("unit-current-rule", body="Current graph capture rule.")
    superseded = unit("unit-superseded-rule", status="superseded",
                      superseded_by=replacement.id, body="Superseded graph capture rule.")
    index = RoutingIndex([archived, replacement, superseded])

    default = index.query("graph capture", conditions=OBSERVED)
    assert [row["id"] for row in default["results"]] == [replacement.id]
    assert default["excluded_conditions"]["archived_or_superseded"] == 2

    included = index.query("graph capture", conditions=OBSERVED, include_archived=True)
    assert {row["id"] for row in included["results"]} == {
        archived.id, replacement.id, superseded.id}

    old = index.explain_closure([superseded.id], conditions=None)
    assert old["units"] == []
    assert old["unresolved"][0]["status"] == "removed_or_superseded"
    assert old["unresolved"][0]["superseded_by"] == replacement.id
    assert old["unresolved"][0]["current_revision"] == superseded.revision

    archived_read = index.explain_closure([archived.id], conditions=None)
    assert [item["id"] for item in archived_read["units"]] == [archived.id]
    assert archived_read["units"][0]["content"] == archived.body

    for bad_status in ("retired", "ACTIVE", None):
        with pytest.raises(ValueError, match="status"):
            unit("unit-bad-status", status=bad_status)
    with pytest.raises(ValueError, match="superseded_by"):
        unit("unit-missing-pointer", status="superseded")
    with pytest.raises(ValueError, match="superseded_by"):
        unit("unit-null-pointer", superseded_by=None)
    with pytest.raises(ValueError, match="superseded_by"):
        unit("unit-self-pointer", status="superseded", superseded_by="unit-self-pointer")


def test_repository_closure_attaches_refs_and_keeps_single_ref_fields(store):
    premise = unit("unit-repository-premise", body="Repository premise for graph capture.")
    step = unit("unit-repository-step", parents=[premise.id],
                body="Repository graph capture step.")
    store.graded.install([premise, step])

    legacy = store.explain(store.graded.ref(step.id, step.revision))
    assert legacy["kind"] == "graded_unit"
    assert legacy["content"] == step.body
    assert legacy["status"] == "active" and legacy["superseded_by"] is None

    closure = store.graded.explain(refs=[step.id], conditions=OBSERVED, token_budget=16000)
    assert [item["id"] for item in closure["requires"]] == [premise.id]
    assert [item["id"] for item in closure["units"]] == [step.id]
    for item in closure["requires"] + closure["units"]:
        assert item["ref"] == store.graded.ref(item["id"], item["revision"])
        assert item["feedback_ref"] == item["ref"]

    compatible = store.graded.explain(step.id, conditions=OBSERVED, token_budget=16000)
    assert compatible["content"] == step.body
    assert compatible["kind"] == "graded_unit"

    compacted = store.graded.explain(
        refs=[step.id], known_refs=[f"{premise.id}@{premise.revision}"], token_budget=16000)
    assert compacted["requires"] == []
    assert [item["id"] for item in compacted["units"]] == [step.id]


def test_response_estimate_uses_canonical_utf8_bytes():
    payload = {"z": "capture", "a": ["规则", 1]}
    from mindie_knowledge.graded.units import canonical
    assert response_token_estimate(payload) == token_estimate(canonical(payload))


def test_omitted_status_keeps_metadata_render_and_digest_stable():
    original = unit("unit-status-stable")
    assert "status" not in original.metadata
    assert original.status == "active" and original.superseded_by is None
    rendered = original.render()
    assert "status:" not in rendered
    reparsed = GradedUnit.from_text(rendered)
    assert reparsed.metadata == original.metadata
    assert reparsed.content_sha256 == original.content_sha256

    archived = unit("unit-status-stable", status="archived")
    assert archived.metadata["status"] == "archived"
    assert archived.content_sha256 != original.content_sha256
    assert "status: archived" in archived.render()
