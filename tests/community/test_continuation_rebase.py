"""Confirmed main corrections retain only unsent blocks; real local Git/RPC storage.

The public GitHub side is FileTransport. No model or private transcript is used.
"""
from contextlib import closing
import hashlib
import shutil
from types import SimpleNamespace

import pytest

from mindie_knowledge.community import submit_batch
from mindie_knowledge.community.common import Deadline
from mindie_knowledge.loop.export import build_batch
from mindie_knowledge.loop.feed import Feed
from mindie_knowledge.loop.store import Store, digest
from mindie_knowledge.materials import MaterialStore
from mindie_knowledge.materials.publication import load_batch_payload
from material_worker_fixture import package_body
from package_fixture import write_package
from .conftest import git

TASK = "b" * 64


def append_parts(store, *texts, navigation="Current synthetic observations."):
    row = store._row(TASK)
    revision = (row["draft_revision"] or row["published_revision"]) if row else None
    blocks = [dict(block_id=digest(["continuation-fixture", text]), text=text,
                   title="Observation", summary="An independent local observation.", source_range={}) for text in texts]
    with store._write_txn():
        result = store.materials.append_batch(TASK, blocks, navigation, title="Synthetic task",
                                             revision=revision, source="draft", promote=False)
        store._bind_material_draft(result["entry"], generation="g1")
    return result["entry"]


def build(store):
    return build_batch(store, settings=SimpleNamespace(generation="g1"))


def mark(store, batch_id, receipt):
    store.mark_batch(batch_id, receipt["status"], attempted=True, pr_url=receipt.get("pr_url"),
                     head_sha=receipt.get("head_sha"), actual_files=receipt.get("files"))


def merge(transport, settings, receipt):
    return transport.merge_pull_request(settings["repository"], int(receipt["pr_url"].rsplit("/", 1)[1]),
                                        sha=receipt["head_sha"], method="squash", deadline=Deadline(120, 100))


def corrected_package(tmp_path):
    with closing(MaterialStore(tmp_path / "correction", "npu")) as store:
        store.append_batch(TASK, [dict(block_id="c" * 64, text="Maintainer-corrected public observation.\n",
            title="Corrected observation", summary="The earlier claim was withdrawn.", source_range={})],
            "Only the corrected observation is established; later observations need their own evidence.",
            title="Maintainer-corrected navigation", status="complete", promote=True)
        return store.export_task(TASK)


def amend_main(tmp_path, remote_url, package=None, name="maintainer"):
    work = tmp_path / name
    git(["clone", "--quiet", remote_url, str(work)])
    shutil.rmtree(work / "tasks" / TASK)
    if package is not None:
        write_package(work, package)
    git(["add", "-A"], cwd=work)
    git(["-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "maintainer correction"], cwd=work)
    git(["push", "--quiet", "origin", "main"], cwd=work)


def package_from_batch(batch):
    from mindie_knowledge.materials import validate_package_files
    prefix = f"tasks/{TASK}/"
    return validate_package_files({file["path"][len(prefix):]: file["content"]
                                   for file in batch["files"] if file["path"].startswith(prefix)
                                   and file.get("content") is not None}, "npu")


def commit_new_increment(store, text, number=1):
    """Exercise the production intake transaction with an already-redacted block."""
    block = dict(block_id=digest(["continuation", number, text]), text=text,
                 title="New local evidence", summary="An independent observation.", source_range={"part": number})
    prepared = dict(batch_id=digest(["batch", number, text]), blocks=[block], redaction_state={})
    previous = store.material_stream("synthetic-public-stream")
    start = previous["source_cursor"] if previous else 0
    return store.commit_material_increment(stream_key="synthetic-public-stream", entry_id=TASK,
        prepared=prepared, start=start, end=start + len(text), source_identity="synthetic",
        authorization=dict(generation="g1", id="synthetic"), owner="test-owner", observed_stream=previous)


def test_confirmed_main_redaction_rebases_unsent_blocks_and_preserves_later_open_pr(
        settings, state_dir, transport, remote_url, tmp_path, monkeypatch):
    from mindie_knowledge.materials import summarizer
    def forbid_model(*_a, **_k):
        pytest.fail("mechanical continuation invoked a model")
    monkeypatch.setattr(summarizer, "summarize_batch", forbid_model)
    with closing(Store(tmp_path / "producer", "npu")) as store:
        feed = Feed(store, dict(repository=settings["repository"], domain="npu", ref="main", url=remote_url))
        original = append_parts(store, "REMOVED_SOURCE_CANARY: the old claim.\n")
        batch_id, _, initial, _, _ = build(store)
        first = submit_batch(initial, settings, state_dir, transport=transport)
        mark(store, batch_id, first)
        merge(transport, settings, first)
        assert feed.sync()["status"] == "synced"
        # Stable new block exists locally while a maintainer replaces the
        # already-sent source with a new identity on main.
        append_parts(store, "Unsent observation B remains useful.\n", navigation="Stale local claim about removed material.")
        corrected = corrected_package(tmp_path)
        amend_main(tmp_path, remote_url, corrected)
        assert feed.sync()["status"] == "synced"
        # Reorganization and the repeated no-op inspect manifests only.
        with monkeypatch.context() as read_guard:
            from pathlib import Path
            read = Path.read_bytes
            def no_body(path):
                assert path.parent.name != "blocks", "rebase read unrelated body bytes"
                return read(path)
            read_guard.setattr(Path, "read_bytes", no_body)
            read_guard.setattr(store.materials, "get_document", lambda *_a, **_k: pytest.fail("rebase assembled a task"))
            changed = store.rebase_draft_on_published(TASK)
            unchanged = store.rebase_draft_on_published(TASK)
            assert changed["revision"] == unchanged["revision"] and changed["content"] == ""
        second_batch = build(store)[2]
        candidate = package_from_batch(second_batch)
        body = package_body(candidate)
        assert "REMOVED_SOURCE_CANARY" not in body
        assert "Maintainer-corrected public observation" in body and "Unsent observation B" in body
        assert candidate["entry"]["title"] == corrected["entry"]["title"]
        assert candidate["entry"]["summary"] == corrected["entry"]["summary"]

        index = next(file for file in second_batch["files"] if file["path"].endswith("/index.md"))
        assert index["base_sha256"] == hashlib.sha256(corrected["files"]["index.md"].encode()).hexdigest()
        assert all(not file.get("delete") for file in second_batch["files"])
        second = submit_batch(second_batch, settings, state_dir, transport=transport)
        assert second["status"] == "submitted", second
        mark(store, batch_id, second)
        # Main still lacks B while its PR is open. Sending C must keep B,
        # and bind the update to the confirmed PR head rather than old main.
        append_parts(store, "Unsent observation C also remains useful.\n")
        third_batch = build(store)[2]
        third_body = package_body(package_from_batch(third_batch))
        assert "Unsent observation B" in third_body and "Unsent observation C" in third_body
        assert "REMOVED_SOURCE_CANARY" not in third_body
        third = submit_batch(third_batch, settings, state_dir, transport=transport)
        assert third["status"] == "updated" and third["pr_url"] == second["pr_url"], third
        mark(store, batch_id, third)
        # A successful withdrawal overrides every stale local/PR candidate.
        amend_main(tmp_path, remote_url, name="withdrawal")
        assert feed.sync()["entries"] == 0
        with pytest.raises(ValueError, match="withdrawn"):
            commit_new_increment(store, "An attempted later capture.\n")
        assert build(store) is None
        assert store.query("REMOVED_SOURCE_CANARY")['results'] == []


def test_delayed_send_receipt_keeps_its_prepared_feed_identity(
        settings, state_dir, transport, remote_url, tmp_path):
    with closing(Store(tmp_path / "producer", "npu")) as store:
        feed = Feed(store, dict(repository=settings["repository"], domain="npu", ref="main", url=remote_url))
        append_parts(store, "REMOVED_SOURCE_CANARY: the old claim.\n")
        batch_id, _, initial, _, _ = build(store)
        first = submit_batch(initial, settings, state_dir, transport=transport)
        mark(store, batch_id, first)
        merge(transport, settings, first)
        feed.sync()
        old_feed = store._row(TASK)["published_revision"]
        append_parts(store, "New observation sent before main changed.\n")
        frozen = build(store)[2]
        second = submit_batch(frozen, settings, state_dir, transport=transport)
        assert second["status"] == "submitted"
        # The external send completed, but its local receipt is delayed until
        # after a maintainer publishes a different main package.
        amend_main(tmp_path, remote_url, corrected_package(tmp_path))
        feed.sync()
        new_feed = store._row(TASK)["published_revision"]
        assert new_feed != old_feed
        mark(store, batch_id, second)
        assert store.feed_get("sent-package:" + TASK)["source_published_revision"] == old_feed
        # Already-sent blocks absent from new main are not called unsent.
        # An actually later local block survives the same reorganization.
        append_parts(store, "Truly unsent observation after main changed.\n")
        store.rebase_draft_on_published(TASK)
        candidate = store.materials.get_document(TASK, store._row(TASK)["draft_revision"], "draft")
        assert "REMOVED_SOURCE_CANARY" not in candidate["content"]
        assert "Truly unsent observation" in candidate["content"]
        assert "New observation sent before main changed" not in candidate["content"]


@pytest.mark.parametrize("status", ["pending", "unknown", "failed", "needs_review"])
def test_changed_main_does_not_rebase_unconfirmed_frozen_candidate(
        settings, state_dir, transport, remote_url, tmp_path, status):
    with closing(Store(tmp_path / "producer", "npu")) as store:
        feed = Feed(store, dict(repository=settings["repository"], domain="npu", ref="main", url=remote_url))
        append_parts(store, "Original public claim.\n")
        batch_id, _, initial, _, _ = build(store)
        first = submit_batch(initial, settings, state_dir, transport=transport)
        mark(store, batch_id, first)
        merge(transport, settings, first)
        feed.sync()
        append_parts(store, "Pending local observation.\n")
        frozen = build(store)[2]
        if status != "pending":
            store.mark_batch(batch_id, status, attempted=True, detail="Synthetic send outcome")
        before = store._row(TASK)["draft_revision"]
        amend_main(tmp_path, remote_url, corrected_package(tmp_path))
        feed.sync()
        store.rebase_draft_on_published(TASK)
        assert load_batch_payload(store, store.batch(batch_id)) == frozen
        assert store.batch(batch_id)["status"] == status
        if status in {"pending", "unknown"}:
            assert store._row(TASK)["draft_revision"] == before
            assert build(store) is None
        else:
            assert store._row(TASK)["draft_revision"] != before
            assert build(store) is not None  # a known failure never becomes a permanent latch


def test_capture_between_freeze_and_outbox_insert_stays_unbatched(tmp_path, monkeypatch):
    from mindie_knowledge.loop import export
    with closing(Store(tmp_path / "producer", "npu")) as store:
        original = append_parts(store, "First prepared observation.\n")
        freeze = export.freeze_batch
        def capture_after_freeze(*args, **kwargs):
            result = freeze(*args, **kwargs)
            append_parts(store, "Concurrent observation after freezing.\n")
            return result
        with monkeypatch.context() as scoped:
            scoped.setattr(export, "freeze_batch", capture_after_freeze)
            batch_id, _, frozen, _, _ = build(store)
        row = store._row(TASK)
        assert row["batched_revision"] == original["revision"]
        assert row["draft_revision"] != original["revision"]
        assert "Concurrent observation" not in package_body(package_from_batch(frozen))
        store.mark_batch(batch_id, "submitted", pr_url="https://example.invalid/pr/1", head_sha="a" * 40)
        later = build(store)
        assert later is not None
        assert "Concurrent observation" in package_body(package_from_batch(later[2]))


@pytest.mark.parametrize("damage", ["missing", "mismatched"])
def test_confirmed_task_cannot_hide_missing_prepared_identity(tmp_path, damage):
    with closing(Store(tmp_path / "producer", "npu")) as store:
        append_parts(store, "Prepared identity requirement.\n")
        batch_id, _, _, _, _ = build(store)
        key = "prepared-package-bases:" + batch_id
        with store.db:
            if damage == "missing":
                store.db.execute("DELETE FROM feed_state WHERE key=?", (key,))
            else:
                store.feed_set(key, dict(batch_revision="0" * 64, source_revisions={}, entry_revisions={}))
        with pytest.raises(ValueError, match="exact prepared package/base identity"):
            store.mark_batch(batch_id, "submitted", pr_url="https://example.invalid/pr/1", head_sha="a" * 40)


def test_production_increment_starts_from_changed_confirmed_main(
        settings, state_dir, transport, remote_url, tmp_path):
    with closing(Store(tmp_path / "producer", "npu")) as store:
        feed = Feed(store, dict(repository=settings["repository"], domain="npu", ref="main", url=remote_url))
        append_parts(store, "REMOVED_SOURCE_CANARY: the old claim.\n")
        batch_id, _, initial, _, _ = build(store)
        first = submit_batch(initial, settings, state_dir, transport=transport)
        mark(store, batch_id, first)
        merge(transport, settings, first)
        feed.sync()
        append_parts(store, "Unsent before correction.\n")
        amend_main(tmp_path, remote_url, corrected_package(tmp_path))
        feed.sync()
        continued = commit_new_increment(store, "Fresh production capture after correction.\n")
        task = store.materials.get_document(TASK, continued["revision"], "draft")
        assert "REMOVED_SOURCE_CANARY" not in task["content"]
        assert "Unsent before correction" in task["content"]
        assert "Fresh production capture" in task["content"]
        assert task["title"] == "Maintainer-corrected navigation"


def test_pending_index_result_settles_before_rebase_preserves_remote_navigation(
        settings, state_dir, transport, remote_url, tmp_path):
    from mindie_knowledge.loop.transcript_capture import _settle_task
    with closing(Store(tmp_path / "producer", "npu")) as store:
        feed = Feed(store, dict(repository=settings["repository"], domain="npu", ref="main", url=remote_url))
        append_parts(store, "REMOVED_SOURCE_CANARY: the old claim.\n")
        batch_id, _, initial, _, _ = build(store)
        first = submit_batch(initial, settings, state_dir, transport=transport)
        mark(store, batch_id, first)
        merge(transport, settings, first)
        feed.sync()
        pending = commit_new_increment(store, "Unsent local observation before correction.\n")
        corrected = corrected_package(tmp_path)
        amend_main(tmp_path, remote_url, corrected)
        feed.sync()
        store.rebase_draft_on_published(TASK)
        assert store._row(TASK)["draft_revision"] == pending["revision"]
        assert build(store) is None
        # Apply a previously returned result without any second model call.
        task = store.materials.read_task(TASK, pending["revision"], "draft")
        local = task["blocks"][-1]
        with store._write_txn():
            store.apply_material_indexes(entry_id=TASK, generation="g1", indexes=[dict(
                block_id=local["block_id"], title="Local observation", summary="Unsent local evidence.")],
                navigation=dict(title="Stale model title", summary="The removed claim was successful."))
            _settle_task(store, TASK)
        candidate = package_from_batch(build(store)[2])
        assert "REMOVED_SOURCE_CANARY" not in package_body(candidate)
        assert "Unsent local observation" in package_body(candidate)
        assert candidate["entry"]["title"] == corrected["entry"]["title"]
        assert candidate["entry"]["summary"] == corrected["entry"]["summary"]
