"""Core C5/C6/C8 semantics, using only in-memory synthetic protocol values."""
from copy import deepcopy
import re

import pytest

from graded_fixture import CONDITIONS, OBSERVED, decision, request, unit
from mindie_knowledge.graded.conditions import match_conditions
from mindie_knowledge.graded.distill import (
    _sha,
    decision_schema,
    noop_decision,
    validate_decision,
    value_gate,
)
from mindie_knowledge.graded.index import RoutingIndex
from mindie_knowledge.graded.units import GradedUnit


def test_parent_rules_are_filtered_independently_and_unknown_is_not_applicable():
    parent = unit("unit-common-a3", conditions={**CONDITIONS, "soc": ["Ascend910C"]})
    child = unit(parents=[parent.id])
    index = RoutingIndex([parent, child])
    result = index.query("graph", conditions=OBSERVED)["results"]
    assert [row["id"] for row in result] == [child.id]
    assert result[0]["inherited_rules"] == []
    assert result[0]["inheritance_excluded"][0]["status"] == "mismatch"
    unknown = index.query("graph", conditions={"soc": "Ascend910B"})
    assert unknown["results"] == []
    assert unknown["excluded_conditions"]["unknown"] == 1
    advised = index.query("graph", conditions={"soc": "Ascend910B"}, include_unknown=True)
    assert advised["results"][0]["applicability"]["status"] == "unknown"
    assert advised["results"][0]["match_status"] == "condition_unknown"
    assert set(advised["results"][0]["missing_conditions"]) == {"cann", "framework", "workload_scope"}


def test_version_requirements_and_multiple_observed_targets_do_not_expand_rules():
    assert match_conditions(CONDITIONS, OBSERVED).status == "matched"
    assert match_conditions(CONDITIONS, {**OBSERVED, "cann": "9.0"}).status == "mismatch"
    assert match_conditions(CONDITIONS, {**OBSERVED, "framework": {"torch_npu": "2.4"}}).status == "mismatch"
    assert match_conditions(CONDITIONS, {**OBSERVED, "soc": ["Ascend910B", "Ascend910C"]}).status == "mismatch"
    assert match_conditions(CONDITIONS, {**OBSERVED, "cann": "unreported"}).status == "unknown"
    # A recorded private host keeps its opaque identity; its leading angle
    # bracket is not a version comparator and grants no broader host match.
    recorded = '<redacted:local-path:123456789abc>'
    assert match_conditions({'hostname': recorded}, {'hostname': recorded}).status == 'matched'
    assert match_conditions({'hostname': recorded}, {'hostname': '<redacted:local-path:abcdef123456>'}).status == 'mismatch'
    assert match_conditions({'hostname': recorded}, {}).status == 'unknown'


def test_unrecorded_hardware_versions_never_become_applicable_by_echoing_unknown():
    required = {"soc": "Ascend910B4", "cann": "unknown", "driver": "unknown", "dtype": "bfloat16"}
    index = RoutingIndex([unit(conditions=required)])
    assert index.query("graph", conditions=required)["results"] == []
    explained = index.query("graph", conditions=required, include_unknown=True)["results"][0]
    assert explained["applicability"]["status"] == "unknown"
    assert "cann: source applicability is unknown" in explained["applicability"]["reasons"]
    # A target's known software version cannot supply missing historical
    # conditions for the experiment that established this unit.
    assert index.query("graph", conditions={**required, "cann": "8.5", "driver": "25.2.2"})["results"] == []
    assert match_conditions({"soc": "Ascend910B4"}, {"soc": "unknown"}).status == "unknown"
    assert match_conditions({"soc": ["unknown"]}, {"soc": ["unknown"]}).status == "unknown"
    assert match_conditions({"soc": ["Ascend910B4"]}, {"soc": ["Ascend910B4", "unknown"]}).status == "unknown"


def test_inheritance_closure_survives_four_levels_and_deduplicates_diamond():
    a = unit("unit-root")
    b = unit("unit-left", parents=[a.id])
    c = unit("unit-right", parents=[a.id])
    d = unit("unit-junction", parents=[b.id, c.id])
    e = unit("unit-leaf", parents=[d.id])
    index = RoutingIndex([a, b, c, d, e])
    assert index.closure[e.id] == [a.id, b.id, c.id, d.id]
    with pytest.raises(ValueError, match="missing inherited"):
        RoutingIndex([e])
    cyclic = GradedUnit.from_parts({**a.metadata, "inherits_from": [e.id]}, a.body)
    with pytest.raises(ValueError, match="cycle"):
        RoutingIndex([cyclic, b, c, d, e])


def test_path_only_move_preserves_revision_while_conditions_or_body_change_it():
    original = unit()
    moved = GradedUnit.from_parts({**original.metadata, "domain_path": "ops/graph"}, original.body, "ops/graph/moved.md")
    assert moved.id == original.id and moved.content_sha256 == original.content_sha256
    assert moved.revision == original.revision == 1
    changed = GradedUnit.from_parts({**original.metadata, "conditions": {"soc": ["Ascend910C"]}}, original.body)
    assert changed.content_sha256 != original.content_sha256
    assert GradedUnit.from_text(original.render()).content_sha256 == original.content_sha256


def test_frontmatter_cannot_override_claim_conditions_with_duplicate_yaml_keys():
    original = unit()
    parsed = GradedUnit.from_text(original.render())
    assert parsed.metadata == original.metadata and parsed.body == original.body
    for text in (
        original.render().replace("conditions:\n", "conditions: {soc: [Ascend910C]}\nconditions:\n"),
        original.render().replace("conditions:\n", "conditions:\n  soc: [Ascend910C]\n"),
    ):
        with pytest.raises(ValueError, match="frontmatter keys must be unique"):
            GradedUnit.from_text(text)


def test_parent_revision_changes_closure_without_replacing_child_body():
    parent = unit("unit-parent")
    child = unit(parents=[parent.id])
    before = RoutingIndex([parent, child])
    revised = GradedUnit.from_parts({**parent.metadata, "revision": 2}, parent.body + "Corrected parent premise.\n", parent.path)
    after = RoutingIndex([revised, child])
    assert before.units[child.id].content_sha256 == after.units[child.id].content_sha256
    assert before.closure[child.id] == after.closure[child.id] == [parent.id]
    row = next(row for row in after.manifest()["units"] if row["id"] == child.id)
    assert row["revision"] == 1 and "content_sha256" not in row


def test_wrong_route_keeps_cross_directory_recall_and_body_budget():
    parent = unit("unit-common", body="Fixture parent graph capture.")
    child = unit(body="Fixture graph capture instructions. " * 100, parents=[parent.id])
    index = RoutingIndex([parent, child])
    found = index.query("instructions", conditions=OBSERVED, domain_path="irrelevant/subdomain", token_budget=40)
    assert found["results"][0]["id"] == child.id
    assert found["results"][0]["route"] == "cross_directory"
    assert found["results"][0]["excerpt_truncated"]
    assert found["body_token_estimate"] <= 40
    assert found["results"][0]["inheritance_excluded"] == [{"id": parent.id, "status": "budget_excluded"}]


def foundation(ident="unit-foundation-premise", **updates):
    values = dict(tier="L1", category="axiom", evidence_strength="hardware_spec", domain_path="_common",
                  title="Foundation hardware premise", triggers={"error_signatures": [], "keywords": ["baseline"]},
                  body="Foundation premise: Ascend910B decode arithmetic.")
    values.update(updates)
    return unit(ident, **values)


def test_common_l1_foundation_mounts_without_lexical_overlap():
    premise = foundation()
    index = RoutingIndex([premise])
    found = index.query("unrelatedquerytoken", conditions=OBSERVED)["results"]
    assert [row["id"] for row in found] == [premise.id]
    assert found[0]["route"] == "foundation_baseline"
    assert found[0]["score"] == 0.05
    assert found[0]["match_status"] == "matched"
    assert found[0]["applicability"]["status"] == "matched"


def test_common_l1_foundation_is_never_mounted_when_conditions_mismatch():
    premise = foundation()
    index = RoutingIndex([premise])
    mismatched = index.query("unrelatedquerytoken", conditions={**OBSERVED, "soc": "Ascend910C"})
    assert mismatched["results"] == []
    assert mismatched["excluded_conditions"]["mismatch"] == 1
    # No supplied observations means no established premise, not a free baseline.
    assert index.query("unrelatedquerytoken")["results"] == []


def test_requested_domain_l1_foundation_mounts_only_for_that_domain():
    premise = foundation(domain_path="serving/graph")
    index = RoutingIndex([premise])
    found = index.query("unrelatedquerytoken", conditions=OBSERVED, domain_path="serving/graph")["results"]
    assert [row["id"] for row in found] == [premise.id]
    assert found[0]["route"] == "foundation_baseline"
    assert index.query("unrelatedquerytoken", conditions=OBSERVED, domain_path="ops/graph")["results"] == []


def test_matched_child_injects_full_l1_foundation_body_and_revision():
    premise = foundation(revision=3)
    child = unit("unit-child", parents=[premise.id], body="Graph capture instructions for left queue.")
    index = RoutingIndex([premise, child])
    found = index.query("graph capture", conditions=OBSERVED)["results"]
    # The parent is inherited, not mounted a second time as a baseline.
    assert [row["id"] for row in found] == [child.id]
    rule = found[0]["inherited_rules"][0]
    assert rule["id"] == premise.id
    assert rule["revision"] == 3
    assert rule["content"] == premise.body
    assert rule["conditions"] == premise.metadata["conditions"]
    assert rule["tier"] == "L1"
    assert rule["applicability"]["status"] == "matched"


def test_long_workflow_reserves_budget_for_its_foundation_before_excerpt():
    premise = foundation(body="Use fp32 accumulation before casting to the output dtype.")
    child = unit("unit-long-workflow", parents=[premise.id],
                 body="Tune graph capture. " * 100, tier="L3", category="workflow")
    result = RoutingIndex([premise, child]).query(
        "graph", conditions=OBSERVED, limit=1, token_budget=32,
    )
    row = result["results"][0]
    assert row["id"] == child.id
    assert row["inherited_rules"][0]["content"] == premise.body
    assert row["excerpt_truncated"]
    assert result["body_token_estimate"] <= 32


def test_foundation_is_omitted_as_a_whole_when_its_premise_cannot_fit():
    premise = foundation(body="Only apply when dtype is bf16 and the reduction dimension is contiguous.")
    result = RoutingIndex([premise]).query("unrelatedquerytoken", conditions=OBSERVED, token_budget=4)
    assert result["results"] == []
    assert result["excluded_budget"] == [premise.id]


def test_in_flight_observed_only_is_incomplete_but_failures_can_support_scoped_observations():
    req = request(state="in_flight")
    assert value_gate(req) == "in_flight_incomplete"
    assert validate_decision(noop_decision(req), req)["action"] == "noop"
    with pytest.raises(ValueError, match="requires noop"):
        validate_decision(decision(req), req)
    for outcome in ("infra_failed", "compile_failed"):
        req = request(outcome=outcome)
        assert validate_decision(decision(req), req)["action"] == "propose_graded_units"
        proposed = decision(req)
        proposed["units"][0]["evidence_strength"] = "reproduced_empirical"
        proposed["units"][0]["frontmatter"]["evidence_strength"] = "reproduced_empirical"
        with pytest.raises(ValueError, match="repeated verified"):
            validate_decision(proposed, req)


def test_in_flight_short_verified_referenced_milestone_is_eligible():
    # Regression: this short referenced finding was rejected unless unrelated
    # pending progress was appended to the request.
    req = request(state="in_flight", outcome="verified")
    item = req["sanitized_inputs"][0]
    assert len(item["content"].encode()) < 120 and item["evidence_refs"]
    assert value_gate(req) is None
    assert validate_decision(decision(req), req)["action"] == "propose_graded_units"


def test_model_cannot_invent_source_locator_digest_or_evidence_refs():
    req = request()
    assert validate_decision(decision(req), req)["action"] == "propose_graded_units"
    for field, value in (("source_locator", "fixture@abcdef1:unread.txt"), ("source_digest", "f" * 64), ("evidence_refs", ["unobserved-run"])):
        proposed = decision(req)
        proposed["units"][0]["frontmatter"]["provenance"][field] = value
        with pytest.raises(ValueError, match="selected evidence|not present"):
            validate_decision(proposed, req)


def test_native_c6_category_prefixed_id_is_rejected_by_prompt_schema_and_admission():
    # The first real DSH canary returned this ID despite the prose-only rule.
    # Both model-facing ID fields must express the same admission constraint.
    bad_id = "sop-synthetic-c6-cache-index-readiness-001"
    req = request()
    proposed = decision(req)
    row = proposed["units"][0]
    schema = decision_schema()["properties"]["units"]["items"]["properties"]
    for field in (schema["target_unit_id"], schema["frontmatter"]["properties"]["id"]):
        assert re.fullmatch(field["pattern"], row["target_unit_id"])
        assert not re.fullmatch(field["pattern"], bad_id)
    row["target_unit_id"] = row["frontmatter"]["id"] = bad_id
    with pytest.raises(ValueError, match="stable id must use unit-"):
        validate_decision(proposed, req)
    row["target_unit_id"] = row["frontmatter"]["id"] = "unit-synthetic-c6-cache-index-readiness-001"
    assert validate_decision(proposed, req)["action"] == "propose_graded_units"


def test_reproduced_evidence_requires_verified_repeats_and_l1_spec_source():
    req = request(outcome="verified", repetitions=3)
    proposed = decision(req)
    proposed["units"][0]["evidence_strength"] = "reproduced_empirical"
    proposed["units"][0]["frontmatter"]["evidence_strength"] = "reproduced_empirical"
    assert validate_decision(proposed, req)["units"][0]["evidence_strength"] == "reproduced_empirical"
    spec = deepcopy(proposed)
    row = spec["units"][0]
    row.update(target_tier="L1", category="axiom", evidence_strength="hardware_spec")
    row["frontmatter"].update(tier="L1", category="axiom", evidence_strength="hardware_spec")
    with pytest.raises(ValueError, match="hardware specification"):
        validate_decision(spec, req)


def test_model_schema_rejects_native_invalid_tier_uses_without_weakening_admission():
    from jsonschema import Draft202012Validator, ValidationError
    validator = Draft202012Validator(decision_schema())
    req = request(outcome="verified", repetitions=3)
    for tier, category in (("L1", "measurement"), ("L2", "sop"), ("L3", "workflow")):
        proposed = decision(req, unit(tier=tier, category=category, evidence_strength="reproduced_empirical"))
        validator.validate(proposed)
        validate_decision(proposed, req)
    # These two pairings were returned by the real DSH G3 organizer. The
    # model-facing schema must reject them just as the C5 parser already does.
    for tier, category in (("L3", "mutator"), ("L2", "measurement")):
        proposed = decision(req)
        proposed["units"][0].update(target_tier=tier, category=category)
        proposed["units"][0]["frontmatter"].update(tier=tier, category=category)
        with pytest.raises(ValidationError):
            validator.validate(proposed)
        with pytest.raises(ValueError, match="tier/category"):
            validate_decision(proposed, req)
    proposed = decision(req)
    proposed["units"][0]["frontmatter"].update(tier="L1", category="measurement")
    with pytest.raises(ValidationError):
        validator.validate(proposed)


def test_verified_milestone_can_use_finished_evidence_but_not_an_inflight_sibling():
    req = request()
    pending = {**req["sanitized_inputs"][0], "outcome": "in_flight", "source_locator": "fixture@abcdef1:pending.txt",
               "source_digest": "b" * 64, "evidence_refs": ["pending-run"]}
    req["sanitized_inputs"].append(pending)
    req["request_digest"] = _sha({key: value for key, value in req.items() if key != "request_digest"})
    assert validate_decision(decision(req), req)["action"] == "propose_graded_units"
    proposed = decision(req)
    proposed["units"][0]["frontmatter"]["provenance"].update(
        source_locator=pending["source_locator"], source_digest=pending["source_digest"], evidence_refs=pending["evidence_refs"])
    with pytest.raises(ValueError, match="selected evidence"):
        validate_decision(proposed, req)


def test_exact_workload_group_rejects_subset_duplicate_and_crossed_case_identity():
    cases = [dict(input_shape='1x37x4x128', workload_case_sha256='a' * 64, samples_per_shape=36),
             dict(input_shape='2x131x4x128', workload_case_sha256='b' * 64, samples_per_shape=36)]
    value = unit(tier='L4', category='mutator', conditions={
        'soc': ['Ascend910B', 'Ascend910C'], 'workload': {'exact_set': cases}})
    index = RoutingIndex([value])
    observed = {'soc': 'Ascend910B', 'workload': cases}
    assert index.query('graph', conditions=observed)['results']
    assert index.query('graph', conditions={**observed, 'workload': list(reversed(cases))})['results']
    for members in (cases[:1], [cases[0], cases[0]], [
            {**cases[0], 'workload_case_sha256': cases[1]['workload_case_sha256']},
            {**cases[1], 'workload_case_sha256': cases[0]['workload_case_sha256']}],
            [{**cases[0], 'samples_per_shape': 1}, cases[1]]):
        assert index.query('graph', conditions={**observed, 'workload': members})['results'] == []
    assert match_conditions(value.metadata['conditions'], {
        **observed, 'workload': [{**cases[0], 'workload_case_sha256': 'unknown'}, cases[1]]}).status == 'unknown'


def test_source_digest_is_optional_when_locator_binds_the_evidence():
    original = unit()
    provenance = {key: value for key, value in original.metadata["provenance"].items()
                  if key != "source_digest"}
    value = GradedUnit.from_parts({**original.metadata, "provenance": provenance},
                                  original.body, original.path)
    assert "source_digest" not in value.metadata["provenance"]
    assert value.content_sha256 and value.revision == 1
