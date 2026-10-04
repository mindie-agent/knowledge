"""Remote-authoritative publication lifecycle, with real local Git.

The Git layer is real (clone/commit/push subprocesses); the GitHub API is the
file-backed dev transport. These are mechanism checks for the loop-level
contract "submitted content obeys the remote; locally we keep only unsent
observations plus minimal receipts" — not GitHub business acceptance.
"""

import hashlib
import json
import subprocess

import pytest

from mindie_knowledge.community import reconcile_batch, submit_batch
from mindie_knowledge.community.batch import contribution_branch
from mindie_knowledge.community.common import UnknownOutcome
from mindie_knowledge.community.transport import FileTransport
from mindie_knowledge.loop import documents, settings as loop_settings
from mindie_knowledge.loop.documents import make_entry, render_entry
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.export import build_batch
from mindie_knowledge.loop.store import Store

from .conftest import REPO, entry_file, package_files, git, make_batch, make_entry, make_remote

PRODUCER = "a" * 64


def _deadline():
    from mindie_knowledge.community.common import Deadline

    return Deadline(120, 60)


def _loop_settings(tmp_path, remote_url):
    """One real shared settings file valid for BOTH the loop and community."""
    return loop_settings.write(
        tmp_path / "community.json", enabled=True, repository=REPO,
        project_roots=[tmp_path], transport="file",
        dev_remotes={REPO: remote_url},
        transaction_seconds=120, operation_limit=60,
    )


def _engine(store, settings):
    return Engine(store, settings_path=str(settings.path))


def _transport(store, remote_url):
    """The dev transport the engine will build itself (same state file)."""
    return FileTransport(store.root / "outbox" / "dev-github.json", {REPO: remote_url})


def _branch(domain, batch_id):
    return contribution_branch(domain, batch_id)


def _branch_file(remote_url, branch, path, tmp_path, name):
    work = tmp_path / name
    git(["clone", "--quiet", "-b", branch, remote_url, str(work)])
    return (work / path).read_text(encoding="utf-8")


def _branch_task(remote_url, branch, task_id, tmp_path, name):
    from mindie_knowledge.materials import validate_package_files
    work = tmp_path / name
    git(["clone", "--quiet", "-b", branch, remote_url, str(work)])
    root = work / "tasks" / task_id
    files = {path.relative_to(root).as_posix(): path.read_bytes().decode("utf-8")
             for path in root.rglob("*.md")}
    validate_package_files(files, "npu")
    return "\n".join(files.values())


def _bot_package(remote_url, branch, doc, tmp_path, name):
    import shutil
    work = tmp_path / name
    git(["clone", "--quiet", "-b", branch, remote_url, str(work)])
    root = work / "tasks" / doc["entry_id"]
    shutil.rmtree(root)
    for item in package_files(doc):
        target = work / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(item["content"].encode("utf-8"))
    git(["add", "-A"], cwd=work)
    git(["-c", "user.name=bot", "-c", "user.email=bot@example.invalid",
         "commit", "-m", "correct current task package"], cwd=work)
    git(["push", "origin", "HEAD"], cwd=work)


def _bot_rewrite(remote_url, branch, path, new_text, tmp_path, name):
    """A maintainer/bot commit directly on our contribution branch."""
    work = tmp_path / name
    git(["clone", "--quiet", "-b", branch, remote_url, str(work)])
    (work / path).write_text(new_text, encoding="utf-8", newline="\n")
    git(["add", "-A"], cwd=work)
    git(["-c", "user.name=bot", "-c", "user.email=bot@example.invalid",
         "commit", "-m", "bot edit"], cwd=work)
    git(["push", "origin", "HEAD"], cwd=work)


def _sync_pr_ref(transport, remote_url, number):
    """Keep the dev double consistent with a real PR after a simulated bot
    commit: move the actual Git ``refs/pull/N/head`` to the branch tip and
    refresh the stored API head — real bot commits move both."""
    data = json.loads(transport.path.read_text(encoding="utf-8"))
    pr = data["repos"][REPO]["pulls"][str(number)]
    branch = pr["head"]["ref"]
    tip = git(["ls-remote", remote_url, f"refs/heads/{branch}"]).split()[0]
    git(["--git-dir", remote_url, "update-ref", f"refs/pull/{number}/head", tip])
    pr["head"]["sha"] = tip
    transport.path.write_text(json.dumps(data), encoding="utf-8")


def _close_pr_unmerged(transport, number):
    data = json.loads(transport.path.read_text(encoding="utf-8"))
    pulls = data["repos"][REPO]["pulls"]
    pulls[str(number)]["state"] = "closed"
    pulls[str(number)]["merged"] = False
    transport.path.write_text(json.dumps(data), encoding="utf-8")


def _submit(engine, store, batch_id):
    engine._submit(store.batch(batch_id))
    return store.batch(batch_id)


def test_failed_first_send_then_new_observation_is_a_creation(tmp_path):
    """A failed first send leaves no confirmed receipt: the next batch with
    added observations is a first creation, never a false 'remote deleted'
    conflict."""
    remote_url = make_remote(tmp_path, "content")
    settings = _loop_settings(tmp_path, remote_url)
    store = Store(tmp_path / "store", "npu")
    try:
        engine = _engine(store, settings)
        doc = store.create_draft(
            kind="experience", title="First send fails", summary="s",
            content="Original body.", owner=PRODUCER,
            generation=settings.generation,
        )
        built = build_batch(store, settings=settings)
        batch_id = built[0]

        class RefusedTransport(FileTransport):
            def create_pull_request(self, repo, **kwargs):
                from mindie_knowledge.community.common import CommunityError

                raise CommunityError("HTTP 403: token lacks authority")

        engine.community = {
            "submit_batch": lambda batch, cfg, sd, cancel=None: submit_batch(
                batch, cfg, sd, cancel=cancel,
                transport=RefusedTransport(store.root / "outbox" / "dev-github.json",
                                           {REPO: remote_url}),
            ),
            "reconcile_batch": reconcile_batch,
        }
        row = _submit(engine, store, batch_id)
        assert row["status"] == "failed"
        assert store.sent_receipt(doc["entry_id"]) is None  # nothing confirmed

        updated, appended = store.append_observation(
            doc["entry_id"], "New observation after the failure.",
            marker="ab" * 16, producer=PRODUCER, generation=settings.generation,
        )
        assert appended
        rebuilt = build_batch(store, settings=settings)
        assert rebuilt is not None and rebuilt[0] == batch_id
        item = next(f for f in rebuilt[2]["files"]
                    if f["path"] == f"tasks/{doc['entry_id']}/index.md")
        assert item["base_sha256"] is None  # creation semantics, not withdrawal
        engine = _engine(store, settings)
        row = _submit(engine, store, batch_id)
        assert row["status"] == "submitted", row["detail"]
        content = _branch_task(remote_url, _branch("npu", batch_id),
                               doc["entry_id"], tmp_path, "check")
        assert "Original body." in content
        assert "New observation after the failure." in content
    finally:
        store.close()


def test_bot_edited_package_refuses_stale_updates_multi_cycle(tmp_path):
    """Remote correction/deletion wins; stale local packages never overwrite it."""
    remote_url = make_remote(tmp_path, "content")
    settings = _loop_settings(tmp_path, remote_url)
    store = Store(tmp_path / "store", "npu")
    try:
        engine = _engine(store, settings)
        doc = store.create_draft(kind="experience", title="Original title", summary="orig summary",
            content="First paragraph.\n\nRemoved incorrect paragraph.", owner=PRODUCER,
            generation=settings.generation)
        batch_id = build_batch(store, settings=settings)[0]
        assert _submit(engine, store, batch_id)["status"] == "submitted"
        branch = _branch("npu", batch_id)
        assert store._row(doc["entry_id"])["draft_revision"] == doc["revision"]
        corrected = make_entry(entry_id=doc["entry_id"], title="Corrected title",
            summary="Corrected source", content="First paragraph.")
        _bot_package(remote_url, branch, corrected, tmp_path, "bot-correction")
        _sync_pr_ref(_transport(store, remote_url), remote_url, 1)
        remote_tip = git(["ls-remote", remote_url, f"refs/heads/{branch}"]).split()[0]
        for index in range(2):
            store.append_observation(doc["entry_id"], f"Later local observation {index}.",
                marker=str(index + 1) * 32, producer=PRODUCER, generation=settings.generation)
            assert build_batch(store, settings=settings) is not None
            receipt = _submit(engine, store, batch_id)
            assert receipt["status"] == "needs_review", receipt["detail"]
            assert git(["ls-remote", remote_url, f"refs/heads/{branch}"]).split()[0] == remote_tip
        public = _branch_task(remote_url, branch, doc["entry_id"], tmp_path, "check-correction")
        assert "Corrected title" in public and "Removed incorrect" not in public
        assert "Later local observation" not in public
        assert len(json.loads(_transport(store, remote_url).path.read_text())["repos"][REPO]["pulls"]) == 1
    finally:
        store.close()


def test_merged_then_open_followup_keeps_all_observations_on_one_pr(tmp_path):
    """A third save must update the suffixed follow-up, not branch from main."""
    remote_url = make_remote(tmp_path, 'content')
    settings = _loop_settings(tmp_path, remote_url)
    store = Store(tmp_path / 'store', 'npu')
    try:
        engine = _engine(store, settings)
        doc = store.create_draft(kind='experience', title='Case', summary='s',
            content='Original public observation.', owner=PRODUCER, generation=settings.generation)
        batch_id = build_batch(store, settings=settings)[0]
        first = _submit(engine, store, batch_id)
        assert first['status'] == 'submitted'
        transport = _transport(store, remote_url)
        transport.merge_pull_request(REPO, 1, sha=first['head_sha'], method='merge', deadline=_deadline())
        for index, text in enumerate(('Second observation before review.', 'Third observation while review is pending.')):
            # Restart/compaction must still use the confirmed PR body.
            engine = _engine(store, settings)
            store.append_observation(doc['entry_id'], text, producer=PRODUCER,
                marker=str(index + 1) * 32, generation=settings.generation)
            build_batch(store, settings=settings)
            receipt = _submit(engine, store, batch_id)
            assert receipt['status'] == ('submitted' if index == 0 else 'updated'), receipt['detail']
        pulls = json.loads(transport.path.read_text())['repos'][REPO]['pulls']
        assert len(pulls) == 2
        assert pulls['2']['head']['ref'] != _branch('npu', batch_id)
        content = _branch_task(remote_url, pulls['2']['head']['ref'], doc["entry_id"], tmp_path, 'verify-followup')
        for text in ('Original public observation.', 'Second observation before review.', 'Third observation while review is pending.'):
            assert content.count(text) == 1
        assert store.sent_receipt(doc['entry_id'])['pr_url'].endswith('/2')

        # Conflicting recorded open PRs require inspection, never an arbitrary
        # base that could silently drop one branch's accepted observations.
        data = json.loads(transport.path.read_text())
        data['repos'][REPO]['pulls']['1'].update(state='open', merged=False)
        transport.path.write_text(json.dumps(data))
        store.append_observation(doc['entry_id'], 'Fourth observation.', producer=PRODUCER,
            marker='4' * 32, generation=settings.generation)
        build_batch(store, settings=settings)
        refused = _submit(engine, store, batch_id)
        assert refused['status'] == 'needs_review'
        assert len(json.loads(transport.path.read_text())['repos'][REPO]['pulls']) == 2
    finally:
        store.close()


def test_rejected_pr_quarantines_its_material_and_new_entries_flow(tmp_path):
    """A proven closed-unmerged PR quarantines exactly its own entries; later
    unrelated material is published on a fresh branch/PR without resurrecting
    the rejected content."""
    remote_url = make_remote(tmp_path, "content")
    settings = _loop_settings(tmp_path, remote_url)
    store = Store(tmp_path / "store", "npu")
    try:
        engine = _engine(store, settings)
        rejected_doc = store.create_draft(
            kind="experience", title="Rejected case", summary="r",
            content="Content the maintainer refused.", owner=PRODUCER,
            generation=settings.generation,
        )
        built = build_batch(store, settings=settings)
        batch_id = built[0]

        class LostResponse(FileTransport):
            def create_pull_request(self, repo, **kwargs):
                pr = super().create_pull_request(repo, **kwargs)
                raise UnknownOutcome("connection lost after PR creation")

        engine.community = {
            "submit_batch": lambda batch, cfg, sd, cancel=None: submit_batch(
                batch, cfg, sd, cancel=cancel,
                transport=LostResponse(store.root / "outbox" / "dev-github.json",
                                       {REPO: remote_url}),
            ),
            "reconcile_batch": reconcile_batch,
        }
        row = _submit(engine, store, batch_id)
        assert row["status"] == "unknown"
        # The PR existed but is now closed unmerged: reconciliation proves
        # the rejection read-only.
        _close_pr_unmerged(_transport(store, remote_url), 1)
        engine._reconcile(store.batch(batch_id))
        row = store.batch(batch_id)
        assert row["status"] == "rejected"
        assert store.quarantined_entries() == {
            rejected_doc["entry_id"]: "pr-rejected"
        }

        # The rejected entry keeps its local draft (readable) but never
        # re-enters an automatic batch, even after new observations.
        store.append_observation(
            rejected_doc["entry_id"], "local follow-up", marker="cc" * 16,
            producer=PRODUCER, generation=settings.generation,
        )
        fresh = store.create_draft(
            kind="experience", title="Unrelated valid case", summary="f",
            content="Fresh unrelated material.", owner=PRODUCER,
            generation=settings.generation,
        )
        engine2 = _engine(store, settings)
        built2 = build_batch(store, settings=settings)
        assert built2 is not None
        paths = [f["path"] for f in built2[2]["files"]]
        assert len(paths) >= 2 and all(path.startswith(f"tasks/{fresh['entry_id']}/") for path in paths)
        row2 = _submit(engine2, store, batch_id)
        assert row2["status"] == "submitted", row2["detail"]
        pulls = json.loads(_transport(store, remote_url).path.read_text())
        assert len(pulls["repos"][REPO]["pulls"]) == 2  # a new PR, not a rewrite
        prs = pulls["repos"][REPO]["pulls"]
        new_pr = prs["2"]
        new_branch = new_pr["head"]["ref"]
        assert new_branch != _branch("npu", batch_id)  # retired branch untouched
        content = _branch_task(remote_url, new_branch, fresh["entry_id"], tmp_path, "check")
        assert "Fresh unrelated material." in content
        assert "maintainer refused" not in content
        # The rejected batch is never auto-resent; explicit retry remains the
        # only operator escape.
        again = submit_batch(built[2], settings.as_dict(), store.root / "outbox",
                             transport=_transport(store, remote_url))
        assert again["status"] == "rejected"
    finally:
        store.close()


def test_unknown_write_confirmed_by_ancestry_after_head_advances(tmp_path):
    """A lost-response write whose PR head has since advanced (bot commits)
    is confirmed by Git ancestry of the exact expected commit — branch
    existence alone is never treated as proof."""
    remote_url = make_remote(tmp_path, "content")
    settings = _loop_settings(tmp_path, remote_url)
    store = Store(tmp_path / "store", "npu")
    try:
        engine = _engine(store, settings)
        doc = store.create_draft(
            kind="experience", title="Lost response case", summary="s",
            content="Body of the lost-response write.", owner=PRODUCER,
            generation=settings.generation,
        )
        built = build_batch(store, settings=settings)
        batch_id = built[0]
        branch = _branch("npu", batch_id)

        class LostPush(FileTransport):
            def create_pull_request(self, repo, **kwargs):
                # The push landed on the remote; only the API response is
                # lost, and the PR record never reaches the caller.
                raise UnknownOutcome("connection lost after push")

        engine.community = {
            "submit_batch": lambda batch, cfg, sd, cancel=None: submit_batch(
                batch, cfg, sd, cancel=cancel,
                transport=LostPush(store.root / "outbox" / "dev-github.json",
                                   {REPO: remote_url}),
            ),
            "reconcile_batch": reconcile_batch,
        }
        row = _submit(engine, store, batch_id)
        assert row["status"] == "unknown"

        # The PR actually exists server-side (created before the loss), and a
        # bot then commits on top, moving the head past the expected commit.
        transport = _transport(store, remote_url)
        data = json.loads(transport.path.read_text())
        tip = git(["ls-remote", remote_url, f"refs/heads/{branch}"]).split()[0]
        pulls = data.setdefault("repos", {}).setdefault(
            REPO, {"pulls": {}, "next_pr": 8}
        )["pulls"]
        pulls["7"] = {
            "number": 7, "title": "t", "body": "b", "state": "open",
            "merged": False, "head": {"ref": branch, "sha": tip},
            "base": {"ref": "main"},
            "html_url": f"https://example.invalid/{REPO}/pull/7",
        }
        transport.path.write_text(json.dumps(data), encoding="utf-8")
        # A real PR's Git ref tracks its branch: record refs/pull/7/head at
        # the current tip when the PR appears, then after the bot advance.
        git(["--git-dir", remote_url, "update-ref", "refs/pull/7/head", tip])
        _bot_rewrite(remote_url, branch, "BOT_NOTES.md", "Current review note.\n",
                     tmp_path, "bot-advance")
        # The PR record and its Git ref now show the advanced head.
        advanced_tip = git(["ls-remote", remote_url, f"refs/heads/{branch}"]).split()[0]
        assert advanced_tip != tip
        _sync_pr_ref(transport, remote_url, 7)

        receipt = reconcile_batch(
            batch_id, settings.as_dict(), store.root / "outbox",
            transport=_transport(store, remote_url),
        )
        assert receipt["status"] == "submitted", receipt["detail"]
        assert "ancestor" in receipt["detail"]
    finally:
        store.close()


def test_noop_update_receipt_reports_actual_remote_package(tmp_path):
    """An exact current package produces no commit and reports actual hashes."""
    remote_url = make_remote(tmp_path, "content")
    settings = _loop_settings(tmp_path, remote_url)
    state_dir = tmp_path / "state"
    transport = FileTransport(state_dir / "dev-github.json", {REPO: remote_url})
    doc = make_entry()
    first = submit_batch(make_batch("batch-noop", [entry_file(doc)]), settings.as_dict(),
                         state_dir, transport=transport)
    assert first["status"] == "submitted"
    branch = _branch("npu", "batch-noop")
    corrected = make_entry(title="Edited title", content="Corrected body.")
    _bot_package(remote_url, branch, corrected, tmp_path, "bot")
    _sync_pr_ref(transport, remote_url, 1)
    current_files = {item["path"]: item["sha256"] for item in package_files(corrected)}
    # A reviewed exact-current package can acknowledge the remote correction;
    # a stale package is separately tested to stop with needs_review.
    candidate = dict(entry_file(corrected), base_sha256=entry_file(doc)["sha256"])
    tip_before = git(["ls-remote", remote_url, f"refs/heads/{branch}"]).split()[0]
    second = submit_batch(make_batch("batch-noop", [candidate]), settings.as_dict(),
                          state_dir, transport=transport)
    assert second["status"] == "unchanged", second["detail"]
    assert git(["ls-remote", remote_url, f"refs/heads/{branch}"]).split()[0] == tip_before
    reported = {item["path"]: item["sha256"] for item in second["files"] if item["sha256"] is not None}
    assert reported == current_files
    assert len(transport.find_pull_requests(REPO, head_branch=branch, deadline=_deadline())) == 1


def test_reconcile_recovers_actual_file_identity_after_response_loss(tmp_path):
    """An unknown package update is confirmed by ancestry with exact hashes.

    A later unrelated bot commit does not change the sent receipt. Reconcile
    stays read-only and preserves the identity recorded before the push.
    """
    remote_url = make_remote(tmp_path, "content")
    settings = _loop_settings(tmp_path, remote_url)
    store = Store(tmp_path / "store", "npu")
    try:
        engine = _engine(store, settings)
        doc = store.create_draft(
            kind="experience", title="Response loss case", summary="s",
            content="Base paragraph.", owner=PRODUCER,
            generation=settings.generation,
        )
        built = build_batch(store, settings=settings)
        batch_id = built[0]
        row = _submit(engine, store, batch_id)
        assert row["status"] == "submitted"
        branch = _branch("npu", batch_id)
        path = f"tasks/{doc['entry_id']}/index.md"

        # New material is appended on its proven base; the API reply is lost.
        store.append_observation(doc["entry_id"], "Observation A.",
            producer=PRODUCER, marker="dd" * 16, generation=settings.generation)
        built2 = build_batch(store, settings=settings)
        assert built2 is not None

        class LostUpdate(FileTransport):
            def update_pull_request(self, repo, number, **kwargs):
                raise UnknownOutcome("connection lost after push")

        engine.community = {
            "submit_batch": lambda batch, cfg, sd, cancel=None: submit_batch(
                batch, cfg, sd, cancel=cancel,
                transport=LostUpdate(store.root / "outbox" / "dev-github.json",
                                     {REPO: remote_url}),
            ),
            "reconcile_batch": reconcile_batch,
        }
        row = _submit(engine, store, batch_id)
        assert row["status"] == "unknown"
        merged_sha = hashlib.sha256(
            _branch_file(remote_url, branch, path, tmp_path, "pushed").encode()
        ).hexdigest()
        candidate = json.loads(store.batch(batch_id)["batch"])
        candidate_sha = next(f["sha256"] for f in candidate["files"]
                             if f["path"] == path)
        assert merged_sha == candidate_sha  # the exact frozen package landed

        # Bot advances the PR once more before reconciliation.
        _bot_rewrite(remote_url, branch, "BOT_NOTES.md",
                     "bot note\n", tmp_path, "botmore")
        _sync_pr_ref(_transport(store, remote_url), remote_url, 1)
        receipt = reconcile_batch(
            batch_id, settings.as_dict(), store.root / "outbox",
            transport=_transport(store, remote_url),
        )
        assert receipt["status"] == "submitted", receipt["detail"]
        assert receipt["files"], "reconciled receipt must carry actual files"
        reported = {f["path"]: f for f in receipt["files"]}
        assert reported[path]["sha256"] == merged_sha
        # Engine propagation: the stored per-entry receipt is the actual
        # committed identity, not the candidate.
        engine2 = _engine(store, settings)
        engine2._reconcile(store.batch(batch_id))
        assert store.sent_receipt(doc["entry_id"])["sha256"] == merged_sha
        # Reconciliation stayed read-only: no new PR, same branch count.
        pulls = json.loads(_transport(store, remote_url).path.read_text())
        assert len(pulls["repos"][REPO]["pulls"]) == 1
    finally:
        store.close()


def test_transient_environment_failure_resubmits_the_stored_batch(tmp_path):
    """A clone-level environment failure records the batch unavailable (never
    a fake failure of the content); resubmission of the exact stored payload
    succeeds once the environment recovers."""
    remote_url = make_remote(tmp_path, "content")
    settings = _loop_settings(tmp_path, remote_url)
    store = Store(tmp_path / "store", "npu")
    try:
        engine = _engine(store, settings)
        doc = store.create_draft(
            kind="experience", title="Offline first attempt", summary="s",
            content="Body retried after the outage.", owner=PRODUCER,
            generation=settings.generation,
        )
        built = build_batch(store, settings=settings)
        batch_id = built[0]

        from mindie_knowledge.community import gitops
        from mindie_knowledge.community.common import TransientError

        real_ensure_clone = gitops.ensure_clone

        def offline_clone(*args, **kwargs):
            raise TransientError("git clone failed: could not resolve host")

        engine.community = {
            "submit_batch": lambda batch, cfg, sd, cancel=None: submit_batch(
                batch, cfg, sd, cancel=cancel,
                transport=_transport(store, remote_url),
            ),
            "reconcile_batch": reconcile_batch,
        }
        gitops.ensure_clone = offline_clone
        try:
            row = _submit(engine, store, batch_id)
        finally:
            gitops.ensure_clone = real_ensure_clone
        assert row["status"] == "unavailable"
        assert store.outbox_unavailable()[0]["batch_id"] == batch_id
        # The stored payload is resubmitted unchanged once the environment
        # recovers (the outbox worker gates this through persisted backoff).
        engine2 = _engine(store, settings)
        row = _submit(engine2, store, batch_id)
        assert row["status"] == "submitted", row["detail"]
        content = _branch_task(remote_url, _branch("npu", batch_id),
                               doc["entry_id"], tmp_path, "check")
        assert "Body retried after the outage." in content
    finally:
        store.close()
