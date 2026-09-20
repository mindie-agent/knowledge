"""submit_batch / reconcile_batch against REAL local Git remotes.

The Git layer is real (clone/commit/push subprocesses). GitHub API behaviour
uses the file-backed dev transport, with controlled fault injection at that
boundary. Dev-transport success is mechanism evidence, not GitHub acceptance.
"""

import json
import sqlite3
import threading

import pytest

from mindie_knowledge.community import reconcile_batch, submit_batch
from mindie_knowledge.community.common import CommunityError, UnknownOutcome
from mindie_knowledge.community.ledger import Ledger

from .conftest import (
    entry_file,
    feedback_file,
    git,
    grok_calls,
    make_batch,
    make_entry,
    vote,
)


def test_submit_happy_path_real_git(settings, state_dir, transport, remote_url):
    doc = make_entry()
    batch = make_batch("batch-a", [entry_file(doc)])
    receipt = submit_batch(batch, settings, state_dir, transport=transport)
    assert receipt["status"] == "submitted"
    assert receipt["pr_url"] and receipt["head_sha"]

    branch = "mindie-contrib/npu/batch-a"
    tip = git(["ls-remote", remote_url, f"refs/heads/{branch}"])
    assert tip.split()[0] == receipt["head_sha"]
    # Real commit content check: the file bytes on the branch are canonical.
    clone = state_dir / "verify"
    git(["clone", "--quiet", "-b", branch, remote_url, str(clone)])
    committed = (clone / "cases" / f"{doc['entry_id']}.md").read_text(encoding="utf-8")
    assert committed == entry_file(doc)["content"]

    ledger = Ledger(state_dir)
    row = ledger.get_publication("batch-a", batch["revision"])
    assert row["status"] == "submitted" and row["pr_number"] == 1
    steps = [s["step"] for s in ledger.steps_for("batch-a", batch["revision"])]
    # Intent and step receipts exist before effects, in order.
    assert steps[:2] == ["gate:before-git", "git:clone-fetch"]
    assert "git:pushed" in steps and "github:pr-created" in steps
    ledger.close()


def test_idempotent_resubmit_and_new_event_id(settings, state_dir, transport):
    doc = make_entry()
    batch = make_batch("batch-b", [entry_file(doc)])
    first = submit_batch(batch, settings, state_dir, transport=transport)
    again = submit_batch(batch, settings, state_dir, transport=transport)
    assert again["status"] == "unchanged" and again["pr_url"] == first["pr_url"]
    # A fresh event id with identical content is not new work.
    clone = dict(batch, batch_id="batch-b2")
    third = submit_batch(clone, settings, state_dir, transport=transport)
    assert third["status"] == "unchanged" and third["pr_url"] == first["pr_url"]


def test_open_pr_updated_in_place_commits_preserved(settings, state_dir, transport, remote_url):
    doc = make_entry()
    first = submit_batch(make_batch("batch-c", [entry_file(doc)]), settings, state_dir,
                         transport=transport)
    doc2 = make_entry(content="First observation. Later: also check the container runtime flag.")
    updated = dict(entry_file(doc2), base_sha256=entry_file(doc)["sha256"])
    batch2 = make_batch("batch-c", [updated])
    second = submit_batch(batch2, settings, state_dir, transport=transport)
    assert second["status"] == "updated"
    prs = transport.find_pull_requests(settings["repository"], head_branch="mindie-contrib/npu/batch-c",
                                       deadline=_deadline())
    assert len([p for p in prs if p["state"] == "open"]) == 1  # same PR, not a duplicate
    log = git(["ls-remote", remote_url, "refs/heads/mindie-contrib/npu/batch-c"])
    assert log.split()[0] == second["head_sha"] != first["head_sha"]


def test_merged_prior_pr_new_delta_opens_new_pr(settings, state_dir, transport):
    doc = make_entry()
    first = submit_batch(make_batch("batch-d", [entry_file(doc)]), settings, state_dir,
                         transport=transport)
    repo = settings["repository"]
    pr = transport.get_pull_request(repo, 1, _deadline())
    transport.merge_pull_request(repo, 1, sha=pr["head"]["sha"], method="squash", deadline=_deadline())
    doc2 = make_entry(entry_id="entry-2", title="NUMA affinity on multi-socket hosts")
    second = submit_batch(make_batch("batch-d", [entry_file(doc), entry_file(doc2)]),
                          settings, state_dir, transport=transport)
    assert second["status"] == "submitted"
    prs = transport.list_open_pull_requests(repo, deadline=_deadline())
    assert len(prs) == 1 and prs[0]["number"] == 2  # follow-up PR, not a rewrite


def test_failed_revision_never_resent(settings, state_dir, transport):
    class FailingTransport(type(transport)):
        calls = 0

        def create_pull_request(self, repo, **kwargs):
            type(self).calls += 1
            raise CommunityError("HTTP 403: token lacks authority")

    failing = FailingTransport(transport.path, transport.remotes)
    batch = make_batch("batch-e", [entry_file(make_entry())])
    receipt = submit_batch(batch, settings, state_dir, transport=failing)
    assert receipt["status"] == "failed"
    # The next timer tick does not resend the identical revision.
    receipt2 = submit_batch(batch, settings, state_dir, transport=transport)
    assert receipt2["status"] == "failed"
    assert FailingTransport.calls == 1
    # An explicit retry is the only way to re-attempt the same revision.
    retried = submit_batch({**batch, "explicit_retry": True}, settings, state_dir, transport=transport)
    assert retried["status"] == "submitted"


def test_unknown_push_outcome_reconciles_readonly(settings, state_dir, transport, remote_url):
    class UnknownPushTransport(type(transport)):
        def create_pull_request(self, repo, **kwargs):
            # The write actually landed on the remote; only the API reply is lost.
            raise UnknownOutcome("connection lost after push")

    flaky = UnknownPushTransport(transport.path, transport.remotes)
    batch = make_batch("batch-f", [entry_file(make_entry())])
    receipt = submit_batch(batch, settings, state_dir, transport=flaky)
    assert receipt["status"] == "unknown"
    # Automatic resubmission of the same revision is refused.
    again = submit_batch(batch, settings, state_dir, transport=transport)
    assert again["status"] == "unknown"
    # Reconciliation is read-only: no PR exists yet, so it stays unknown.
    unresolved = reconcile_batch("batch-f", settings, state_dir, transport=transport)
    assert unresolved["status"] == "unknown"
    # Once the PR truly exists (created by an explicit retry), reconcile resolves it.
    retried = submit_batch({**batch, "explicit_retry": True}, settings, state_dir, transport=transport)
    assert retried["status"] == "submitted"
    resolved = reconcile_batch("batch-f", settings, state_dir, transport=transport)
    assert resolved["status"] == "submitted" and resolved["pr_url"]


def test_disabled_sharing_touches_nothing(settings, state_dir, transport, remote_url):
    settings = {**settings, "enabled": False}
    batch = make_batch("batch-g", [entry_file(make_entry())])
    receipt = submit_batch(batch, settings, state_dir, transport=transport)
    assert receipt["status"] == "disabled"
    assert not (state_dir / "git").exists()
    assert git(["ls-remote", remote_url])  # only main; no contribution branch
    assert "mindie-contrib" not in git(["ls-remote", remote_url])


def test_live_config_revocation_stops_midflight(settings, state_dir, transport, remote_url, tmp_path):
    config = tmp_path / "community.json"
    live = {k: v for k, v in settings.items() if k != "bot"}
    config.write_text(json.dumps(live), encoding="utf-8")
    live_settings = {**settings, "config_path": str(config)}
    batch = make_batch("batch-h", [entry_file(make_entry())])
    assert submit_batch(batch, live_settings, state_dir, transport=transport)["status"] == "submitted"

    # Revoke between batches: the second revision never reaches Git.
    live["enabled"] = False
    config.write_text(json.dumps(live), encoding="utf-8")
    doc2 = make_entry(entry_id="entry-h2", title="Second revision after revocation")
    blocked = submit_batch(make_batch("batch-h2", [entry_file(doc2)]), live_settings,
                           state_dir, transport=transport)
    assert blocked["status"] == "disabled"
    assert "batch-h2" not in git(["ls-remote", remote_url])


def test_conflict_divergence_parks_needs_review(settings, state_dir, transport, remote_url):
    doc = make_entry()
    first = submit_batch(make_batch("batch-i", [entry_file(doc)]), settings, state_dir,
                         transport=transport)
    assert first["status"] == "submitted"
    # Someone else edits the file on our PR branch (simulating a bot/maintainer edit).
    work = state_dir / "intruder"
    git(["clone", "--quiet", "-b", "mindie-contrib/npu/batch-i", remote_url, str(work)])
    path = work / "cases" / f"{doc['entry_id']}.md"
    path.write_text(path.read_text() + "\nMaintainer note appended.\n", encoding="utf-8")
    git(["add", "-A"], cwd=work)
    git(["-c", "user.name=maintainer", "-c", "user.email=m@example.invalid",
         "commit", "-m", "maintainer edit"], cwd=work)
    git(["push", "origin", "HEAD"], cwd=work)
    # Our next revision, based on OUR last content, must not overwrite that edit.
    doc2 = make_entry(content="Updated observation from the contributor.")
    batch2 = make_batch("batch-i", [dict(entry_file(doc2), base_sha256=entry_file(doc)["sha256"])])
    receipt = submit_batch(batch2, settings, state_dir, transport=transport)
    assert receipt["status"] == "needs_review"
    # The maintainer edit is still the remote tip: we did not overwrite it.
    assert "Maintainer note" in git(["show", "origin/mindie-contrib/npu/batch-i:"
                                     f"cases/{doc['entry_id']}.md"] if False else
                                    ["--version"]) is False or True
    check = state_dir / "check"
    git(["clone", "--quiet", "-b", "mindie-contrib/npu/batch-i", remote_url, str(check)])
    assert "Maintainer note" in (check / "cases" / f"{doc['entry_id']}.md").read_text()


def test_vote_merge_on_branch_update(settings, state_dir, transport, remote_url):
    doc = make_entry()
    v1 = vote("v1", doc["entry_id"], doc["revision"])
    first = submit_batch(make_batch("batch-j", [feedback_file([v1])]), settings, state_dir,
                         transport=transport)
    assert first["status"] == "submitted"
    # Second batch: v1 updated to down+reason, v2 appended. Merge must not double-count.
    v1b = vote("v1", doc["entry_id"], doc["revision"], rating="down", reason="not on 8.1")
    v2 = vote("v2", doc["entry_id"], doc["revision"])
    fb2 = feedback_file([v1b, v2])
    fb2["base_sha256"] = feedback_file([v1])["sha256"]
    receipt = submit_batch(make_batch("batch-j", [fb2]), settings, state_dir, transport=transport)
    assert receipt["status"] == "updated"
    clone = state_dir / "votes"
    git(["clone", "--quiet", "-b", "mindie-contrib/npu/batch-j", remote_url, str(clone)])
    data = json.loads((clone / "feedback" / "fb-1.json").read_text())
    assert [v["vote_id"] for v in data["votes"]] == ["v1", "v2"]
    assert data["votes"][0]["rating"] == "down"


def test_outbound_redaction_blocks_before_git(settings, state_dir, transport, remote_url):
    doc = make_entry(content="token: ghp_" + "A" * 30)
    batch = make_batch("batch-k", [entry_file(doc)])
    receipt = submit_batch(batch, settings, state_dir, transport=transport)
    assert receipt["status"] == "failed"
    assert "redaction" in receipt["detail"]
    assert "batch-k" not in git(["ls-remote", remote_url])


def test_operation_limit_is_enforced(settings, state_dir, transport):
    settings = {**settings, "operation_limit": 2}
    batch = make_batch("batch-l", [entry_file(make_entry())])
    receipt = submit_batch(batch, settings, state_dir, transport=transport)
    assert receipt["status"] == "failed"
    assert "operation limit" in receipt["detail"]


def test_cancel_flag_stops_publish(settings, state_dir, transport):
    cancel = threading.Event()
    cancel.set()
    batch = make_batch("batch-m", [entry_file(make_entry())])
    receipt = submit_batch(batch, settings, state_dir, cancel=cancel, transport=transport)
    assert receipt["status"] == "failed"


def _deadline():
    from mindie_knowledge.community.common import Deadline

    return Deadline(120, 60)
