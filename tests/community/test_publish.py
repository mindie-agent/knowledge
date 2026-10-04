"""submit_batch / reconcile_batch against REAL local Git remotes.

The Git layer is real (clone/commit/push subprocesses). GitHub API behaviour
uses the file-backed dev transport, with controlled fault injection at that
boundary. Dev-transport success is mechanism evidence, not GitHub acceptance.
"""

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from mindie_knowledge.community import reconcile_batch, submit_batch
from mindie_knowledge.community.common import CommunityError, UnknownOutcome
from mindie_knowledge.community.ledger import Ledger
from authority_support import damage_database

from .conftest import (
    entry_file,
    feedback_file,
    git,
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
    committed = (clone / entry_file(doc)["path"]).read_text(encoding="utf-8")
    assert committed == entry_file(doc)["content"]

    ledger = Ledger(state_dir)
    row = ledger.get_publication("batch-a", batch["revision"])
    assert row["status"] == "submitted" and row["pr_number"] == 1
    steps = [s["step"] for s in ledger.steps_for("batch-a", batch["revision"])]
    # Intent and step receipts exist before effects, in order.
    assert steps[:2] == ["publication-contract:validated", "gate:before-worktree"]
    assert "git:pushed" in steps and "github:pr-created" in steps
    ledger.close()


@pytest.mark.parametrize('failure_point', ['push-step', 'gate-step', 'intent-step', 'pr-step', 'final'])
def test_completed_external_write_survives_receipt_failure(
        settings, state_dir, transport, remote_url, monkeypatch, failure_point):
    original_step, original_finish = Ledger.record_step, Ledger.finish_publication
    def broken_step(self, batch_id, revision, step, detail=''):
        if (failure_point, step) in {('push-step', 'git:pushed'), ('gate-step', 'gate:before-api'),
                                     ('intent-step', 'github:create-pr'), ('pr-step', 'github:pr-created')}:
            raise sqlite3.OperationalError('synthetic local receipt failure')
        return original_step(self, batch_id, revision, step, detail)
    def broken_finish(self, batch_id, revision, **kwargs):
        if failure_point == 'final' and kwargs['status'] == 'submitted':
            raise sqlite3.OperationalError('synthetic local terminal receipt failure')
        return original_finish(self, batch_id, revision, **kwargs)
    monkeypatch.setattr(Ledger, 'record_step', broken_step)
    monkeypatch.setattr(Ledger, 'finish_publication', broken_finish)
    batch = make_batch('receipt-effect', [entry_file(make_entry())])
    receipt = submit_batch(batch, settings, state_dir, transport=transport)
    assert receipt['recording_failed'] and receipt['failed_stage'] == 'receipt'
    tip = git(['ls-remote', remote_url, 'refs/heads/mindie-contrib/npu/receipt-effect'])
    assert tip.split()[0] == receipt['head_sha']
    prs = transport.list_open_pull_requests(settings['repository'], deadline=_deadline())
    if failure_point in {'push-step', 'gate-step', 'intent-step'}:
        assert receipt['status'] == 'failed' and receipt['completed_steps'] == ['git-push']
        assert prs == []
    else:
        assert receipt['status'] == 'submitted' and receipt['pr_url']
        assert len(prs) == 1 and receipt['completed_steps'] == ['git-push', 'pr-create']
    monkeypatch.setattr(Ledger, 'record_step', original_step)
    monkeypatch.setattr(Ledger, 'finish_publication', original_finish)
    def forbid_write(*args, **kwargs):
        raise AssertionError('receipt recovery must not repeat the external write')
    monkeypatch.setattr(transport, 'create_pull_request', forbid_write)
    again = submit_batch(batch, settings, state_dir, transport=transport)
    assert again['status'] in ({'failed'} if failure_point in {'push-step', 'gate-step', 'intent-step'} else {'submitted', 'unchanged'})


@pytest.mark.parametrize('authority', ['publication', 'runtime'])
@pytest.mark.parametrize('before_step', ['git:push', 'github:create-pr'])
@pytest.mark.parametrize('damage', ['missing_marker', 'replaced_db'])
def test_live_authority_loss_blocks_next_real_external_write(
        settings, state_dir, transport, remote_url, tmp_path, monkeypatch, authority, before_step, damage):
    from contextlib import closing
    from mindie_knowledge.loop.store import Store
    owner = Store(tmp_path / 'source', 'demo')
    original = Ledger.record_step
    def damage_after_intent(self, batch_id, revision, step, detail=''):
        result = original(self, batch_id, revision, step, detail)
        if step == before_step:
            db = self.db if authority == 'publication' else owner.db
            path = state_dir / 'community-ledger.sqlite3' if authority == 'publication' else owner.root / 'state-v4.sqlite3'
            if damage == 'missing_marker':
                path.with_name(path.name + '.owner').unlink()
            else:
                replacement = path.with_name('replacement.sqlite3')
                with closing(sqlite3.connect(replacement)) as copy:
                    db.backup(copy)
                read = (lambda: self.get_publication(batch_id, revision)) if authority == 'publication' else owner.status
                damage_database(db, path, read=read, replacement=replacement)
        return result
    monkeypatch.setattr(Ledger, 'record_step', damage_after_intent)
    batch = make_batch('lost-authority', [entry_file(make_entry())])
    try:
        result = submit_batch(batch, settings, state_dir, transport=transport,
                              authority_guard=owner.db.assert_authority)
        assert result['status'] == 'unavailable'
        prs = transport.list_open_pull_requests(settings['repository'], deadline=_deadline())
        assert prs == []
        tip = git(['ls-remote', remote_url, 'refs/heads/mindie-contrib/npu/lost-authority'])
        if before_step == 'git:push':
            assert not tip and not result['external_write_completed']
        else:
            assert tip.split()[0] == result['head_sha']
            assert result['completed_steps'] == ['git-push']
    finally:
        owner.close()


@pytest.mark.parametrize('gate_number', [3, 4])
@pytest.mark.parametrize('change', ['disabled', 'corrupt'])
def test_profile_consent_change_stops_next_actual_external_write(
        settings, state_dir, transport, remote_url, tmp_path, monkeypatch, gate_number, change):
    from mindie_knowledge import consent_store
    from mindie_knowledge.community import publish
    consent = tmp_path / 'consent.json'
    consent_store.record_choice(consent, 'contribute')
    settings['consent_config'] = str(consent)
    Path(settings['config_path']).write_text(json.dumps(settings))
    original, calls = publish._settings_gate, []
    def change_at_gate(admitted):
        calls.append(1)
        if len(calls) == gate_number:
            if change == 'disabled':
                consent_store.record_choice(consent, 'disabled')
            else:
                consent.write_text('{broken')
        original(admitted)
    monkeypatch.setattr(publish, '_settings_gate', change_at_gate)
    batch = make_batch('revoke-native-write', [entry_file(make_entry())])
    receipt = submit_batch(batch, settings, state_dir, transport=transport)
    assert receipt['status'] == ('disabled' if change == 'disabled' else 'unavailable')
    assert not transport.list_open_pull_requests(settings['repository'], deadline=_deadline())
    tip = git(['ls-remote', remote_url, 'refs/heads/mindie-contrib/npu/revoke-native-write'])
    if gate_number == 3:
        assert tip == ''
        assert receipt['external_write_completed'] is False
    else:
        assert tip.split()[0] == receipt['head_sha']
        assert receipt['completed_steps'] == ['git-push']
        assert 'Git push completed' in receipt['detail']


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


@pytest.mark.parametrize("timed_out", [False, True])
def test_existing_branch_checkout_failure_stops_all_writes(
    settings, state_dir, transport, remote_url, monkeypatch, timed_out,
):
    from mindie_knowledge.community import gitops
    from mindie_knowledge.community.common import ProcessResult

    class RefusedPullRequest(type(transport)):
        def create_pull_request(self, repo, **kwargs):
            raise CommunityError("synthetic PR permission refusal")

    batch = make_batch("checkout-failure", [entry_file(make_entry())])
    first = submit_batch(batch, settings, state_dir,
                         transport=RefusedPullRequest(transport.path, transport.remotes))
    assert first["status"] == "failed", first
    branch = "mindie-contrib/npu/checkout-failure"
    before = git(["ls-remote", remote_url, f"refs/heads/{branch}"])
    assert before
    real_run = gitops.run_argv

    def failed_checkout(argv, **kwargs):
        if "checkout" in argv:
            return ProcessResult(128, b"", b"synthetic existing checkout failure", timed_out)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(gitops, "run_argv", failed_checkout)
    for name in ("apply_files", "stage_and_commit", "push_branch"):
        monkeypatch.setattr(gitops, name, lambda *_args, **_kwargs: pytest.fail("write after failed checkout"))
    result = submit_batch({**batch, "explicit_retry": True}, settings, state_dir, transport=transport)
    assert result["status"] == ("unavailable" if timed_out else "failed"), result
    assert ("timed out" if timed_out else "synthetic existing checkout failure") in result["detail"]
    assert git(["ls-remote", remote_url, f"refs/heads/{branch}"]) == before
    assert transport.list_open_pull_requests(settings["repository"], deadline=_deadline()) == []


def test_missing_publication_executable_resumes_identical_batch_after_repair(
    settings, state_dir, transport, tmp_path,
):
    from mindie_knowledge.community.common import run_argv

    class MissingExecutableTransport(type(transport)):
        def create_pull_request(self, repo, **kwargs):
            # Exercise the real spawn boundary, not a forged unavailable receipt.
            run_argv([str(tmp_path / "missing-gh")], timeout=1)

    batch = make_batch("batch-missing-gh", [entry_file(make_entry())])
    failing = MissingExecutableTransport(transport.path, transport.remotes)
    receipt = submit_batch(batch, settings, state_dir, transport=failing)
    assert receipt["status"] == "unavailable"
    assert "executable not found" in receipt["detail"]
    # The same stored payload can resume without explicit_retry or a new id.
    repaired = submit_batch(batch, settings, state_dir, transport=transport)
    assert repaired["status"] in {"submitted", "updated"}
    assert repaired["pr_url"]


def test_unlaunchable_executable_is_recoverable_before_any_child_runs(tmp_path):
    from mindie_knowledge.community.common import run_argv, TransientError

    with pytest.raises(TransientError, match="cannot start"):
        run_argv([str(tmp_path)], timeout=1)


def test_unknown_push_outcome_reconciles_readonly(settings, state_dir, transport, remote_url):
    class UnknownPushTransport(type(transport)):
        def create_pull_request(self, repo, **kwargs):
            # The push landed; the API result cannot yet be established.
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
    # An explicit retry flag still cannot prove the unknown write was absent.
    retried = submit_batch({**batch, "explicit_retry": True}, settings, state_dir, transport=transport)
    assert retried['status'] == 'unknown'
    assert transport.list_open_pull_requests(settings['repository'], deadline=_deadline()) == []
    # Simulate the delayed remote result becoming visible; only a read follows.
    transport.create_pull_request(settings['repository'], title='Delayed response fixture', body='',
                                  head='mindie-contrib/npu/batch-f', base='main', deadline=_deadline())
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
    path = work / entry_file(doc)["path"]
    path.write_text(path.read_text() + "\nMaintainer note appended.\n", encoding="utf-8")
    git(["add", "-A"], cwd=work)
    git(["-c", "user.name=maintainer", "-c", "user.email=m@example.invalid",
         "commit", "-m", "maintainer edit"], cwd=work)
    git(["push", "origin", "HEAD"], cwd=work)
    # The PR head ref moves with the branch. It is not left on the old tip.
    git(["push", "origin", "HEAD:refs/pull/1/head"], cwd=work)
    # Our next revision, based on OUR last content, must not overwrite that edit.
    doc2 = make_entry(content="Updated observation from the contributor.")
    batch2 = make_batch("batch-i", [dict(entry_file(doc2), base_sha256=entry_file(doc)["sha256"])])
    receipt = submit_batch(batch2, settings, state_dir, transport=transport)
    assert receipt["status"] == "needs_review"
    # The maintainer edit is still the remote tip: we did not overwrite it.
    check = state_dir / "check"
    git(["clone", "--quiet", "-b", "mindie-contrib/npu/batch-i", remote_url, str(check)])
    assert "Maintainer note" in (check / entry_file(doc)["path"]).read_text()


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


def test_network_shaped_git_failures_are_transient_but_auth_is_not():
    from mindie_knowledge.community.gitops import _is_transient_git_error

    # Observed isolated evidence: proxy/DNS/connection failures are the
    # environment, never the content or the authorization.
    assert _is_transient_git_error(
        "fatal: unable to access 'https://example.invalid/x.git/': "
        "Proxy CONNECT aborted"
    )
    assert _is_transient_git_error("fatal: Could not resolve host: github.com")
    assert _is_transient_git_error("fatal: connection timed out")
    assert _is_transient_git_error("error: HTTP 503: Service Unavailable")
    assert not _is_transient_git_error("fatal: Authentication failed")
    assert not _is_transient_git_error("HTTP 403: token lacks authority")
    assert not _is_transient_git_error("fatal: repository not found")
    assert not _is_transient_git_error("")


def test_deleted_entry_is_not_restored_by_a_pending_correction(settings, state_dir, transport, remote_url):
    doc = make_entry()
    first = submit_batch(make_batch('batch-delete', [entry_file(doc)]), settings,
                         state_dir, transport=transport)
    assert first['status'] == 'submitted'
    work = state_dir / 'withdraw'
    git(['clone', '--quiet', '-b', 'mindie-contrib/npu/batch-delete', remote_url, str(work)])
    path = entry_file(doc)["path"]
    git(['rm', path], cwd=work)
    git(['-c', 'user.name=maintainer', '-c', 'user.email=m@example.invalid',
         'commit', '-m', 'Withdraw incorrect entry'], cwd=work)
    git(['push', 'origin', 'HEAD'], cwd=work)
    git(['push', 'origin', 'HEAD:refs/pull/1/head'], cwd=work)
    before = git(['rev-parse', 'HEAD'], cwd=work)
    correction = make_entry(content='Later observation retained privately.')
    batch = make_batch('batch-delete', [dict(entry_file(correction),
                       base_sha256=entry_file(doc)['sha256'])])
    result = submit_batch(batch, settings, state_dir, transport=transport)
    assert result['status'] == 'needs_review' and 'deleted' in result['detail']
    assert git(['ls-remote', remote_url, 'refs/heads/mindie-contrib/npu/batch-delete']).split()[0] == before


def test_contract_mismatch_before_intent_or_remote_write_and_same_payload_recovers(
    settings, state_dir, transport, remote_url, tmp_path,
):
    from mindie_knowledge.publication_contract import make_contract, render_contract
    from mindie_knowledge.community.common import sha256_text
    wanted = render_contract(make_contract('npu', 'b' * 40))
    settings['publication_contract_sha256'] = sha256_text(wanted)
    config = tmp_path / 'community.json'
    data = json.loads(config.read_text())
    data['publication_contract_sha256'] = settings['publication_contract_sha256']
    config.write_text(json.dumps(data))
    batch = make_batch('contract-recovery', [entry_file(make_entry())])
    before = git(['ls-remote', remote_url])
    receipt = submit_batch(batch, settings, state_dir, transport=transport)
    assert receipt['status'] == 'unavailable' and receipt['error_code'] == 'contract_mismatch'
    assert receipt['failed_stage'] == 'publication-contract'
    assert receipt['external_write_attempted'] is False
    ledger = Ledger(state_dir)
    try:
        assert ledger.get_publication(batch['batch_id'], batch['revision']) is None
    finally:
        ledger.close()
    assert git(['ls-remote', remote_url]) == before
    assert transport.list_open_pull_requests(settings['repository'], deadline=_deadline()) == []
    work = tmp_path / 'repair-contract'
    git(['clone', remote_url, str(work)])
    (work / 'publication-contract.json').write_bytes(wanted.encode('utf-8'))
    git(['add', '.'], cwd=work)
    git(['-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
         'commit', '-m', 'reviewed deployment combination'], cwd=work)
    git(['push', 'origin', 'main'], cwd=work)
    # Identical frozen payload, no explicit retry or new batch identity needed.
    receipt = submit_batch(batch, settings, state_dir, transport=transport)
    assert receipt['status'] == 'submitted', receipt
