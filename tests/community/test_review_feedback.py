"""Feedback-only retirement with real Git, and own-patch CI continuation."""

import json
import sys

from mindie_knowledge.community import entrydoc, submit_batch
from mindie_knowledge.community.common import Deadline
from mindie_knowledge.community.review import review_pull_request

from .conftest import (
    entry_file,
    feedback_file,
    git,
    grok_calls,
    grok_script,
    make_batch,
    make_entry,
    vote,
)


def _seed_entry_on_main(settings, remote_url, state_dir, doc):
    seed = state_dir / "seed-entry"
    git(["clone", "--quiet", remote_url, str(seed)])
    path = seed / "cases" / f"{doc['entry_id']}.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(entry_file(doc)["content"], encoding="utf-8")
    git(["add", "cases"], cwd=seed)
    git(["-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-m", "known entry"], cwd=seed)
    git(["push", "origin", "main"], cwd=seed)


def test_feedback_only_downvote_retires_referenced_entry(settings, state_dir, transport, tmp_path, remote_url):
    doc = make_entry()
    _seed_entry_on_main(settings, remote_url, state_dir, doc)
    down = vote("v-down", doc["entry_id"], doc["revision"], rating="down",
                reason="wrong on cann 8.1, container numbering changed")
    receipt = submit_batch(make_batch("fb-retire", [feedback_file([down])]),
                           settings, state_dir, transport=transport)
    assert receipt["status"] == "submitted", receipt
    settings["bot"] = {"grok_argv": grok_script(tmp_path, {
        "schema": "mindie-review/1", "verdict": "retire", "reason": "confirmed",
        "retire": {"path": f"cases/{doc['entry_id']}.md", "reason": "wrong on cann 8.1"}})}
    result = review_pull_request(settings["repository"], 1, settings, state_dir, transport=transport)
    assert result["status"] == "merged" and result["verdict"] == "retire", result
    assert grok_calls(tmp_path) == 1
    clone = state_dir / "main-after"
    git(["clone", "--quiet", "-b", "main", remote_url, str(clone)])
    retired = entrydoc.parse_entry((clone / "cases" / f"{doc['entry_id']}.md").read_text())
    assert retired["status"] == "retired"
    assert retired["retirement_reason"] == "wrong on cann 8.1"
    assert retired["content"] == doc["content"]  # history/content preserved


def test_unknown_feedback_reference_stays_pending(settings, state_dir, transport, tmp_path):
    doc = make_entry()  # never published to main
    down = vote("v-x", doc["entry_id"], doc["revision"], rating="down", reason="broken")
    submit_batch(make_batch("fb-unknown", [feedback_file([down])]), settings, state_dir,
                 transport=transport)
    settings["bot"] = {"grok_argv": grok_script(tmp_path, {
        "schema": "mindie-review/1", "verdict": "accept", "reason": ""})}
    result = review_pull_request(settings["repository"], 1, settings, state_dir, transport=transport)
    assert result["status"] == "pending" and "unknown" in result["detail"]
    assert grok_calls(tmp_path) == 0  # no model call on unresolvable references


def test_own_patch_ci_pending_continuation_no_second_model(settings, state_dir, transport, tmp_path, remote_url):
    doc = make_entry()
    _seed_entry_on_main(settings, remote_url, state_dir, doc)
    down = vote("v-down", doc["entry_id"], doc["revision"], rating="down", reason="obsolete")
    submit_batch(make_batch("fb-ci", [feedback_file([down])]), settings, state_dir,
                 transport=transport)
    settings["bot"] = {"grok_argv": grok_script(tmp_path, {
        "schema": "mindie-review/1", "verdict": "retire", "reason": "obsolete",
        "retire": {"path": f"cases/{doc['entry_id']}.md", "reason": "obsolete"}})}
    repo = settings["repository"]
    pr = transport.get_pull_request(repo, 1, Deadline(60, 30))
    head = pr["head"]["sha"]
    # CI runs on every push: the patch head lands with checks still pending.
    transport.seed(repo, checks={head: [{"status": "completed", "conclusion": "success"}]},
                   checks_default=[{"status": "in_progress", "conclusion": None}])
    first = review_pull_request(repo, 1, settings, state_dir, transport=transport)
    assert first["status"] == "pending", first  # patch pushed, but patch-head CI unknown
    assert grok_calls(tmp_path) == 1
    # The patch head arrives as a synchronize event while its CI is pending.
    patched = transport.get_pull_request(repo, 1, Deadline(60, 30))["head"]["sha"]
    assert patched != head
    second = review_pull_request(repo, 1, settings, state_dir, transport=transport)
    assert second["status"] == "pending"
    assert grok_calls(tmp_path) == 1  # no second model call for our own patch head
    # CI finishes green on the patch head: deterministic continuation merges.
    transport.seed(repo, checks={patched: [{"status": "completed", "conclusion": "success"}]})
    third = review_pull_request(repo, 1, settings, state_dir, transport=transport)
    assert third["status"] == "merged", third
    assert grok_calls(tmp_path) == 1
