"""Explicit post-exhaustion inspection and real-Git continuation recovery.

Uses real local Git repositories (bare remote + clones) and a stub transport
for the PR API; no network, no mocks of the Git layer.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

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

    # A closed unmerged PR is a proven content-level rejection.
    _ledger_row(tmp_path / "s4", status="unknown", head_sha="b" * 40)
    closed = dict(pr, state="closed", merged=False,
                  head={"ref": "mindie/test/batch-x", "sha": "b" * 40})
    receipt = inspect_batch("batch-x", cfg, tmp_path / "s4",
                            transport=StubTransport([closed]))
    assert receipt["status"] == "rejected"

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
    (work / path).write_bytes(body.encode("utf-8"))
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


def _seed_merged_pr(store, bare, head):
    """The receipt's PR is merged. Main, not the deleted branch, is the body."""
    from mindie_knowledge.community.transport import FileTransport

    FileTransport(store.root / "outbox" / "dev-github.json", {REPO: str(bare)}).seed(
        REPO,
        pulls={
            "9": {
                "number": 9,
                "state": "closed",
                "merged": True,
                "html_url": "https://x/pull/9",
                "head": {
                    "ref": "mindie/test/batch-z",
                    "sha": head,
                    "repo": {"full_name": REPO},
                },
                "base": {"ref": "main", "repo": {"full_name": REPO}},
            }
        },
    )


def _restore_diagnostics(engine, entry_id):
    """Expose the real Git error for platform-specific receipt failures."""
    from mindie_knowledge.community import gitops
    from mindie_knowledge.community.common import run_argv

    receipt = engine.store.sent_receipt(entry_id)
    settings = engine._settings()
    repo = settings.as_dict().get("fork") or settings.repository
    work = engine.state_dir / "git" / repo.replace("/", "_")
    result = run_argv(
        ["git", "show", f"{receipt['head_sha']}:{receipt['path']}", "--"],
        cwd=work, env=gitops.git_env(settings.as_dict()), timeout=5,
        max_output=8192,
    )
    return {"errors": engine.errors, "git_exit": result.code,
            "git_error": result.err_text[:1200], "work_path_length": len(str(work))}


def test_restore_uses_current_remote_body_after_branch_deletion(tmp_path):
    bare, head, body, path, doc = _bare_remote(tmp_path)
    settings = write_settings(
        tmp_path / "community.json", enabled=True, roots=[tmp_path],
        repository=REPO, dev_remotes={REPO: str(bare)}, transport="file",
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
    _seed_merged_pr(store, bare, head)

    engine = Engine(store, settings_path=tmp_path / "community.json")
    assert engine._restore_sent_draft(doc["entry_id"], settings.generation), _restore_diagnostics(engine, doc["entry_id"])
    row = store._row(doc["entry_id"])
    assert row["draft_revision"] == doc["revision"]
    restored = store.get(store.ref(doc["entry_id"]))
    # The current remote body (merged main) is the append base, not an old
    # confirmed-head draft.
    assert restored["content"] == "the sent body"
    updated, appended = store.append_observation(
        doc["entry_id"], "second observation", marker="ef" * 32,
        producer=PRODUCER, generation=settings.generation,
    )
    assert appended and "the sent body" in updated["content"]
    assert "second observation" in updated["content"]

    # A tampered receipt path never seeds a base from mismatched content:
    # restore reads the CURRENT remote body and requires the entry identity.
    store2 = Store(tmp_path / "store2", "test")
    store2.create_draft(kind="experience", title="Sent case", summary="s",
                        content="the sent body", owner=PRODUCER,
                        entry_id=doc["entry_id"],
                        generation=settings.generation)
    bad = dict(receipt, files=[{"path": "cases/" + "0" * 64 + ".md",
                                "sha256": hashlib.sha256(body.encode()).hexdigest()}])
    with store2._write_txn():
        store2.db.execute(
            "INSERT INTO outbox VALUES(?,?,?,?,?,?,?,1.0,NULL,1.0,0,NULL,?)",
            (batch_id, "r" * 64, json.dumps(bad), "submitted", "",
             "https://x/pull/9", head, settings.generation),
        )
    store2.compact_confirmed(batch_id)
    engine2 = Engine(store2, settings_path=tmp_path / "community.json")
    # The wrong path means no receipt was ever recorded for this entry.
    assert not engine2._restore_sent_draft(doc["entry_id"], settings.generation), engine2.errors
    assert any("no matching receipt" in err for err in engine2.errors), engine2.errors
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
        repository=REPO, dev_remotes={REPO: str(bare)}, transport="file",
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
    _seed_merged_pr(store, bare, head)

    engine = Engine(store, settings_path=tmp_path / "community.json")
    assert engine._restore_sent_draft(doc["entry_id"], settings.generation), _restore_diagnostics(engine, doc["entry_id"])
    restored = store.get(store.ref(doc["entry_id"]))
    assert restored["content"] == "the sent body"
    updated, appended = store.append_observation(
        doc["entry_id"], "A continued after B", marker="ab" * 32,
        producer=PRODUCER, generation=settings.generation,
    )
    assert appended and "the sent body" in updated["content"]
    assert "A continued after B" in updated["content"]
    store.close()


def _clone_workdir_in_windows_max_path_window(tmp_path):
    """Workdir long enough that cwd + `<sha>:cases/<64>.md` exceeds 260, while
    the real case file path stays at or under 260 — the native CI failure."""
    suffix = Path("outbox") / "git" / "mindie-agent_knowledge-test"
    path = f"cases/{'d' * 64}.md"
    object_expr_len = 40 + 1 + len(path)
    min_work = 260 - object_expr_len  # combined length 261 once a separator is added
    max_work = 260 - 1 - len(path)
    for n in range(0, 240):
        candidate = tmp_path / ("w" * n) / suffix if n else tmp_path / suffix
        if min_work <= len(str(candidate)) <= max_work:
            return candidate, path
    pytest.skip("cannot construct a workdir in the Windows MAX_PATH window")


def test_show_file_exact_blob_from_long_workdir_full_entry_id(tmp_path):
    """Real Git: show_file returns the blob at the exact SHA, not a filename.

    Native Windows CI at 7008e8a failed `git show <sha>:cases/<64chars>.md`
    because git statted the whole object expression as a working-tree path.
    workdir length 152 + expression 114 exceeds MAX_PATH 260; the case file
    itself is shorter. `--` after the object stops that probe.
    """
    from mindie_knowledge.community import gitops
    from mindie_knowledge.community.common import Deadline

    work, path = _clone_workdir_in_windows_max_path_window(tmp_path)
    work.mkdir(parents=True)
    _git(["init"], work)
    _git(["config", "user.email", "t@t"], work)
    _git(["config", "user.name", "t"], work)
    marker = "exact-blob-body-at-receipt-head"
    body = f"{marker}\n"
    (work / "cases").mkdir()
    (work / path).write_bytes(body.encode("utf-8"))
    _git(["add", "--", path], work)
    _git(["commit", "-m", "sent"], work)
    sha = _git(["rev-parse", "HEAD"], work)
    assert len(sha) == 40
    object_expr = f"{sha}:{path}"
    assert len(str(work)) + 1 + len(object_expr) > 260
    assert len(str(work / path)) <= 260

    # Untracked file whose relative path equals the object expression: without
    # `--`, git reports an ambiguous filename instead of showing the blob.
    # Windows cannot create a colon in a path component.
    try:
        decoy = work / f"{sha}:cases" / Path(path).name
        decoy.parent.mkdir(parents=True)
        decoy.write_bytes(b"not the blob\n")
    except OSError:
        pass

    cat = subprocess.run(
        ["git", "cat-file", "-p", object_expr],
        cwd=work, capture_output=True, timeout=30,
    )
    assert cat.returncode == 0, cat.stderr
    blob = cat.stdout.decode("utf-8")
    assert marker in blob

    raw = gitops.show_file(work, sha, path, Deadline(), env=gitops.GIT_ENV)
    assert raw == blob
    assert gitops.show_file(
        work, sha, "cases/missing.md", Deadline(), env=gitops.GIT_ENV,
    ) is None
