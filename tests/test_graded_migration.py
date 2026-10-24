"""Mixed query migration and observed-parent feedback with real local stores."""
from contextlib import closing
import hashlib

import pytest

from graded_fixture import unit
from mindie_knowledge.graded.feedback import summarize_usage_events
from mindie_knowledge.graded.index import response_token_estimate, token_estimate
from mindie_knowledge.graded.units import GradedUnit
from mindie_knowledge.materials import MaterialStore
from mindie_knowledge.materials.references import ReadReferenceError
from mindie_knowledge.redact import scan_text


def body_cost(row):
    return token_estimate(row.get("excerpt", "")) + sum(body_cost(item) for item in row.get("related", [])) + sum(
        token_estimate(item["content"]) for item in row.get("inherited_rules", []))


def mixed_corpus(store, tmp_path, *, name="mixed"):
    """One real legacy material feed plus one installed graded unit."""
    task = "a" * 64
    with closing(MaterialStore(tmp_path / name, store.domain)) as author:
        blocks = [dict(block_id=hashlib.sha256(str(index).encode()).hexdigest(),
                       text="Legacy graph probe material. " * 30, source_range={"part": index},
                       title="Legacy graph observation", summary="Legacy graph navigation.")
                  for index in range(5)]
        author.append_batch(task, blocks, "Legacy graph investigation.", title="Legacy graph",
                            status="complete", promote=True)
        store.install_feed([author.export_task(task)], feed_ident=f"{name}-source")
    graded = unit(conditions={"soc": ["Ascend910B"]}, body="Graded graph probe. " * 100)
    store.graded.install([graded])
    return graded


def test_mixed_navigation_trims_legacy_payload_before_claiming_a_floor(store, tmp_path):
    """Legacy excerpts are removable, so they can never form an irreducible floor."""
    mixed_corpus(store, tmp_path)

    nav = store.query("graph", conditions={"soc": "Ascend910B"}, limit=4,
                      token_budget=800, navigation_only=True)

    # A navigation page delivers cards; the inline legacy row yields first.
    assert nav["cards"]
    assert nav["source_results"] == {"graded": 0, "legacy_material": 0}
    assert nav["budget_met"] is True
    assert "minimum_tokens" not in nav
    assert nav["usage"]["estimated_tokens"] == response_token_estimate(nav) <= 800
    assert nav["usage"]["has_more"] is True
    assert nav["excluded_budget"]


def test_roomy_mixed_navigation_keeps_legacy_references_without_inline_bodies(store, tmp_path):
    """Adding a graded unit cannot hide legacy matches from navigation."""
    mixed_corpus(store, tmp_path)

    for budget in (2400, 16000):
        nav = store.query("graph", conditions={"soc": "Ascend910B"}, limit=4,
                          token_budget=budget, navigation_only=True)
        assert nav["cards"], budget
        legacy = [row for row in nav["results"] if row["source"] == "legacy_material"]
        assert legacy, budget
        assert all(not row.get("excerpt") for row in nav["results"])
        assert all(not child.get("excerpt") for row in legacy for child in row["related"])
        assert nav["body_token_estimate"] == 0
        assert nav["budget_met"] is True
        assert nav["usage"]["estimated_tokens"] == response_token_estimate(nav) <= budget
        # Every omitted preview still names readable material, and its cursor
        # continues after exactly those previews rather than skipping them.
        row = legacy[0]
        assert row["excerpt_omitted"] is True
        refs = {row["ref"], *(child["ref"] for child in row["related"])}
        cursor = row["related_next"]
        while cursor is not None:
            continued = store.query(continuation=cursor, limit=4)
            group = continued["results"][0]
            refs.update(child["ref"] for child in group["related"])
            cursor = group["related_next"]
        assert len(refs) == row["related_count"] + 1
        assert all(store.explain(ref)["kind"] == "block" for ref in refs)

    # The same corpus without navigation_only still returns legacy bodies.
    page = store.query("graph", conditions={"soc": "Ascend910B"}, limit=4, token_budget=16000)
    assert any(row["source"] == "legacy_material" and row.get("excerpt")
               for row in page["results"])


def test_mixed_page_sheds_preview_text_but_keeps_every_reference_reachable(store, tmp_path):
    """Preview text is optional; the references it carries are not.

    `page_groups` anchors `related_next` after the previews it already sent, so
    deleting those previews would strand their blocks: paging resumes past them
    and no later page ever returns them.
    """
    mixed_corpus(store, tmp_path)

    page = store.query("graph", conditions={"soc": "Ascend910B"}, limit=4, token_budget=2400)
    legacy = [row for row in page["results"] if row["source"] == "legacy_material"]
    assert legacy, page["source_results"]
    row = legacy[0]

    assert row["related_count"] == 4
    assert row["related"], "the delivered references must survive preview shedding"
    for preview in row["related"]:
        assert preview["excerpt"] == ""
        assert preview["excerpt_omitted"] is True
        assert preview["ref"]
    assert page["usage"]["has_more"] is True
    assert page["usage"]["estimated_tokens"] == response_token_estimate(page) <= 2400

    # Follow the real cursor to exhaustion: the anchor plus every related block
    # must be delivered exactly once across the pages the caller can reach.
    reachable = {row["ref"], *(preview["ref"] for preview in row["related"])}
    cursor, pages = row["related_next"], 0
    while cursor is not None:
        pages += 1
        assert pages <= 8, "related paging must terminate"
        continued = store.query(continuation=cursor, limit=4)
        assert continued["page"] == "related"
        group = continued["results"][0]
        reachable.update(preview["ref"] for preview in group["related"])
        cursor = group.get("related_next")

    assert len(reachable) == row["related_count"] + 1
    for ref in reachable:
        assert store.explain(ref)["kind"] == "block"


def test_graded_delivery_is_counted_only_when_the_unit_survives_composition(store, tmp_path):
    """A mixed page trims after the graded sub-page, so it owns the receipt."""
    graded = mixed_corpus(store, tmp_path)

    def deliveries():
        return summarize_usage_events(store, graded.id,
                                      include_delivery=True)["query_deliveries"]

    # Too tight for any graded card or row: nothing graded reaches the caller.
    before = deliveries()
    dropped = store.query("graph", conditions={"soc": "Ascend910B"}, limit=4, token_budget=400)
    assert [row for row in dropped["results"] if row["source"] == "graded"] == []
    assert dropped["cards"] == [] and dropped["foundation_cards"] == []
    assert deliveries() == before

    # The same page with room for the graded card does count exactly one.
    delivered = store.query("graph", conditions={"soc": "Ascend910B"}, limit=4,
                            token_budget=800, navigation_only=True)
    assert graded.id in {card["id"] for card in delivered["cards"]}
    assert deliveries() == before + 1


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
    result = store.query("graph", conditions={"soc": "Ascend910B"}, limit=4, token_budget=6000, domain_path="unrelated")
    assert result["retrieval"] == "mixed-bm25"
    assert {row["source"] for row in result["results"]} == {"legacy_material", "graded"}
    assert legacy_ref in {row["ref"] for row in result["results"]}
    assert len(result["results"]) <= 4
    # One budget covers both sources: the declared body total counts graded
    # excerpts, premises and nested legacy related excerpts alike, and the
    # declared delivery estimate describes the mixed payload actually returned.
    assert sum(body_cost(row) for row in result["results"]) == result["body_token_estimate"]
    assert result["usage"]["body_token_estimate"] == result["body_token_estimate"]
    assert result["usage"]["estimated_tokens"] == response_token_estimate(result) <= 6000
    assert result["budget_met"] is True

    # The same mixed page under a budget that cannot hold both sources reports
    # the shortfall instead of silently returning an over-budget response.
    tight = store.query("graph", conditions={"soc": "Ascend910B"}, limit=4, token_budget=700)
    assert response_token_estimate(tight) == tight["usage"]["estimated_tokens"]
    assert tight["usage"]["has_more"] is True
    assert tight["budget_met"] is (tight["usage"]["estimated_tokens"] <= 700)
    if not tight["budget_met"]:
        assert tight["minimum_tokens"] == tight["usage"]["estimated_tokens"] > 700
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
    found = store.query("graph", conditions={"soc": "Ascend910B"}, limit=1, token_budget=4000)["results"][0]
    inherited = found["inherited_rules"][0]
    vote = store.record_vote(root_hash="fixture-parent-reader", ref=inherited["feedback_ref"], rating="up",
                             reason="Parent premise applied.", publishable=False)
    assert vote["unit_id"] == parent.id and vote["revision"] == parent.revision
    with pytest.raises(ValueError, match="actually returned"):
        store.record_vote(root_hash="fixture-parent-reader", ref=store.graded.ref(unrelated.id, unrelated.revision),
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


def test_redactor_recognizes_only_complete_graded_revision_refs():
    ref = "mindie://npu/units/unit-rule@7"
    assert not scan_text(f"Read `{ref}`.")
    for malformed in (ref + "/extra", ref + "?token=x", "mindie://npu/units/unit-rule@0", "ssh://" + ref, "operator@private-host"):
        assert any(finding.rule == "username-at-host" for finding in scan_text(malformed))
    old_digest_ref = "mindie://npu/units/unit-rule@" + "a" * 64
    assert any(finding.rule == "username-at-host" for finding in scan_text(old_digest_ref))


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


def test_graded_only_query_skips_legacy_engine_until_visible_material_arrives(store, tmp_path, monkeypatch):
    from mindie_knowledge.materials.provenance import QueryContinuationError
    searches = []
    search = store.materials.search

    def observed_search(*args, **kwargs):
        searches.append(kwargs.get("continuation"))
        return search(*args, **kwargs)

    monkeypatch.setattr(store.materials, "search", observed_search)
    rule = unit(conditions={"soc": ["Ascend910B"]})
    store.graded.install([rule])
    assert store.query("graph", conditions={"soc": "Ascend910B"})["results"][0]["id"] == rule.id
    assert searches == []

    with closing(MaterialStore(tmp_path / "arriving-author", store.domain)) as author:
        blocks = [dict(block_id=hashlib.sha256(str(index).encode()).hexdigest(),
                       text="Legacy graph observation.", title="Legacy graph", summary="Graph material.",
                       source_range={"part": index}) for index in range(5)]
        author.append_batch("a" * 64, blocks, "Legacy graph.", title="Legacy graph", status="complete", promote=True)
        store.install_feed([author.export_task("a" * 64)], feed_ident="arriving-feed")
    mixed = store.query("graph", conditions={"soc": "Ascend910B"}, limit=4, token_budget=6000)
    assert {row["source"] for row in mixed["results"]} == {"graded", "legacy_material"}
    cursor = next(row["related_next"] for row in mixed["results"] if row.get("related_next"))
    assert searches == [None]

    store.install_feed([], feed_ident="arriving-feed")
    assert store.query("graph", conditions={"soc": "Ascend910B"})["results"][0]["id"] == rule.id
    assert searches == [None]
    with pytest.raises(QueryContinuationError):
        store.query(continuation=cursor, limit=1)
    assert searches == [None, cursor]
