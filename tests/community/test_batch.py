"""Batch schema, path allowlist, hash checks, vote merge and templates."""

import json

import pytest

from mindie_knowledge.community import entrydoc
from mindie_knowledge.community.batch import (
    check_path,
    merge_votes,
    render_commit_message,
    render_pr_body,
    render_pr_title,
    validate_batch,
    validate_feedback,
)
from mindie_knowledge.community.common import CommunityError, sha256_text

from .conftest import entry_file, feedback_file, make_batch, make_entry, vote


def test_valid_entry_batch_passes():
    batch = make_batch("b1", [entry_file(make_entry())])
    checked = validate_batch(batch)
    assert checked["batch_id"] == "b1"
    assert checked["votes_only"] is False
    assert checked["files"][0]["path"].startswith("cases/")


def test_votes_only_batch_is_valid_without_entries():
    doc = make_entry()
    batch = make_batch("votes-1", [feedback_file([vote("v1", doc["entry_id"], doc["revision"])])])
    checked = validate_batch(batch)
    assert checked["votes_only"] is True


@pytest.mark.parametrize(
    "path",
    [
        "../escape.md",
        "cases/../secret.md",
        "/abs/cases/x.md",
        ".github/workflows/x.yml",
        "cases/.hidden.md",
        "cases/nested/x.md",
        "cases/x.py",
        "cases/x.sh",
        "skills/x/SKILL.md",
        "README.md",
        "feedback/x.md",
        "cases//x.md",
        "cases/x.md/",
    ],
)
def test_disallowed_paths_rejected(path):
    with pytest.raises(CommunityError):
        check_path(path)


@pytest.mark.parametrize("path", ["cases/x.md", "topics/topic-a.md", "feedback/fb.json"])
def test_allowed_paths(path):
    assert check_path(path) == path


def test_sha256_mismatch_rejected():
    item = entry_file(make_entry())
    item["sha256"] = "0" * 64
    batch = make_batch("b2", [item])
    with pytest.raises(CommunityError, match="sha256"):
        validate_batch(batch)


def test_revision_mismatch_rejected():
    batch = make_batch("b3", [entry_file(make_entry())])
    batch["revision"] = "f" * 64
    with pytest.raises(CommunityError, match="revision"):
        validate_batch(batch)


def test_wrong_schema_rejected():
    batch = make_batch("b4", [entry_file(make_entry())], schema="mindie-contribution/0")
    with pytest.raises(CommunityError, match="schema"):
        validate_batch(batch)


def test_duplicate_paths_rejected():
    item = entry_file(make_entry())
    batch = make_batch("b5", [item, dict(item)])
    with pytest.raises(CommunityError, match="duplicate"):
        validate_batch(batch)


def test_vote_validation_bounds():
    doc = make_entry()
    base = vote("v1", doc["entry_id"], doc["revision"])
    bad_rating = dict(base, rating="meh")
    with pytest.raises(CommunityError, match="rating"):
        validate_feedback(json.dumps({"schema": "mindie-feedback/1", "votes": [bad_rating]}), "feedback/x.json")
    long_reason = dict(base, reason="x" * 1001)
    with pytest.raises(CommunityError, match="reason"):
        validate_feedback(json.dumps({"schema": "mindie-feedback/1", "votes": [long_reason]}), "feedback/x.json")
    dup = [base, dict(base)]
    with pytest.raises(CommunityError, match="duplicate"):
        validate_feedback(json.dumps({"schema": "mindie-feedback/1", "votes": dup}), "feedback/x.json")


def test_vote_merge_updates_same_id_not_double_count():
    doc = make_entry()
    existing = [vote("v1", doc["entry_id"], doc["revision"], rating="down", reason="old")]
    new = [
        vote("v1", doc["entry_id"], doc["revision"], rating="up", reason=""),
        vote("v2", doc["entry_id"], doc["revision"]),
    ]
    merged = merge_votes(existing, new)
    assert [v["vote_id"] for v in merged] == ["v1", "v2"]
    assert merged[0]["rating"] == "up"  # updated in place, not appended


def test_templates_are_deterministic():
    batch = validate_batch(make_batch("b6", [entry_file(make_entry())], summary="hello"))
    assert render_pr_title(batch) == render_pr_title(batch)
    assert render_pr_body(batch) == render_pr_body(batch)
    assert render_commit_message(batch) == render_commit_message(batch)
    body = render_pr_body(batch)
    assert "b6" in body and batch["revision"] in body and batch["files"][0]["path"] in body
    assert "hello" in render_pr_title(batch)


def test_feedback_render_is_canonical():
    doc = make_entry()
    from mindie_knowledge.community.batch import render_feedback

    votes = [vote("v2", doc["entry_id"], doc["revision"]), vote("v1", doc["entry_id"], doc["revision"])]
    text = render_feedback(votes)
    assert text.index("v1") < text.index("v2")
    parsed = validate_feedback(text, "feedback/x.json")
    assert [v["vote_id"] for v in parsed["votes"]] == ["v1", "v2"]


def test_entrydoc_roundtrip_and_revision_stability():
    doc = make_entry(content="Line one.\nLine two.\n")
    text = entrydoc.render_entry(doc)
    parsed = entrydoc.parse_entry(text)
    assert parsed["entry_id"] == doc["entry_id"]
    assert parsed["revision"] == doc["revision"]
    assert parsed["status"] == "active"
    retired = dict(doc, status="retired", retirement_reason="superseded")
    retired["revision"] = entrydoc.revision_of({**retired, "revision": None})
    parsed_retired = entrydoc.parse_entry(entrydoc.render_entry(retired))
    assert parsed_retired["status"] == "retired"
    assert parsed_retired["retirement_reason"] == "superseded"


def test_entrydoc_rejects_retired_without_reason():
    doc = make_entry(status="retired", retirement_reason="")
    doc["revision"] = entrydoc.revision_of({**doc, "revision": None})
    with pytest.raises(ValueError, match="reason"):
        entrydoc.parse_entry(entrydoc.render_entry(doc))


def test_entrydoc_rejects_tampered_revision():
    doc = make_entry()
    text = entrydoc.render_entry(doc).replace(doc["summary"], "tampered summary")
    with pytest.raises(ValueError, match="revision"):
        entrydoc.parse_entry(text)
