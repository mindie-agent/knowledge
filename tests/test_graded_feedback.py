"""Stable graded revision feedback on the real Store; no model, network or sleeps."""
from __future__ import annotations

import hashlib
import json

import pytest

from mindie_knowledge.graded.feedback import (
    SCHEMA_FEEDBACK,
    merge_votes,
    observe,
    outbound_votes,
    parse_feedback_ref,
    record_vote,
    render_feedback,
    summarize_usage_events,
    summarize_votes,
    validate_feedback,
)
from mindie_knowledge.graded.units import GradedUnit
from mindie_knowledge.loop.store import MAX_VOTE_REASON

DOMAIN = "vllm-ascend"
ROOT = "a" * 64


def make_unit(ident="unit-feedback-probe", *, body="Fixture feedback probe rule.\n",
              revision=None):
    metadata = {
        "schema": "mindie-graded-unit/1",
        "id": ident,
        "tier": "L2",
        "category": "sop",
        "evidence_strength": "single_observation",
        "domain_path": "serving/graph",
        "inherits_from": [],
        "title": "Fixture feedback probe",
        "triggers": {"error_signatures": [], "keywords": ["graph", "feedback"]},
        "conditions": {"soc": ["Ascend910B"], "workload_scope": "decode"},
        "provenance": {
            "source_type": "session_distill",
            "source_locator": "fixture@abcdef1:feedback.txt",
            "source_digest": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "evidence_refs": ["fixture-feedback-observation"],
            "supersedes": [],
        },
    }
    if revision is not None:
        metadata["revision"] = revision
    return GradedUnit.from_parts(metadata, body, f"{metadata['domain_path']}/{ident}.md")


def ref(item, domain=DOMAIN):
    return f"mindie://{domain}/units/{item.id}@{item.revision}"


def public_vote(item, *, root=ROOT, rating="up", reason="Useful in the recorded run.",
                vote_id=None, revision=None):
    revision = item.revision if revision is None else revision
    vote_id = vote_id or hashlib.sha256(
        f"{root}:{item.id}:{revision}".encode("utf-8")
    ).hexdigest()
    return {
        "vote_id": vote_id,
        "root_id": root,
        "unit_id": item.id,
        "revision": revision,
        "rating": rating,
        "reason": reason,
    }


def feedback_text(votes, *, schema=SCHEMA_FEEDBACK, extra=None):
    value = {"schema": schema, "votes": votes}
    value.update(extra or {})
    return json.dumps(value)


def graded_install(store, units, **kwargs):
    if not hasattr(store, "graded"):
        pytest.skip("coordinator graded repository wiring is not present")
    return store.graded.install(units, **kwargs)


def test_observe_is_explicit_idempotent_and_stores_only_identity(store):
    item = make_unit()
    graded_install(store, [item])
    key = f"graded:observed:{item.id}:{item.revision}"

    assert store.db.execute("SELECT 1 FROM state WHERE key=?", (key,)).fetchone() is None
    assert store.db.execute(
        "SELECT 1 FROM known_revisions WHERE entry_id=? AND revision=?",
        (item.id, item.revision),
    ).fetchone() is None

    first = observe(store, item)
    second = observe(store, item)

    assert first == second == {"unit_id": item.id, "revision": item.revision}
    rows = store.db.execute(
        "SELECT key, value FROM state WHERE key LIKE 'graded:observed:%'"
    ).fetchall()
    assert len(rows) == 1 and rows[0]["key"] == key
    assert json.loads(rows[0]["value"]) == first
    assert item.body.strip() not in rows[0]["value"]
    assert store.db.execute(
        "SELECT count(*) FROM known_revisions WHERE entry_id=?", (item.id,)
    ).fetchone()[0] == 1


def test_one_query_writes_one_transaction_and_counts_each_unit_once(store):
    """A delivered unit reaches the caller as a result row and its own card.

    That is one delivery, so its counter advances once per query, and the whole
    receipt batch commits in a single SQLite write transaction.
    """
    from graded_fixture import OBSERVED, unit as graded_unit

    premise = graded_unit("unit-batch-premise", tier="L1", category="axiom",
                          evidence_strength="hardware_spec", domain_path="_common",
                          body="Batch premise for graph capture.")
    child = graded_unit("unit-batch-child", parents=[premise.id],
                        body="Graph capture child step. " * 4)
    store.graded.install([premise, child])

    statements = []
    store.db.set_trace_callback(statements.append)
    try:
        found = store.query("graph capture", conditions=OBSERVED, token_budget=4000)
    finally:
        store.db.set_trace_callback(None)

    # The same unit is delivered both as a result and as its navigation card.
    assert child.id in {row["id"] for row in found["results"]}
    assert child.id in {card["id"] for card in found["cards"]}
    assert sum(1 for text in statements if text.strip().upper().startswith("BEGIN")) == 1

    assert summarize_usage_events(store, child.id, include_delivery=True)["query_deliveries"] == 1
    store.query("graph capture", conditions=OBSERVED, token_budget=4000)
    assert summarize_usage_events(store, child.id, include_delivery=True)["query_deliveries"] == 2

    # An inherited premise is registered as readable without being counted as
    # its own query delivery.
    premise_summary = summarize_usage_events(store, premise.id, include_delivery=True)
    assert premise_summary["query_deliveries"] >= 1
    assert store.db.execute(
        "SELECT count(*) FROM known_revisions WHERE entry_id=?", (premise.id,)
    ).fetchone()[0] == 1


@pytest.mark.parametrize("operation", ["query", "explain"])
def test_explore_context_counts_units_after_result_rows_are_trimmed(store, operation):
    item = make_unit("unit-context-delivery", body="Graph feedback bottleneck evidence.")
    store.graded.install([item])
    conditions = {"soc": "Ascend910B", "workload_scope": "decode"}
    if operation == "query":
        delivered = store.query("graph feedback", conditions=conditions, mode="explore", token_budget=800)
        assert delivered["results"] == [] and delivered["cards"] == []
    else:
        delivered = store.explain(refs=[ref(item)], conditions=conditions, mode="explore", token_budget=800)
        assert delivered["units"] == [] and delivered["requires"] == []
    assert [row["id"] for row in delivered["exploration_context"]["mounted_units"]] == [item.id]
    assert summarize_usage_events(store, item.id, include_delivery=True)[operation + "_deliveries"] == 1
    if operation == "query":
        roomy = store.query("graph feedback", conditions=conditions, mode="explore", token_budget=4000)
        assert roomy["results"]
    else:
        roomy = store.explain(refs=[ref(item)], conditions=conditions, mode="explore", token_budget=4000)
        assert roomy["units"]
    assert roomy["exploration_context"]["mounted_units"]
    assert summarize_usage_events(store, item.id, include_delivery=True)[operation + "_deliveries"] == 2


def test_distinct_revisions_keep_separate_delivery_receipts(store):
    item = make_unit("unit-revision-receipt")
    revised = make_unit("unit-revision-receipt", body="Revised feedback probe rule.\n",
                        revision=2)

    observe(store, item, delivery="query")
    observe(store, revised, delivery="query")

    rows = store.db.execute(
        "SELECT key FROM state WHERE key LIKE 'graded:observed:unit-revision-receipt:%'"
    ).fetchall()
    assert len(rows) == 2
    assert summarize_usage_events(store, item.id, revision=1,
                                  include_delivery=True)["query_deliveries"] == 1
    assert summarize_usage_events(store, revised.id, revision=2,
                                  include_delivery=True)["query_deliveries"] == 1


def test_vote_requires_an_actually_observed_revision(store):
    item = make_unit()

    with pytest.raises(ValueError, match="actually returned"):
        record_vote(store, root_hash=ROOT, ref=ref(item), rating="up",
                    reason="", publishable=False)
    assert store.db.execute("SELECT count(*) FROM votes").fetchone()[0] == 0

    observe(store, item)
    vote = record_vote(store, root_hash=ROOT, ref=ref(item), rating="up",
                       reason="Confirmed after the run.", publishable=True,
                       generation="generation-1")

    assert vote["unit_id"] == item.id
    assert vote["revision"] == item.revision == 1
    assert vote["rating"] == "up" and vote["publishable"] is True
    assert "reason" not in vote

    row = store.db.execute("SELECT * FROM votes").fetchone()
    assert row["entry_id"] == item.id
    assert row["revision"] == "1"
    assert row["root_opaque"] == vote["root_id"]
    assert row["reason"] == "Confirmed after the run."
    assert store.granted(
        "vote", f"{vote['root_id']}:{item.id}", "1", "generation-1"
    )


@pytest.mark.parametrize(
    "candidate",
    [
        "mindie://{d}/units/{i}",                                  # unpinned
        "mindie://{d}/units/{i}@01",                               # noncanonical revision
        "mindie://{d}/tasks/{i}@1",                                # wrong collection
        "mindie://other-domain/units/{i}@1",                       # foreign domain
        "mindie://{d}/units/not-a-unit@1",                         # fake stable id
        "mindie://{d}/units/{i}@0",                                # zero revision
        "mindie://{d}/units/{i}@1/blocks/extra",                   # extra path
        " mindie://{d}/units/{i}@1",                               # noncanonical space
    ],
)
def test_only_a_canonical_domain_reference_is_accepted(store, candidate):
    item = make_unit()
    observe(store, item)
    value = candidate.format(d=DOMAIN, i=item.id)
    with pytest.raises(ValueError):
        parse_feedback_ref(value, DOMAIN)
    with pytest.raises(ValueError):
        record_vote(store, root_hash=ROOT, ref=value, rating="up",
                    reason="", publishable=False)
    assert store.db.execute("SELECT count(*) FROM votes").fetchone()[0] == 0


def test_old_observed_revision_stays_votable_after_current_unit_update(store):
    original = make_unit()
    current = GradedUnit.from_parts(
        {**original.metadata, "revision": 2},
        original.body + "Later correction.\n",
        original.path,
    )
    observe(store, original)
    observe(store, current)
    assert original.content_sha256 != current.content_sha256

    old = record_vote(store, root_hash=ROOT, ref=ref(original), rating="down",
                      reason="The first revision failed.", publishable=True,
                      generation="generation-1")
    new = record_vote(store, root_hash=ROOT, ref=ref(current), rating="up",
                      reason="The corrected revision worked.", publishable=True,
                      generation="generation-1")

    assert old["revision"] == 1
    assert new["revision"] == 2
    rows = store.db.execute(
        "SELECT revision, rating FROM votes WHERE entry_id=? ORDER BY revision",
        (original.id,),
    ).fetchall()
    assert {tuple(row) for row in rows} == {("1", "down"), ("2", "up")}

    summary = summarize_votes([old, new], current)
    assert summary["up"] == 1 and summary["down"] == 0 and summary["total"] == 1
    outbound = outbound_votes(store, [dict(row) for row in store.db.execute("SELECT * FROM votes")])
    assert {vote["revision"] for vote in outbound} == {1, 2}


def test_same_root_and_revision_replaces_up_down_without_multiplying(store):
    item = make_unit()
    observe(store, item)
    up = record_vote(store, root_hash=ROOT, ref=ref(item), rating="up",
                     reason="First impression.", publishable=True,
                     generation="generation-1")
    down = record_vote(store, root_hash=ROOT, ref=ref(item), rating="down",
                       reason="Failed after more testing.", publishable=True,
                       generation="generation-2")

    assert up["vote_id"] == down["vote_id"]
    rows = store.db.execute("SELECT * FROM votes").fetchall()
    assert len(rows) == 1
    assert rows[0]["rating"] == "down"
    assert rows[0]["reason"] == "Failed after more testing."
    assert store.db.execute(
        "SELECT count(*) FROM grants WHERE kind='vote'"
    ).fetchone()[0] == 1
    assert store.granted(
        "vote", f"{down['root_id']}:{item.id}", "1", "generation-2"
    )
    summary = summarize_votes([up, down], item)
    assert summary == {
        "unit_id": item.id,
        "revision": 1,
        "up": 0,
        "down": 1,
        "total": 1,
    }


def test_merge_retains_older_revision_and_deduplicates_by_stable_key():
    original = make_unit()
    current = GradedUnit.from_parts(
        {**original.metadata, "revision": 2},
        original.body + "Corrected wording.\n",
        original.path,
    )
    old = public_vote(original, root="b" * 64, rating="down")
    new = public_vote(current, root="b" * 64, rating="up")

    merged = merge_votes([old], [new])
    assert merged == sorted([old, new], key=lambda vote: (
        vote["root_id"], vote["unit_id"], vote["revision"]
    ))

    replacement = {**new, "rating": "down", "reason": "A later observation."}
    deduped = merge_votes([], [new, replacement])
    assert len(deduped) == 1
    assert deduped[0]["rating"] == "down"
    assert deduped[0]["reason"] == "A later observation."

    with pytest.raises(ValueError, match="unknown"):
        merge_votes([new], [{**new, "content_digest": "sha256:" + "0" * 64}])


def test_validate_and_render_public_feedback_contract():
    item = make_unit()
    vote = public_vote(item)
    text = render_feedback([vote, {**vote, "rating": "down"}])

    checked = validate_feedback(text, "feedback/graded.json")
    assert checked["schema"] == SCHEMA_FEEDBACK
    assert len(checked["votes"]) == 1
    assert checked["votes"][0] == {**vote, "rating": "down"}
    assert set(checked["votes"][0]) == {
        "vote_id", "root_id", "unit_id", "revision", "rating", "reason",
    }
    assert json.loads(text)["schema"] == SCHEMA_FEEDBACK

    with pytest.raises(ValueError, match="not valid JSON"):
        validate_feedback("{", "feedback/broken.json")
    with pytest.raises(ValueError, match="schema"):
        validate_feedback(feedback_text([vote], schema="mindie-feedback/1"), "feedback/wrong.json")
    with pytest.raises(ValueError, match="unknown"):
        validate_feedback(feedback_text([{**vote, "extra": True}]), "feedback/extra.json")
    with pytest.raises(ValueError, match="unknown feedback"):
        validate_feedback(feedback_text([vote], extra={"schema_version": "x"}), "feedback/extra.json")
    with pytest.raises(ValueError, match="positive integer"):
        validate_feedback(feedback_text([{**vote, "revision": True}]), "feedback/revision.json")
    with pytest.raises(ValueError, match="rating"):
        validate_feedback(feedback_text([{**vote, "rating": "sideways"}]), "feedback/rating.json")
    with pytest.raises(ValueError, match="at most"):
        validate_feedback(
            feedback_text([{**vote, "reason": "x" * (MAX_VOTE_REASON + 1)}]),
            "feedback/reason.json",
        )
    with pytest.raises(ValueError, match="duplicate vote_id"):
        validate_feedback(feedback_text([vote, {**vote, "root_id": "c" * 64}]), "feedback/ids.json")
    with pytest.raises(ValueError, match="duplicate vote"):
        validate_feedback(feedback_text([vote, {**vote, "vote_id": "d" * 64}]), "feedback/dupes.json")
    historical = validate_feedback(
        feedback_text([
            vote,
            {**vote, "root_id": "f" * 64, "revision": item.revision + 1,
             "vote_id": "e" * 64},
        ]),
        "feedback/historical.json",
    )
    assert [row["revision"] for row in historical["votes"]] == [1, 2]


def test_private_reason_stays_local_and_publishable_reason_is_scanned(store):
    item = make_unit()
    observe(store, item)
    canary = "10.23.45.67"

    private = record_vote(store, root_hash=ROOT, ref=ref(item), rating="down",
                          reason=f"Private observation from {canary}.",
                          publishable=False)
    assert private["publishable"] is False
    row = store.db.execute("SELECT * FROM votes").fetchone()
    assert row["publishable"] == 0
    assert canary in row["reason"]
    assert store.db.execute("SELECT count(*) FROM grants").fetchone()[0] == 0
    converted = outbound_votes(store, [dict(row)])
    assert converted[0]["rating"] == "down" and converted[0]["reason"] == ""

    with pytest.raises(ValueError, match="ipv4-address") as caught:
        record_vote(store, root_hash=ROOT, ref=ref(item), rating="up",
                    reason=f"Publish this observation from {canary}.",
                    publishable=True, generation="generation-1")
    assert canary not in str(caught.value)
    assert store.db.execute("SELECT count(*) FROM votes").fetchone()[0] == 1
    assert store.db.execute("SELECT count(*) FROM grants").fetchone()[0] == 0
    assert store.db.execute("SELECT rating FROM votes").fetchone()[0] == "down"

    safe = record_vote(store, root_hash=ROOT, ref=ref(item), rating="up",
                       reason="The documented retry recovered the run.",
                       publishable=True, generation="generation-1")
    assert store.granted(
        "vote", f"{safe['root_id']}:{item.id}", "1", "generation-1"
    )


def test_outbound_votes_converts_internal_rows_and_rejects_unobserved_revisions(store):
    item = make_unit()
    observe(store, item)
    vote = record_vote(store, root_hash=ROOT, ref=ref(item), rating="up",
                       reason="Bound to the returned revision.", publishable=True,
                       generation="generation-1")
    internal = dict(store.db.execute("SELECT * FROM votes").fetchone())

    converted = outbound_votes(store, [internal])
    assert converted == [{
        "vote_id": vote["vote_id"],
        "root_id": vote["root_id"],
        "unit_id": item.id,
        "revision": 1,
        "rating": "up",
        "reason": "Bound to the returned revision.",
    }]
    assert set(converted[0]) == {
        "vote_id", "root_id", "unit_id", "revision", "rating", "reason",
    }
    rebound = outbound_votes(store, [vote])[0]
    assert {
        key: rebound[key] for key in (
            "vote_id", "root_id", "unit_id", "revision", "rating"
        )
    } == {
        key: converted[0][key] for key in (
            "vote_id", "root_id", "unit_id", "revision", "rating"
        )
    }

    never_observed = {
        **internal,
        "entry_id": "unit-never-observed",
        "revision": "999",
    }
    with pytest.raises(ValueError, match="actually returned"):
        outbound_votes(store, [never_observed])


def test_summarize_votes_counts_only_current_revision_and_dedupes_root():
    original = make_unit()
    current = GradedUnit.from_parts(
        {**original.metadata, "revision": 2},
        original.body + "Current wording.\n",
        original.path,
    )
    current_up = public_vote(current, root="b" * 64, rating="up")
    duplicate_root = public_vote(current, root="b" * 64, rating="up", vote_id="c" * 64)
    other_root_down = public_vote(current, root="d" * 64, rating="down")
    old_feedback = public_vote(original, root="e" * 64, rating="down")

    summary = summarize_votes(
        [current_up, duplicate_root, other_root_down, old_feedback], current
    )
    assert summary == {
        "unit_id": current.id,
        "revision": 2,
        "up": 1,
        "down": 1,
        "total": 2,
    }

    assert summarize_votes([{**current_up, "revision": 1}], current)["total"] == 0


def test_real_query_and_explain_observe_then_summarize_the_local_vote(store):
    item = make_unit()
    graded_install(store, [item])
    conditions = {"soc": "Ascend910B", "workload_scope": "decode"}

    # Import alone is not observation: the first real query performs it.
    result = store.query("feedback", conditions=conditions)
    found = result["results"][0]
    assert found["id"] == item.id
    assert found["feedback"]["up"] == 0 and found["feedback"]["total"] == 0
    assert store.db.execute(
        "SELECT 1 FROM state WHERE key=?",
        (f"graded:observed:{item.id}:{item.revision}",),
    ).fetchone()

    vote = record_vote(store, root_hash=ROOT, ref=found["feedback_ref"],
                       rating="up", reason="Recovered the recorded scenario.",
                       publishable=True, generation="generation-1")
    assert vote["unit_id"] == found["id"]

    queried = store.query("feedback", conditions=conditions)["results"][0]["feedback"]
    explained = store.explain(found["feedback_ref"])["feedback"]
    assert queried["up"] == explained["up"] == 1
    assert queried["down"] == explained["down"] == 0
    assert "content_digest" not in queried
