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


def _import_pipeline(tmp_path):
    from conftest import make_admission
    from lane_support import load_parser
    from mindie_knowledge.loop.activation import Admission
    from mindie_knowledge.loop.transcript_redaction import install_scanner
    from material_worker_fixture import command
    from test_history_import import append, message

    repo = tmp_path / "main"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "fixture@example.invalid"], repo)
    _git(["config", "user.name", "fixture"], repo)
    (repo / "README.md").write_text("# Public package fixture\n")
    _git(["add", "."], repo)
    _git(["commit", "-qm", "initialize"], repo)
    settings = write_settings(tmp_path / "community.json", enabled=True, roots=[tmp_path],
                              repository=REPO, transport="file", dev_remotes={REPO: str(repo)})
    store = Store(tmp_path / "store", "test")
    engine = Engine(store, settings_path=settings.path,
                    admission=Admission(make_admission(tmp_path, project_root=tmp_path)),
                    transcript_adapter=load_parser("codex"), capture_mode="public-transcript",
                    redactor_executable=install_scanner(), summary_command=command(title="Imported task"))
    source = tmp_path / "history.jsonl"
    append(source, dict(type="session_meta", payload=dict(id="old-unactivated", cwd=str(tmp_path))),
           message("Original imported evidence before publication."))
    return engine, source, repo


def _publish_imported_current(engine, source, repo):
    from test_history_import import contribute, summary_due
    from mindie_knowledge.loop.export import build_batch
    from mindie_knowledge.loop.feed import Feed
    from package_fixture import write_package

    imported = contribute((engine, source))
    summary_due(engine)
    store = engine.store
    entry_id = store.get(imported["ref"].split("@", 1)[0])["entry_id"]
    package = store.materials.export_task(entry_id)
    assert package["ready"]
    built = build_batch(store, settings=engine._settings())
    write_package(repo, package)
    _git(["add", "-A"], repo)
    _git(["commit", "-qm", "publish current task package"], repo)
    commit = _git(["rev-parse", "HEAD"], repo)
    store.mark_batch(built[0], "submitted", head_sha=commit, pr_url="https://example.invalid/pull/9")
    feed = Feed(store, dict(repository=REPO, ref="main", domain="test", url=str(repo)))
    assert feed.sync()["status"] == "synced"
    store.compact_confirmed(built[0])
    assert store._row(entry_id)["draft_revision"] is None
    return entry_id, package, built[0], feed


def test_import_continues_from_current_feed_after_publication_compaction(tmp_path):
    """Actual import -> Git main -> current feed -> compact -> incremental import.

    Earlier blocks and their completed indexes survive; only new blocks need
    indexing. Neither a deleted contribution branch nor old body DB rows are
    needed to continue the task.
    """
    from test_history_import import append, message, contribute
    from mindie_knowledge.loop.export import build_batch
    engine, source, repo = _import_pipeline(tmp_path)
    store = engine.store
    try:
        entry_id, package, _, _ = _publish_imported_current(engine, source, repo)
        old_files = dict(package["files"])
        append(source, message("Later evidence after the task seemed finished."))
        result = contribute((engine, source))
        assert result["status"] == "extended"
        task = store.materials.read_task(entry_id)
        body = store.materials.get_document(entry_id, source="draft")["content"]
        assert body.count("Original imported evidence") == 1
        assert body.count("Later evidence after the task") == 1
        current = store.materials.export_task(entry_id)
        assert all(current["files"][path] == text for path, text in old_files.items() if path != "index.md")
        assert task["blocks"][0]["indexed"] is True
        assert task["blocks"][-1]["indexed"] is False
        assert build_batch(store, settings=engine._settings()) is None
        assert "Original imported evidence" not in "\n".join(store.db.iterdump())
    finally:
        store.close()


def test_aba_continuation_uses_current_files_after_lineage_replacement(tmp_path):
    """A's task and receipt survive an unrelated B publication, with no DB bodies."""
    from test_history_import import append, message, contribute
    from mindie_knowledge.loop.export import build_batch
    engine, source, repo = _import_pipeline(tmp_path)
    store = engine.store
    try:
        entry_id, _, batch_id, feed = _publish_imported_current(engine, source, repo)
        hash_a = store.sent_file_hash(entry_id)
        second = store.create_draft(kind="experience", title="Unrelated task B", summary="Second task",
            content="Unrelated new material.", owner=PRODUCER, generation=engine._settings().generation)
        built = build_batch(store, settings=engine._settings())
        assert built[0] == batch_id and built[3] == [second["entry_id"]]
        assert store.sent_file_hash(entry_id) == hash_a
        assert all("content" not in item for item in json.loads(store.batch(batch_id)["batch"])["files"])
        append(source, message("A continued after B arrived."))
        assert contribute((engine, source))["status"] == "extended"
        body = store.materials.get_document(entry_id, source="draft")["content"]
        assert "Original imported evidence" in body and "A continued after B" in body
        # Main withdrawal is current source authority. A later local increment
        # must not recreate it or advance the source cursor.
        import shutil
        shutil.rmtree(repo / "tasks" / entry_id)
        _git(["add", "-A"], repo)
        _git(["commit", "-qm", "withdraw task"], repo)
        assert feed.sync()["entries"] == 0
        before = dict(store.db.execute("SELECT * FROM material_streams").fetchone())
        append(source, message("This increment must remain uncommitted after withdrawal."))
        with pytest.raises((ValueError, RuntimeError), match="withdrawn|resurrect"):
            contribute((engine, source))
        assert dict(store.db.execute("SELECT * FROM material_streams").fetchone()) == before
        assert store.get(store.ref(entry_id))["withdrawn"] is True
    finally:
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
