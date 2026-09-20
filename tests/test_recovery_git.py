"""Explicit post-exhaustion inspection and real-Git continuation recovery.

Uses real local Git repositories (bare remote + clones) and a stub transport
for the PR API; no network, no mocks of the Git layer.
"""

import hashlib
import json
import subprocess
import sys

import pytest

from mindie_knowledge.community.ledger import Ledger
from mindie_knowledge.community.publish import inspect_batch, reconcile_batch
from mindie_knowledge.loop import documents
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store

from conftest import write_settings

PRODUCER = "c" * 64
REPO = "mindie-agent/knowledge-test"


class StubTransport:
    def __init__(self, prs):
        self.prs = prs

    def get_pull_request(self, repo, number, deadline):
        for pr in self.prs:
            if pr.get("number") == number:
                return pr
        from mindie_knowledge.community.common import CommunityError

        raise CommunityError("no such PR")

    def find_pull_requests(self, repo, head_branch, deadline):
        return [pr for pr in self.prs
                if pr.get("head", {}).get("ref") == head_branch]


def _settings_dict(tmp_path, **extra):
    settings = write_settings(tmp_path / "community.json", enabled=True,
                              roots=[tmp_path], repository=REPO, **extra)
    return settings, settings.as_dict()


def _ledger_row(state_dir, *, status="unknown", head_sha=None, pr_number=7):
    ledger = Ledger(state_dir)
    try:
        ledger.record_intent(batch_id="batch-x", revision="r" * 64,
                             domain="test", repository=REPO,
                             branch="mindie/test/batch-x")
        if head_sha:
            ledger.record_step("batch-x", "r" * 64, "git:pushed", head_sha)
        ledger.finish_publication("batch-x", "r" * 64, status=status,
                                  detail="cap exhausted", pr_number=pr_number,
                                  head_sha=head_sha)
    finally:
        ledger.close()


def _read_row(state_dir):
    ledger = Ledger(state_dir)
    try:
        return ledger.get_publication("batch-x", "r" * 64)
    finally:
        ledger.close()


def test_inspect_confirms_exhausted_row_only_at_exact_expected_head(tmp_path):
    _, cfg = _settings_dict(tmp_path)
    state_dir = tmp_path / "state"
    _ledger_row(state_dir, status="failed", head_sha="a" * 40)
    pr = {"number": 7, "state": "open", "merged": False,
          "html_url": "https://x/pull/7",
          "head": {"ref": "mindie/test/batch-x", "sha": "a" * 40}}
    receipt = inspect_batch("batch-x", cfg, state_dir,
                            transport=StubTransport([pr]))
    assert receipt["status"] == "submitted", receipt["detail"]
    assert _read_row(state_dir)["status"] == "submitted"

    # A matching open PR at a DIFFERENT head is not proof this revision
    # arrived: the row stays unknown, never confirmed, never failed.
    _ledger_row(tmp_path / "s2", status="failed", head_sha="b" * 40)
    pr_moved = dict(pr, head={"ref": "mindie/test/batch-x", "sha": "c" * 40})
    receipt = inspect_batch("batch-x", cfg, tmp_path / "s2",
                            transport=StubTransport([pr_moved]))
    assert receipt["status"] == "unknown"
    assert _read_row(tmp_path / "s2")["status"] == "unknown"

    # No remote evidence at all: unknown stays unknown (never magically
    # confirmed-failed by inspection).
    _ledger_row(tmp_path / "s3", status="unknown", head_sha="b" * 40)
    receipt = inspect_batch("batch-x", cfg, tmp_path / "s3",
                            transport=StubTransport([]))
    assert receipt["status"] == "unknown"

    # A closed unmerged PR is a proven failure.
    _ledger_row(tmp_path / "s4", status="unknown", head_sha="b" * 40)
    closed = dict(pr, state="closed", merged=False,
                  head={"ref": "mindie/test/batch-x", "sha": "b" * 40})
    receipt = inspect_batch("batch-x", cfg, tmp_path / "s4",
                            transport=StubTransport([closed]))
    assert receipt["status"] == "failed"

    # Historical cap-failed with no evidence becomes unknown, not kept failed.
    _ledger_row(tmp_path / "s5", status="failed", head_sha="b" * 40)
    receipt = inspect_batch("batch-x", cfg, tmp_path / "s5",
                            transport=StubTransport([]))
    assert receipt["status"] == "unknown"
    assert _read_row(tmp_path / "s5")["status"] == "unknown"

    # A matching PR with no remote head SHA is not confirmation.
    _ledger_row(tmp_path / "s6", status="unknown", head_sha="b" * 40)
    no_sha = dict(pr, head={"ref": "mindie/test/batch-x"})
    receipt = inspect_batch("batch-x", cfg, tmp_path / "s6",
                            transport=StubTransport([no_sha]))
    assert receipt["status"] == "unknown"


def test_reconcile_cap_stays_unknown_and_is_read_only(tmp_path):
    _, cfg = _settings_dict(tmp_path)
    state_dir = tmp_path / "state"
    _ledger_row(state_dir, status="unknown", head_sha="a" * 40)

    class Recording(StubTransport):
        writes = 0

        def create_pull_request(self, *args, **kwargs):
            type(self).writes += 1
            raise AssertionError("reconcile must not write")

        def update_pull_request(self, *args, **kwargs):
            type(self).writes += 1
            raise AssertionError("reconcile must not write")

    transport = Recording([])
    last = None
    for _ in range(6):
        last = reconcile_batch("batch-x", cfg, state_dir, transport=transport)
    assert last["status"] == "unknown"
    assert "inspect" in last["detail"]
    assert Recording.writes == 0
    assert _read_row(state_dir)["status"] == "unknown"


def test_overwritten_observed_head_is_not_the_expected_commit(tmp_path):
    _, cfg = _settings_dict(tmp_path)
    state_dir = tmp_path / "state"
    intended = "a" * 40
    observed = "b" * 40
    ledger = Ledger(state_dir)
    try:
        ledger.record_intent(batch_id="batch-x", revision="r" * 64,
                             domain="test", repository=REPO,
                             branch="mindie/test/batch-x")
        ledger.record_step("batch-x", "r" * 64, "git:pushed", intended)
        ledger.finish_publication("batch-x", "r" * 64, status="unknown",
                                  detail="observed later head", pr_number=7,
                                  head_sha=observed)
    finally:
        ledger.close()
    pr_observed = {"number": 7, "state": "open", "merged": False,
                   "html_url": "https://x/pull/7",
                   "head": {"ref": "mindie/test/batch-x", "sha": observed}}
    receipt = inspect_batch("batch-x", cfg, state_dir,
                            transport=StubTransport([pr_observed]))
    assert receipt["status"] == "unknown"
    pr_intended = dict(pr_observed, head={"ref": "mindie/test/batch-x",
                                          "sha": intended})
    receipt = inspect_batch("batch-x", cfg, state_dir,
                            transport=StubTransport([pr_intended]))
    assert receipt["status"] == "submitted"
    assert receipt["head_sha"] == intended


def _git(args, cwd):
    result = subprocess.run(["git", *args], cwd=cwd, text=True,
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _bare_remote(tmp_path):
    """Real bare remote with one contribution file on a branch; returns
    (bare, head_sha, body, path) with the branch DELETED afterwards."""
    bare = tmp_path / "remote.git"
    work = tmp_path / "seed"
    _git(["init", "--bare", str(bare)], tmp_path)
    _git(["init", str(work)], tmp_path)
    _git(["config", "user.email", "t@t"], work)
    _git(["config", "user.name", "t"], work)
    entry_id = "d" * 64
    doc = documents.make_entry(entry_id=entry_id, domain="test",
                               kind="experience", title="Sent case",
                               summary="s", content="the sent body")
    from mindie_knowledge.loop.documents import render_entry

    body = render_entry(doc)
    path = f"cases/{entry_id}.md"
    (work / "cases").mkdir()
    (work / path).write_text(body)
    _git(["add", "."], work)
    _git(["commit", "-m", "base"], work)
    _git(["branch", "-M", "main"], work)
    _git(["remote", "add", "origin", str(bare)], work)
    _git(["push", "origin", "main"], work)
    _git(["checkout", "-b", "mindie/test/batch-z"], work)
    _git(["push", "origin", "mindie/test/batch-z"], work)
    head = _git(["rev-parse", "HEAD"], work)
    _git(["push", "origin", "--delete", "mindie/test/batch-z"], work)
    _git(["config", "uploadpack.allowAnySHA1InWant", "true"], bare)
    return bare, head, body, path, doc


def test_restore_uses_exact_receipt_head_after_branch_deletion(tmp_path):
    bare, head, body, path, doc = _bare_remote(tmp_path)
    settings = write_settings(
        tmp_path / "community.json", enabled=True, roots=[tmp_path],
        repository=REPO, dev_remotes={REPO: str(bare)},
    )
    store = Store(tmp_path / "store", "test")
    local = store.create_draft(kind="experience", title="Sent case", summary="s",
                               content="the sent body", owner=PRODUCER,
                               entry_id=doc["entry_id"],
                               generation=settings.generation)
    assert local["revision"] == doc["revision"]
    from mindie_knowledge.loop.export import lineage_of

    batch_id = lineage_of("test", settings.generation)
    receipt = {
        "schema": "mindie-contribution-receipt/1", "batch_id": batch_id,
        "revision": "r" * 64, "domain": "test",
        "entry_refs": [store.ref(doc["entry_id"], doc["revision"])],
        "files": [{"path": path,
                   "sha256": hashlib.sha256(body.encode()).hexdigest()}],
        "summary": "test",
    }
    with store._write_txn():
        store.db.execute(
            "INSERT INTO outbox VALUES(?,?,?,?,?,?,?,1.0,NULL,1.0,0,NULL,?)",
            (batch_id, "r" * 64, json.dumps(receipt), "submitted", "",
             "https://x/pull/9", head, settings.generation),
        )
    store.compact_confirmed(batch_id)
    assert store._row(doc["entry_id"])["draft_revision"] is None

    engine = Engine(store, settings_path=tmp_path / "community.json")
    assert engine._restore_sent_draft(doc["entry_id"], settings.generation)
    row = store._row(doc["entry_id"])
    assert row["draft_revision"] == doc["revision"]
    restored = store.get(store.ref(doc["entry_id"]))
    assert restored["content"] == "the sent body"  # exact prior remote body
    updated, appended = store.append_observation(
        doc["entry_id"], "second observation", marker="ef" * 32,
        producer=PRODUCER, generation=settings.generation,
    )
    assert appended and "the sent body" in updated["content"]
    assert "second observation" in updated["content"]

    # A tampered receipt hash never seeds a base.
    store2 = Store(tmp_path / "store2", "test")
    store2.create_draft(kind="experience", title="Sent case", summary="s",
                        content="the sent body", owner=PRODUCER,
                        entry_id=doc["entry_id"],
                        generation=settings.generation)
    bad = dict(receipt, files=[{"path": path, "sha256": "0" * 64}])
    with store2._write_txn():
        store2.db.execute(
            "INSERT INTO outbox VALUES(?,?,?,?,?,?,?,1.0,NULL,1.0,0,NULL,?)",
            (batch_id, "r" * 64, json.dumps(bad), "submitted", "",
             "https://x/pull/9", head, settings.generation),
        )
    store2.compact_confirmed(batch_id)
    engine2 = Engine(store2, settings_path=tmp_path / "community.json")
    assert not engine2._restore_sent_draft(doc["entry_id"], settings.generation)
    store.close()
    store2.close()


def test_aba_continuation_after_lineage_replace_and_branch_removal(tmp_path):
    """Real local Git: confirm A, compact, later B-only batch replaces the
    lineage outbox; A still restores from the per-entry receipt after the
    contribution branch is gone. Sent bodies leave the DB; B's unsent draft
    stays."""
    from mindie_knowledge.loop.export import build_batch, lineage_of

    bare, head, body, path, doc = _bare_remote(tmp_path)
    settings = write_settings(
        tmp_path / "community.json", enabled=True, roots=[tmp_path],
        repository=REPO, dev_remotes={REPO: str(bare)},
    )
    store = Store(tmp_path / "store", "test")
    local = store.create_draft(kind="experience", title="Sent case", summary="s",
                               content="the sent body", owner=PRODUCER,
                               entry_id=doc["entry_id"],
                               generation=settings.generation)
    assert local["revision"] == doc["revision"]
    batch_id = lineage_of("test", settings.generation)
    receipt = {
        "schema": "mindie-contribution-receipt/1", "batch_id": batch_id,
        "revision": "r" * 64, "domain": "test",
        "entry_refs": [store.ref(doc["entry_id"], doc["revision"])],
        "files": [{"path": path,
                   "sha256": hashlib.sha256(body.encode()).hexdigest()}],
        "summary": "test",
    }
    with store._write_txn():
        store.db.execute(
            "INSERT INTO outbox VALUES(?,?,?,?,?,?,?,1.0,NULL,1.0,0,NULL,?)",
            (batch_id, "r" * 64, json.dumps(receipt), "submitted", "",
             "https://x/pull/9", head, settings.generation),
        )
    store.compact_confirmed(batch_id)
    assert store._row(doc["entry_id"])["draft_revision"] is None
    assert store._revision_doc(doc["entry_id"], doc["revision"]) is None
    hash_a = store.sent_file_hash(doc["entry_id"])
    assert hash_a and store.sent_receipt(doc["entry_id"])["head_sha"] == head

    store.create_draft(kind="experience", title="Entry B", summary="b",
                       content="b-only later batch", owner=PRODUCER,
                       generation=settings.generation)
    built = build_batch(store, settings=settings,
                        revision_fn=lambda *a: "s" * 64)
    assert built is not None
    replaced = json.loads(store.batch(batch_id)["batch"])
    assert replaced.get("schema") == "mindie-contribution/1"
    assert all("the sent body" not in f.get("content", "") for f in replaced["files"])
    assert store.sent_file_hash(doc["entry_id"]) == hash_a

    assert store._revision_doc(doc["entry_id"], doc["revision"]) is None
    assert any("b-only later batch" in json.loads(row[0]).get("content", "")
               for row in store.db.execute("SELECT doc FROM revisions"))

    engine = Engine(store, settings_path=tmp_path / "community.json")
    assert engine._restore_sent_draft(doc["entry_id"], settings.generation)
    restored = store.get(store.ref(doc["entry_id"]))
    assert restored["content"] == "the sent body"
    updated, appended = store.append_observation(
        doc["entry_id"], "A continued after B", marker="ab" * 32,
        producer=PRODUCER, generation=settings.generation,
    )
    assert appended and "the sent body" in updated["content"]
    assert "A continued after B" in updated["content"]
    store.close()
