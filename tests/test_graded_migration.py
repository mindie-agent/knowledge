"""Mixed query migration and observed-parent feedback with real local stores."""
from contextlib import closing
import hashlib

import pytest

from graded_fixture import unit
from mindie_knowledge.graded.index import token_estimate
from mindie_knowledge.graded.units import GradedUnit
from mindie_knowledge.materials import MaterialStore
from mindie_knowledge.materials.references import ReadReferenceError
from mindie_knowledge.redact import scan_text


def body_cost(row):
    return token_estimate(row.get("excerpt", "")) + sum(body_cost(item) for item in row.get("related", [])) + sum(
        token_estimate(item["content"]) for item in row.get("inherited_rules", []))


def test_local_c5_preserves_legacy_results_reference_reads_and_one_budget(store, tmp_path):
    task = "a" * 64
    with closing(MaterialStore(tmp_path / "author", store.domain)) as author:
        blocks = [dict(block_id=hashlib.sha256(str(index).encode()).hexdigest(),
                       text="Legacy graph probe material. " * 30, source_range={"part": index},
                       title="Legacy graph observation", summary="Legacy graph navigation.") for index in range(5)]
        author.append_batch(task, blocks, "Legacy graph investigation.", title="Legacy graph", status="complete", promote=True)
        store.install_feed([author.export_task(task)], feed_ident="legacy-source")
    before = store.query("graph", limit=4)
    legacy_ref = before["results"][0]["ref"]
    store.graded.install([unit(conditions={"soc": ["Ascend910B"]}, body="Graded graph probe. " * 100)])
    result = store.query("graph", conditions={"soc": "Ascend910B"}, limit=4, token_budget=60, domain_path="unrelated")
    assert result["retrieval"] == "mixed-bm25"
    assert {row["source"] for row in result["results"]} == {"legacy_material", "graded"}
    assert legacy_ref in {row["ref"] for row in result["results"]}
    assert len(result["results"]) <= 4
    assert sum(body_cost(row) for row in result["results"]) == result["body_token_estimate"] <= 60
    assert store.explain(legacy_ref)["kind"] == "block"
    cursors = [row["related_next"] for row in result["results"] if row.get("related_next")]
    assert cursors
    continued = store.query(continuation=cursors[0], limit=1)
    assert continued["retrieval"] == "reme-bm25" and continued["page"] == "related"
    assert store.explain(continued["results"][0]["related"][0]["ref"])["kind"] == "block"
    assert "graded ranking preference" in result["domain_path_scope"]


def test_only_returned_inherited_parent_is_observed_for_feedback(store):
    parent = unit("unit-rule-parent", conditions={"soc": ["Ascend910B"]}, body="A prerequisite rule.",
                  title="Prerequisite", triggers={"keywords": ["parent"], "error_signatures": []})
    child = unit("unit-graph-child", conditions={"soc": ["Ascend910B"]}, parents=[parent.id], body="Graph child rule.")
    unrelated = unit("unit-unseen", conditions={"soc": ["Ascend910C"]})
    store.graded.install([parent, child, unrelated])
    found = store.query("graph", conditions={"soc": "Ascend910B"}, limit=1, token_budget=100)["results"][0]
    inherited = found["inherited_rules"][0]
    vote = store.record_vote(root_hash="fixture-parent-reader", ref=inherited["feedback_ref"], rating="up",
                             reason="Parent premise applied.", publishable=False)
    assert vote["unit_id"] == parent.id and vote["revision"] == parent.revision
    with pytest.raises(ValueError, match="actually returned"):
        store.record_vote(root_hash="fixture-parent-reader", ref=store.graded.ref(unrelated.id, unrelated.content_sha256),
                          rating="up", reason="Not observed.", publishable=False)


def test_public_parent_withdrawal_retires_dependent_local_candidate_without_resurrection(store):
    parent = unit("unit-public-parent")
    child = unit("unit-local-child", parents=[parent.id])
    store.graded.install_feed([parent], feed_ident="graded-source", source_revision="a" * 40)
    store.graded.install([parent, child], generation="g1")
    observed = store.explain(store.graded.ref(child.id))
    store.graded.install_feed([], feed_ident="graded-source", source_revision="b" * 40)
    assert store.graded.pending("g1") == []
    with pytest.raises(ReadReferenceError, match="absent"):
        store.explain(observed["ref"])
    assert store.is_withdrawn(observed["ref"])
    assert store.graded._catalog()["retired_local"][0]["id"] == child.id


def test_redactor_recognizes_only_complete_pinned_graded_refs():
    ref = "mindie://npu/units/unit-rule@" + "a" * 64
    assert not scan_text(f"Read `{ref}`.")
    for malformed in (ref + "/extra", ref + "?token=x", ref[:-2], "ssh://" + ref, "operator@private-host"):
        assert any(finding.rule == "username-at-host" for finding in scan_text(malformed))


@pytest.mark.parametrize("old_generation", [None, "g0"])
def test_c6_grant_applies_only_to_admitted_units_and_preserves_unrelated_candidates(store, old_generation):
    from graded_fixture import request, decision
    from mindie_knowledge.graded.distill import apply_decision
    private = unit("unit-private-import")
    store.graded.install([private], generation=old_generation)
    req = request()
    apply_decision(store, req, decision(req), generation="g1")
    assert {item["id"] for item in store.graded.pending("g1")} == {"unit-fixture-probe"}
    retained = next(item for item in store.graded._catalog()["local"] if item["id"] == private.id)
    assert retained.get("generation") == old_generation


def test_c6_does_not_replace_an_unrelated_unpublished_correction_with_public_bytes(store):
    from graded_fixture import request, decision
    from mindie_knowledge.graded.distill import apply_decision
    public = unit("unit-unrelated-public")
    store.graded.install_feed([public], feed_ident="graded-source", source_revision="a" * 40)
    correction = GradedUnit.from_parts(public.metadata, public.body + "Pending reviewed correction.\n", public.path)
    store.graded.install([correction], generation="g0")
    retained = store.graded.pending("g0")[0]
    req = request()
    apply_decision(store, req, decision(req), generation="g1")
    assert store.graded.pending("g0") == [retained]
    assert {item["id"] for item in store.graded.pending("g1")} == {"unit-fixture-probe"}
    assert store.graded._load().units[public.id].content_sha256 == public.content_sha256



def test_only_verified_same_feed_format_switch_retires_legacy_membership_without_deletion(store, tmp_path):
    task = "b" * 64
    with closing(MaterialStore(tmp_path / "old-author", store.domain)) as author:
        author.append_batch(task, [dict(block_id="c" * 64, text="Legacy graph record.", source_range={"part": 0},
                                       title="Legacy graph", summary="Legacy graph record.")],
                            "Legacy graph record.", title="Legacy graph record", status="complete", promote=True)
        store.install_feed([author.export_task(task)], feed_ident="same-feed", source_revision="a" * 40)
    previous = store.query("graph")["results"][0]
    old_files = list((store.root / "materials").rglob("*.md"))
    assert old_files
    store.graded.install_feed([unit(conditions={"soc": ["Ascend910B"]})], feed_ident="same-feed", source_revision="b" * 40)
    current = store.query("graph", conditions={"soc": "Ascend910B"})
    assert all(row.get("source") == "graded" for row in current["results"])
    assert store.db.execute("SELECT feed_active FROM entries WHERE entry_id=?", (task,)).fetchone()[0] == 0
    assert store.feed_get("graded-format-switch:same-feed")["commit"] == "b" * 40
    assert all(path.exists() for path in old_files)
    with pytest.raises(ReadReferenceError):
        store.explain(previous["ref"])
