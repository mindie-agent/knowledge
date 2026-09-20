"""Repository-side review runner: real Git, dev transport, fake review CLI."""

import json
import sys

import pytest

from mindie_knowledge.community import submit_batch
from mindie_knowledge.community.common import Deadline
from mindie_knowledge.community.review import handle_event, poll_once, review_pull_request

from .conftest import (
    entry_file,
    feedback_file,
    git,
    grok_calls,
    grok_script,
    make_batch,
    make_entry,
    make_remote,
    vote,
)


def _open_pr(settings, state_dir, transport, files, batch_id="rev-1"):
    receipt = submit_batch(make_batch(batch_id, files), settings, state_dir, transport=transport)
    assert receipt["status"] == "submitted", receipt
    return receipt


def test_pure_votes_merge_without_model(settings, state_dir, transport, remote_url):
    doc = make_entry()
    _open_pr(settings, state_dir, transport,
             [feedback_file([vote("v1", doc["entry_id"], doc["revision"]),
                             vote("v2", doc["entry_id"], doc["revision"], rating="down")])])
    result = review_pull_request(settings["repository"], 1, settings, state_dir, transport=transport)
    assert result["status"] == "merged" and result["verdict"] == "accept"
    assert grok_calls(state_dir.parent) == 0  # no model call for structural votes
    pr = transport.get_pull_request(settings["repository"], 1, Deadline(60, 30))
    assert pr["merged"] is True


def test_entry_pr_pending_without_configured_model(settings, state_dir, transport):
    _open_pr(settings, state_dir, transport, [entry_file(make_entry())])
    result = review_pull_request(settings["repository"], 1, settings, state_dir, transport=transport)
    assert result["status"] == "pending"
    pr = transport.get_pull_request(settings["repository"], 1, Deadline(60, 30))
    assert pr["merged"] is False


def test_model_accept_merges_once(settings, state_dir, transport, tmp_path):
    settings["bot"] = {"grok_argv": grok_script(tmp_path, {
        "schema": "mindie-review/1", "verdict": "accept", "reason": "solid"})}
    _open_pr(settings, state_dir, transport, [entry_file(make_entry())])
    result = review_pull_request(settings["repository"], 1, settings, state_dir, transport=transport)
    assert result["status"] == "merged"
    assert grok_calls(tmp_path) == 1
    # The same head is never reviewed twice; no second model call.
    again = review_pull_request(settings["repository"], 1, settings, state_dir, transport=transport)
    assert grok_calls(tmp_path) == 1
    assert again["status"] in ("merged", "skipped")


def test_model_failure_consumes_attempt(settings, state_dir, transport, tmp_path):
    script = tmp_path / "fail_grok.py"
    counter = tmp_path / "fail-calls"
    script.write_text(
        "import pathlib, sys\nsys.stdin.read()\n"
        f"c = pathlib.Path({str(counter)!r})\n"
        "c.write_text(str(int(c.read_text()) + 1) if c.exists() else '1')\n"
        "sys.exit(3)\n",
        encoding="utf-8",
    )
    settings["bot"] = {"grok_argv": [sys.executable, str(script)]}
    _open_pr(settings, state_dir, transport, [entry_file(make_entry())])
    result = review_pull_request(settings["repository"], 1, settings, state_dir, transport=transport)
    assert result["status"] == "failed"
    again = review_pull_request(settings["repository"], 1, settings, state_dir, transport=transport)
    assert "already reviewed" in again["detail"]
    assert int(counter.read_text()) == 1  # same failed head: no retry


def test_model_retire_applies_deterministic_patch(settings, state_dir, transport, tmp_path, remote_url):
    doc = make_entry()
    _open_pr(settings, state_dir, transport, [entry_file(doc)])
    settings["bot"] = {"grok_argv": grok_script(tmp_path, {
        "schema": "mindie-review/1", "verdict": "retire", "reason": "wrong on cann 8.1",
        "retire": {"path": f"cases/{doc['entry_id']}.md", "reason": "wrong on cann 8.1"}})}
    result = review_pull_request(settings["repository"], 1, settings, state_dir, transport=transport)
    assert result["status"] == "merged" and result["verdict"] == "retire"
    # The retired content and reason survive in real Git history on main.
    clone = state_dir / "main-check"
    git(["clone", "--quiet", "-b", "main", remote_url, str(clone)])
    from mindie_knowledge.community import entrydoc

    retired = entrydoc.parse_entry((clone / "cases" / f"{doc['entry_id']}.md").read_text())
    assert retired["status"] == "retired"
    assert retired["retirement_reason"] == "wrong on cann 8.1"
    assert retired["content"] == doc["content"]


def test_disallowed_paths_stay_pending(settings, state_dir, transport, tmp_path, remote_url):
    # Hand-craft a PR branch that smuggles a workflow file past the batch gate.
    work = state_dir / "evil"
    git(["clone", "--quiet", remote_url, str(work)])
    git(["checkout", "-b", "mindie-contrib/npu/evil"], cwd=work)
    (work / ".github").mkdir()
    (work / ".github" / "pwn.yml").write_text("on: push\n", encoding="utf-8")
    git(["add", "-A"], cwd=work)
    git(["-c", "user.name=e", "-c", "user.email=e@example.invalid", "commit", "-m", "x"], cwd=work)
    git(["push", "origin", "HEAD"], cwd=work)
    settings["bot"] = {"grok_argv": grok_script(tmp_path, {"schema": "mindie-review/1",
                                                           "verdict": "accept", "reason": ""})}
    transport.create_pull_request(settings["repository"], title="evil", body="",
                                  head="mindie-contrib/npu/evil", base="main",
                                  deadline=Deadline(60, 30))
    result = review_pull_request(settings["repository"], 1, settings, state_dir, transport=transport)
    assert result["status"] == "pending"
    assert grok_calls(tmp_path) == 0  # deterministic gate fires before any model
    pr = transport.get_pull_request(settings["repository"], 1, Deadline(60, 30))
    assert pr["merged"] is False


def test_head_moved_during_review_stays_pending(settings, state_dir, transport, tmp_path, remote_url):
    doc = make_entry()
    _open_pr(settings, state_dir, transport, [entry_file(doc)])
    # The fake model pushes a new commit to the PR branch as a side effect.
    mover = tmp_path / "mover_grok.py"
    mover.write_text(
        "import json, pathlib, subprocess, sys\n"
        "sys.stdin.read()\n"
        f"work = pathlib.Path({str(state_dir / 'mover')!r})\n"
        f"subprocess.run(['git', 'clone', '--quiet', '-b', 'mindie-contrib/npu/rev-1', {remote_url!r}, str(work)])\n"
        "(work / 'cases' / 'extra.md').write_text('---\\n')\n"
        "subprocess.run(['git', 'add', '-A'], cwd=work)\n"
        "subprocess.run(['git', '-c', 'user.name=r', '-c', 'user.email=r@i.invalid', 'commit', '-m', 'race'], cwd=work)\n"
        "subprocess.run(['git', 'push', 'origin', 'HEAD'], cwd=work)\n"
        "print(json.dumps({'schema': 'mindie-review/1', 'verdict': 'accept', 'reason': ''}))\n",
        encoding="utf-8",
    )
    settings["bot"] = {"grok_argv": [sys.executable, str(mover)]}
    # FileTransport re-reads the branch tip at merge time: the head moved.
    result = review_pull_request(settings["repository"], 1, settings, state_dir, transport=transport)
    assert result["status"] in ("pending", "failed")
    pr = transport.get_pull_request(settings["repository"], 1, Deadline(60, 30))
    assert pr["merged"] is False


def test_incomplete_checks_stay_pending(settings, state_dir, transport, tmp_path):
    doc = make_entry()
    _open_pr(settings, state_dir, transport, [entry_file(doc)])
    settings["bot"] = {"grok_argv": grok_script(tmp_path, {
        "schema": "mindie-review/1", "verdict": "accept", "reason": ""})}
    pr = transport.get_pull_request(settings["repository"], 1, Deadline(60, 30))
    transport.seed(settings["repository"],
                   checks={pr["head"]["sha"]: [{"status": "in_progress", "conclusion": None}]})
    result = review_pull_request(settings["repository"], 1, settings, state_dir, transport=transport)
    assert result["status"] == "pending" and "checks" in result["detail"]


def test_bot_own_pr_and_events_do_not_recurse(settings, state_dir, transport, tmp_path):
    doc = make_entry()
    _open_pr(settings, state_dir, transport, [entry_file(make_entry())])
    settings["bot"] = {"account": "mindie-bot",
                       "grok_argv": grok_script(tmp_path, {"schema": "mindie-review/1",
                                                           "verdict": "accept", "reason": ""})}
    event = {"action": "opened", "number": 1,
             "pull_request": {"number": 1, "head": {"sha": "x", "ref": "mindie-contrib/npu/rev-1"},
                              "user": {"login": "mindie-bot"}},
             "repository": {"full_name": settings["repository"]},
             "sender": {"login": "mindie-bot"}}
    assert handle_event(event, settings, state_dir, transport=transport)["status"] == "ignored"
    assert grok_calls(tmp_path) == 0


def test_event_and_poll_once_paths(settings, state_dir, transport):
    doc = make_entry()
    _open_pr(settings, state_dir, transport,
             [feedback_file([vote("v9", doc["entry_id"], doc["revision"])])], batch_id="rev-9")
    event = {"action": "opened",
             "pull_request": {"number": 1, "head": {"sha": "s", "ref": "mindie-contrib/npu/rev-9"},
                              "user": {"login": "contributor"}},
             "repository": {"full_name": settings["repository"]},
             "sender": {"login": "contributor"}}
    result = handle_event(event, settings, state_dir, transport=transport)
    assert result["status"] == "merged"
    # A second PR + poll-once sweep.
    _open_pr(settings, state_dir, transport,
             [feedback_file([vote("v10", doc["entry_id"], doc["revision"])], path="feedback/fb-2.json")],
             batch_id="rev-10")
    swept = poll_once(settings, state_dir, transport=transport)
    assert any(r.get("pr") == 2 and r.get("status") == "merged" for r in swept["reviewed"])
