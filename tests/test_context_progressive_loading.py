"""P0 context loading: shared closures, whole-response budgets, visible refs.

These tests exercise observable contracts only: which bodies are charged once,
what remains unresolved, and whether a body can be silently shortened.
"""
import json

import pytest
from graded_fixture import OBSERVED, unit

from mindie_knowledge.graded.index import (
    RoutingIndex,
    response_token_estimate,
    token_estimate,
)
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


@pytest.mark.parametrize("hardware_key", ["soc", "npu_model"])
def test_offline_software_closure_is_independent_of_explicit_hardware_premises(hardware_key):
    observed = {"framework": "fixture-profiler", "trace_device": "fixture:two-rank"}
    software = unit("unit-interval-accounting", tier="L1", category="axiom",
                    evidence_strength="software_contract", domain_path="_common",
                    conditions={"framework": "fixture-profiler"},
                    body="Union intervals in each declared window; do not add overlapping durations.")
    hardware = unit("unit-ub-alignment", tier="L1", category="axiom",
                    evidence_strength="hardware_spec", domain_path="_common",
                    conditions={hardware_key: ["Ascend910B"]},
                    body="The selected chip requires its declared UB alignment in device kernels.")
    offline = unit("unit-host-parser", conditions={"framework": "fixture-profiler"},
                   parents=[software.id], body="Parse per-rank intervals and compare exposed communication.")
    device = unit("unit-device-kernel", conditions={"framework": "fixture-profiler"},
                  parents=[software.id, hardware.id], body="Apply the chip alignment to the device kernel.")
    index = RoutingIndex([software, hardware, offline, device])

    answer = index.explain_closure([offline.id, device.id], conditions=observed, token_budget=16000)
    assert [row["id"] for row in answer["units"]] == [offline.id]
    assert [row["id"] for row in answer["requires"]] == [software.id]
    assert all(row["ready_to_apply"] for row in answer["units"] + answer["requires"])
    assert {row["id"]: row["status"] for row in answer["unresolved"]} == {
        hardware.id: "condition_unknown", device.id: "condition_unknown"}

    wrong = index.explain_closure([device.id], conditions={**observed, hardware_key: "Ascend910C"})
    assert not wrong["units"] and not wrong["requires"]
    assert {row["status"] for row in wrong["unresolved"]} == {"condition_mismatch"}
    supported = index.explain_closure([device.id], conditions={**observed, hardware_key: "Ascend910B"})
    assert [row["id"] for row in supported["units"]] == [device.id]
    assert {row["id"] for row in supported["requires"]} == {software.id, hardware.id}
    assert supported["unresolved"] == []


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


def test_truncated_excerpts_are_never_ready_and_navigation_only_has_no_inline_body():
    big = unit("unit-navigation-long", body="Graph capture step. " * 200)
    index = RoutingIndex([big])
    truncated = index.query("graph capture", conditions=OBSERVED, token_budget=40)["results"][0]
    assert truncated["excerpt_truncated"] is True
    assert truncated["ready_to_apply"] is False

    navigation = index.query("graph capture", conditions=OBSERVED, navigation_only=True,
                             token_budget=16000)
    assert navigation["results"] == []
    assert navigation["body_token_estimate"] == 0
    assert [card["id"] for card in navigation["cards"]] == [big.id]
    assert "excerpt" not in navigation["cards"][0]
    assert big.body not in json.dumps(navigation, ensure_ascii=False)

    tight = index.query("graph capture", conditions=OBSERVED, navigation_only=True,
                        token_budget=20)
    assert tight["cards"] == [] and tight["foundation_cards"] == []
    assert response_token_estimate({"cards": tight["cards"],
                                    "foundation_cards": tight["foundation_cards"]}) <= 20


def test_explain_closure_blocks_a_superseded_ancestor():
    old = unit("unit-old-premise", status="superseded", superseded_by="unit-new-premise",
               body="Superseded graph capture premise.")
    replacement = unit("unit-new-premise", body="Current graph capture premise.")
    child = unit("unit-child-of-old", parents=[old.id], body="Child graph capture step.")
    index = RoutingIndex([old, replacement, child])

    closure = index.explain_closure([child.id], conditions=OBSERVED)
    assert closure["units"] == []
    blocked = closure["unresolved"][0]
    assert blocked["status"] == "removed_or_superseded"
    assert blocked["blocked_by"] == old.id
    assert blocked["superseded_by"] == replacement.id
    assert blocked["current_revision"] == child.revision
    assert blocked["ref"] == child.id


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
    default_partial = index.query("graph capture", conditions=partial)
    assert default_partial["results"] == []
    assert default_partial["cards"] == []
    assert default_partial["excluded_conditions"]["unknown"] == 1
    assert default_partial["unverified_cards"][0]["id"] == rule.id
    assert default_partial["unverified_cards"][0]["match_status"] == "condition_unknown"
    assert default_partial["unverified_cards"][0]["ready_to_apply"] is False
    assert set(default_partial["unverified_cards"][0]["missing_conditions"]) == {
        "cann", "framework", "workload_scope"}
    assert set(default_partial["unverified_cards"][0]["required_conditions"]) == {
        "cann", "framework", "workload_scope"}

    unknown = index.explain_closure([rule.id], conditions=partial)
    assert unknown["units"] == []
    assert unknown["unresolved"][0]["status"] == "condition_unknown"
    assert set(unknown["unresolved"][0]["missing_conditions"]) == {
        "cann", "framework", "workload_scope"}
    assert set(unknown["unresolved"][0]["required_conditions"]) == {
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


def test_single_ref_explain_charges_compat_body_before_admitting_the_closure(store):
    """Existing top-level fields must fit along with the complete closure."""
    premise = unit("unit-merge-premise", body="Merge premise for graph capture. " * 10)
    step = unit("unit-merge-step", parents=[premise.id],
                body="Merge graph capture step. " * 40)
    store.graded.install([premise, step])

    roomy = store.graded.explain(step.id, conditions=OBSERVED, token_budget=16000)
    assert roomy["content"] == step.body
    assert roomy["budget_met"] is True
    assert roomy["usage"]["estimated_tokens"] == response_token_estimate(roomy) <= 16000
    assert "minimum_tokens" not in roomy

    # Metadata and the compatibility view count toward admission, not just
    # the body-only estimate used by the inner closure assembler.
    requested = roomy["usage"]["estimated_tokens"] - 100
    tight = store.graded.explain(step.id, conditions=OBSERVED, token_budget=requested)
    assert tight["units"] == [] and tight["requires"] == []
    assert "content" not in tight
    assert step.body not in json.dumps(tight)
    assert tight["unresolved"][0]["status"] == "budget_excluded"
    assert tight["budget_met"] is True
    assert tight["usage"]["estimated_tokens"] == response_token_estimate(tight) <= requested
    assert tight["usage"]["has_more"] is True


@pytest.mark.parametrize("single_ref", [True, False], ids=["single-ref", "batch"])
def test_explain_without_conditions_obeys_budget_and_keeps_complete_prerequisites(store, single_ref):
    premise = unit("unit-budget-premise", body="Required graph allocation contract. " * 12)
    step = unit("unit-budget-step", parents=[premise.id],
                body="Read the capture state before replacing its graph. " * 80)
    store.graded.install([premise, step])
    ref = store.graded.ref(step.id, step.revision)
    selection = {"ref": ref} if single_ref else {"refs": [ref]}

    narrow = store.explain(**selection, token_budget=400)
    assert narrow["units"] == [] and narrow["requires"] == []
    assert step.body not in json.dumps(narrow)
    assert narrow["unresolved"][0]["status"] == "budget_excluded"
    assert narrow["usage"]["estimated_tokens"] == response_token_estimate(narrow) <= 400

    complete = store.explain(**selection, token_budget=16000)
    assert [item["id"] for item in complete["requires"]] == [premise.id]
    assert complete["units"][0]["content"] == step.body
    assert complete["requires"][0]["content"] == premise.body
    assert complete["units"][0]["ref"] == ref
    assert complete["requires"][0]["ref"] == store.graded.ref(premise.id, premise.revision)
    assert complete["units"][0]["ready_to_apply"] is False
    assert complete["usage"]["estimated_tokens"] == response_token_estimate(complete) <= 16000
    if single_ref:
        assert complete["content"] == step.body
        assert complete["frontmatter"] == step.metadata


def test_single_ref_known_versions_do_not_reload_or_hide_a_revised_premise(store):
    premise = unit("unit-single-visible-parent", revision=2, body="Current allocation contract.")
    step = unit("unit-single-visible-step", parents=[premise.id], body="Capture the current graph.")
    store.graded.install([premise, step])
    ref = store.graded.ref(step.id, step.revision)
    parent_ref = store.graded.ref(premise.id, premise.revision)

    visible = store.explain(ref, known_refs=[ref, parent_ref], token_budget=400)
    assert visible["units"] == [] and visible["requires"] == []
    assert "content" not in visible
    assert visible["unresolved"] == []

    stale_parent = store.explain(ref, known_refs=[ref, store.graded.ref(premise.id, 1)])
    assert stale_parent["units"] == []
    assert stale_parent["requires"][0]["ref"] == parent_ref
    assert stale_parent["requires"][0]["content"] == premise.body
    assert "content" not in stale_parent


@pytest.mark.parametrize("shared_parent", [False, True])
def test_outer_budget_keeps_premises_needed_by_a_visible_requested_body(store, shared_parent):
    premise = unit("unit-retained-premise", revision=2, body="Required allocation contract.")
    visible = unit("unit-retained-visible", parents=[premise.id], body="Previously read procedure.")
    other = unit("unit-retained-other", parents=[premise.id] if shared_parent else [],
                 body="Another requested procedure.")
    store.graded.install([premise, visible, other])
    visible_ref = store.graded.ref(visible.id, visible.revision)
    other_ref = store.graded.ref(other.id, other.revision)
    known = [visible_ref, store.graded.ref(premise.id, 1)]
    selection = {"refs": [visible_ref, other_ref], "known_refs": known, "conditions": OBSERVED}

    roomy = store.explain(**selection, token_budget=16000)
    retained = store.explain(**selection, token_budget=roomy["usage"]["estimated_tokens"] - 32)
    assert retained["units"] == []
    assert [item["ref"] for item in retained["requires"]] == [
        store.graded.ref(premise.id, premise.revision)]
    assert retained["requires"][0]["content"] == premise.body
    assert retained["requires"][0]["ready_to_apply"] is True
    assert [item["id"] for item in retained["unresolved"]] == [other.id]
    assert retained["budget_met"] is True

    premise_only = store.explain(visible_ref, known_refs=known, conditions=OBSERVED,
                                 token_budget=16000)
    excluded = store.explain(
        **selection, token_budget=premise_only["usage"]["estimated_tokens"] - 32)
    assert excluded["units"] == [] and excluded["requires"] == []
    assert any(item["id"] == premise.id and item["status"] == "budget_excluded"
               for item in excluded["unresolved"])
    assert premise.body not in json.dumps(excluded)


def test_store_query_enforces_outer_response_token_budget(store):
    premise, procedure, left, right = shared_workflows()
    extra = [
        unit(f"unit-workflow-extra-{idx}", tier="L3", category="workflow",
             parents=[procedure.id], body=("Extra graph capture workflow step. " * 25))
        for idx in range(3)
    ]
    store.graded.install([premise, procedure, left, right, *extra])

    nav = store.query("graph capture", conditions=OBSERVED, navigation_only=True, token_budget=800)
    assert nav["cards"]
    assert nav["response_token_estimate"] <= 800
    assert response_token_estimate(nav) <= 800
    assert nav["usage"]["has_more"] is True
    assert nav["excluded_budget"]

    full = store.query("graph capture", conditions=OBSERVED, navigation_only=False, token_budget=800)
    assert full["results"]
    assert full["response_token_estimate"] <= 800
    assert response_token_estimate(full) <= 800
    assert full["usage"]["has_more"] is True
    assert full["excluded_budget"]
    assert full["budget_met"] is True
    assert "minimum_tokens" not in full


def test_unsatisfiable_budget_is_reported_not_advertised_as_met(store):
    """Any budget is a legal request, so an unreachable one must say so."""
    premise, procedure, left, right = shared_workflows()
    store.graded.install([premise, procedure, left, right])

    # The protocol envelope alone exceeds this request; nothing is trimmable
    # down to it, and the response must not claim the budget was honoured.
    tiny = store.query("graph capture", conditions=OBSERVED, token_budget=20)
    assert tiny["budget_met"] is False
    assert tiny["usage"]["budget_met"] is False
    assert tiny["minimum_tokens"] == tiny["usage"]["estimated_tokens"] > 20
    assert tiny["usage"]["estimated_tokens"] == response_token_estimate(tiny)
    assert tiny["usage"]["has_more"] is True
    assert tiny["results"] == [] and tiny["cards"] == []
    assert left.body not in json.dumps(tiny, ensure_ascii=False)

    roomy = store.query("graph capture", conditions=OBSERVED, token_budget=16000)
    assert roomy["budget_met"] is True
    assert roomy["response_token_estimate"] == response_token_estimate(roomy) <= 16000


def test_explore_budget_charges_the_mounted_exploration_context(store):
    """`budget_scope` claims the context is charged, so it must really be."""
    premise, procedure, left, right = shared_workflows()
    mutator = unit("unit-explore-mutator", tier="L4", category="mutator",
                   body="Vectorize the graph capture tail.")
    store.graded.install([premise, procedure, left, right, mutator])

    explored = store.query("graph capture", conditions=OBSERVED, mode="explore",
                           token_budget=16000, goal="remove recompiles")
    assert explored["exploration_context"]["subagent_fact_pack"]["stripped_l3_ids"]
    assert "exploration_context" in explored["budget_scope"]
    # The declared estimate covers the mounted fact pack, not just the rows.
    assert explored["usage"]["estimated_tokens"] == response_token_estimate(explored)
    assert explored["budget_met"] is True

    context_tokens = response_token_estimate(explored["exploration_context"])
    assert context_tokens > 0
    squeezed = store.query("graph capture", conditions=OBSERVED, mode="explore",
                           token_budget=max(1, context_tokens // 2), goal="remove recompiles")
    assert squeezed["budget_met"] is False
    assert squeezed["minimum_tokens"] == squeezed["usage"]["estimated_tokens"]
    assert squeezed["exploration_context"]["subagent_fact_pack"]["schema_version"] == (
        "mindie-rsi-subagent-pack/1")


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
