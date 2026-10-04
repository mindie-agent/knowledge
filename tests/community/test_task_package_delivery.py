"""Orthogonal mechanisms reduced from the inspected K3 task shapes.

The source cases resumed after apparent completion, corrected early benchmark
claims, and located a middle-of-task prefix failure. These fixtures contain no
real transcript text. Git and the ReMe consumer are real; GitHub is FileTransport
here, so this suite does not claim public GitHub/Grok acceptance.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import closing
from types import SimpleNamespace

import pytest

from mindie_knowledge.community import reconcile_batch, submit_batch
from mindie_knowledge.community.common import CommunityError, Deadline, UnknownOutcome
from mindie_knowledge.community.batch import validate_batch
from mindie_knowledge.loop.export import build_batch
from mindie_knowledge.loop.feed import Feed
from mindie_knowledge.loop.store import Store, digest
from mindie_knowledge.materials.publication import load_batch_payload, staging_path
from .conftest import commit_tree_file, git

TASK = "b" * 64


def append_parts(store, *texts, navigation="Initial smoke passed; the full run remains unverified."):
    row = store._row(TASK)
    revision = (row["draft_revision"] or row["published_revision"]) if row else None
    blocks = [dict(block_id=digest(["public-k3-mechanism", text]), text=text,
                   title="Prefix replay and precision check", summary="A failed attempt and later correction.",
                   source_range={"case": "public-synthetic-reduction", "part": hashlib.sha256(text.encode()).hexdigest()})
              for text in texts]
    with store._write_txn():
        result = store.materials.append_batch(TASK, blocks, navigation, title="Long task continuation",
                                             revision=revision, source="draft", promote=False)
        store._bind_material_draft(result["entry"], generation="g1")
    return result["entry"]


def mark(store, batch_id, receipt):
    store.mark_batch(batch_id, receipt["status"], attempted=True, detail=receipt.get("detail", ""),
                     pr_url=receipt.get("pr_url"), head_sha=receipt.get("head_sha"),
                     actual_files=receipt.get("files"))


def build(store):
    return build_batch(store, settings=SimpleNamespace(generation="g1"))


def merge(transport, settings, receipt):
    return transport.merge_pull_request(settings["repository"], int(receipt["pr_url"].rsplit("/", 1)[1]),
                                        sha=receipt["head_sha"], method="squash", deadline=Deadline(120, 100))


def consumer(tmp_path, settings, remote_url):
    store = Store(tmp_path / "consumer", "npu")
    return store, Feed(store, dict(repository=settings["repository"], domain="npu", ref="main", url=remote_url))


def forbid_model(*_args, **_kwargs):
    raise AssertionError("public synchronization or retrieval must not invoke a model")


def test_complete_package_repeat_stop_and_resume(settings, state_dir, transport, remote_url, tmp_path, monkeypatch):
    from mindie_knowledge.materials import summarizer
    monkeypatch.setattr(summarizer, "summarize_batch", forbid_model)
    producer = Store(tmp_path / "producer", "npu")
    reader, feed = consumer(tmp_path, settings, remote_url)
    try:
        initial = append_parts(producer, "Beginning: isolated smoke ran.\n",
                               "MIDDLE_PREFIX_FAILURE_SENTINEL: a cache replay failed.\n",
                               "End: full checkpoint acceptance is still pending.\n")
        batch_id, _, batch, _, _ = build(producer)
        assert len(batch["files"]) == 4
        assert build(producer) is None  # repeated Stop with unchanged material
        descriptor = json.loads(producer.batch(batch_id)["batch"])
        assert all("content" not in file for file in descriptor["files"])
        assert "MIDDLE_PREFIX_FAILURE_SENTINEL" not in "\n".join(producer.db.iterdump())
        first = submit_batch(load_batch_payload(producer, producer.batch(batch_id)), settings,
                             state_dir, transport=transport)
        assert first["status"] == "submitted"
        mark(producer, batch_id, first)
        assert producer.compact_confirmed(batch_id)["staging"] == 1
        assert producer.materials.export_task(TASK)["revision"] == initial["revision"]  # open PR keeps candidate
        assert feed.sync()["entries"] == 0  # unmerged proposal is not the public source
        merge(transport, settings, first)
        assert feed.sync()["status"] == "synced"
        hit = reader.query("cache replay failed")["results"][0]
        assert "MIDDLE_PREFIX_FAILURE_SENTINEL" in reader.get(hit["ref"])["content"]
        old_ref = hit["ref"]
        revised = append_parts(producer, "Later correction: the earlier success claim was invalid.\n",
                               navigation="Correction: initial smoke did not establish full precision acceptance.")
        next_batch = build(producer)[2]
        second = submit_batch(next_batch, settings, state_dir, transport=transport)
        assert second["status"] == "submitted" and second["pr_url"] != first["pr_url"]
        mark(producer, batch_id, second)
        # Stable earlier blocks keep identical Git bytes; only navigation and one new block changed.
        cache = state_dir / "git" / settings["repository"].replace("/", "_")
        changed = git(["diff", "--name-only", first["head_sha"], second["head_sha"]], cwd=cache).splitlines()
        assert len(changed) == 2 and f"tasks/{TASK}/index.md" in changed
        merge(transport, settings, second)
        assert feed.sync()["status"] == "synced"
        with pytest.raises((ValueError, KeyError), match="unknown|expired"):
            reader.get(old_ref)
        latest = reader.get(reader.ref(TASK))
        assert latest["revision"] == revised["revision"]
        assert "Correction" in latest["summary"] and "cache replay failed" in latest["content"]
        assert feed.sync()["status"] == "unchanged"
    finally:
        producer.close()
        reader.close()


def test_unknown_create_reconciles_without_duplicate_write(settings, state_dir, transport, tmp_path, monkeypatch):
    store = Store(tmp_path / "producer", "npu")
    try:
        append_parts(store, "A task resumed after an interrupted operation.\n")
        batch_id, _, batch, _, _ = build(store)
        real_create = transport.create_pull_request
        calls = []
        def lose_response(*args, **kwargs):
            calls.append(True)
            real_create(*args, **kwargs)
            raise UnknownOutcome("response lost after the server accepted the PR")
        monkeypatch.setattr(transport, "create_pull_request", lose_response)
        unknown = submit_batch(batch, settings, state_dir, transport=transport)
        assert unknown["status"] == "unknown"
        mark(store, batch_id, unknown)
        assert build(store) is None
        resolved = reconcile_batch(batch_id, settings, state_dir, transport=transport)
        assert resolved["status"] == "submitted" and len(calls) == 1
        mark(store, batch_id, resolved)
        again = submit_batch(load_batch_payload(store, store.batch(batch_id)), settings,
                             state_dir, transport=transport)
        assert again["status"] == "unchanged" and len(calls) == 1
    finally:
        store.close()


def test_cleanup_failure_keeps_published_outcome_and_candidate(settings, state_dir, transport, tmp_path, monkeypatch):
    from mindie_knowledge.materials import publication
    store = Store(tmp_path / "producer", "npu")
    try:
        doc = append_parts(store, "Completed upload with a later local cleanup failure.\n")
        batch_id, revision, batch, _, _ = build(store)
        receipt = submit_batch(batch, settings, state_dir, transport=transport)
        mark(store, batch_id, receipt)
        real_remove = publication.shutil.rmtree
        monkeypatch.setattr(publication.shutil, "rmtree", lambda *_a, **_k: (_ for _ in ()).throw(PermissionError("synthetic cleanup refusal")))
        result = store.compact_confirmed(batch_id)
        assert result["cleanup_status"] == "failed"
        assert store.batch(batch_id)["status"] == "submitted"
        assert staging_path(store.root, batch_id, revision).exists()
        assert store.get(store.ref(TASK))["revision"] == doc["revision"]
        assert build(store) is None
        monkeypatch.setattr(publication.shutil, "rmtree", real_remove)
        assert store.compact_confirmed(batch_id)["staging"] == 1
    finally:
        store.close()


@pytest.mark.parametrize("status", ["failed", "needs_review", "rejected", "submitted", "updated", "unchanged"])
def test_replacement_retires_only_resolved_frozen_revision(tmp_path, status):
    store = Store(tmp_path / "producer", "npu")
    try:
        append_parts(store, "A previously resolved candidate.\n")
        batch_id, first, _, _, _ = build(store)
        store.mark_batch(batch_id, status, attempted=True, head_sha="a" * 40)
        if status == "rejected":
            store.create_draft(kind="experience", title="Other unquarantined task",
                               summary="New work after a rejected task", content="A separate new task.\n",
                               generation="g1")
        else:
            append_parts(store, "A separate new increment.\n")
        _, second, _, _, _ = build(store)
        assert first != second and not staging_path(store.root, batch_id, first).exists()
        assert staging_path(store.root, batch_id, second).exists()
        assert load_batch_payload(store, store.batch(batch_id))["revision"] == second
        assert store.batch(batch_id)["status"] == "pending"
        assert store.feed_get("publication-cleanup:" + batch_id)["status"] == "complete"
    finally:
        store.close()


@pytest.mark.parametrize("status", ["pending", "unknown", "unavailable"])
def test_uncertain_revision_never_replaced_or_cleaned(tmp_path, status):
    store = Store(tmp_path / "producer", "npu")
    try:
        append_parts(store, "A candidate with no known final receipt.\n")
        batch_id, revision, _, _, _ = build(store)
        store.mark_batch(batch_id, status, attempted=True)
        append_parts(store, "New text cannot replace uncertain frozen bytes.\n")
        assert build(store) is None
        assert staging_path(store.root, batch_id, revision).exists()
        assert store.batch(batch_id)["revision"] == revision
        assert store.feed_get("publication-cleanup:" + batch_id) is None
    finally:
        store.close()


@pytest.mark.parametrize("receipt_failure", [False, True])
def test_replacement_cleanup_failure_does_not_block_or_repeat_new_send(tmp_path, monkeypatch, receipt_failure):
    from mindie_knowledge.loop.engine import Engine
    from mindie_knowledge.materials import publication

    store = Store(tmp_path / "producer", "npu")
    try:
        append_parts(store, "An old failed candidate.\n")
        batch_id, first, _, _, _ = build(store)
        store.mark_batch(batch_id, "failed", attempted=True)
        append_parts(store, "A new candidate can be staged and sent once.\n")
        real_cleanup = publication.cleanup_staged_batch
        def refuse_old(root, ident, revision):
            if revision == first:
                raise PermissionError("old candidate cleanup refused")
            return real_cleanup(root, ident, revision)
        monkeypatch.setattr(publication, "cleanup_staged_batch", refuse_old)
        real_set = store.feed_set
        def fail_final_receipt(key, value):
            if receipt_failure and key == "publication-cleanup:" + batch_id and value["status"] == "failed":
                raise OSError("cleanup receipt write refused")
            return real_set(key, value)
        monkeypatch.setattr(store, "feed_set", fail_final_receipt)
        errors, sent = [], []
        engine = SimpleNamespace(store=store, _settings=lambda: SimpleNamespace(generation="g1", allows_capture=lambda: True),
                                 _error=errors.append, _submit=lambda row: sent.append(row["revision"]))
        Engine._flush(engine)
        assert len(sent) == 1 and sent[0] != first
        assert store.batch(batch_id)["status"] == "pending"
        assert load_batch_payload(store, store.batch(batch_id))["revision"] == sent[0]
        assert "old candidate cleanup refused" in errors[0]
        if receipt_failure:
            assert "cleanup receipt write refused" in errors[0]
        assert not any(attempt["status"] == "failed" for attempt in store.status()["export_attempts"])
        Engine._flush(engine)
        assert len(sent) == 1  # no new build or duplicate send after cleanup failure
        assert staging_path(store.root, batch_id, first).exists()
        # A later confirmed send retries the old exact revision as well; its
        # failure must not be overwritten by successful cleanup of the new one.
        monkeypatch.setattr(store, "feed_set", real_set)
        store.mark_batch(batch_id, "submitted", attempted=True, head_sha="a" * 40)
        assert store.compact_confirmed(batch_id)["cleanup_status"] == "failed"
        assert store.feed_get("publication-cleanup:" + batch_id)["pending_revisions"] == [first]
        monkeypatch.setattr(publication, "cleanup_staged_batch", real_cleanup)
        assert store.compact_confirmed(batch_id)["staging"] == 1
        assert not staging_path(store.root, batch_id, first).exists()
        assert store.feed_get("publication-cleanup:" + batch_id)["status"] == "complete"
    finally:
        store.close()


def test_invalid_package_keeps_current_then_valid_withdrawal_removes_it(settings, state_dir, transport, remote_url, tmp_path, monkeypatch):
    from mindie_knowledge.materials import summarizer
    monkeypatch.setattr(summarizer, "summarize_batch", forbid_model)
    producer = Store(tmp_path / "producer", "npu")
    reader, feed = consumer(tmp_path, settings, remote_url)
    try:
        append_parts(producer, "Middle task failure remains reference material.\n")
        _, _, batch, _, _ = build(producer)
        receipt = submit_batch(batch, settings, state_dir, transport=transport)
        merge(transport, settings, receipt)
        assert feed.sync()["status"] == "synced"
        pinned = reader.query("Middle task failure")["results"][0]["ref"]
        edit = tmp_path / "upstream-edit"
        git(["clone", "-q", remote_url, str(edit)])
        block = next((edit / "tasks" / TASK / "blocks").glob("*.md"))
        block.unlink()
        git(["add", "-A"], cwd=edit)
        git(["-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "invalid partial package"], cwd=edit)
        git(["push", "-q", "origin", "main"], cwd=edit)
        invalid = feed.sync()
        assert invalid["status"] == "invalid"
        assert "Middle task failure" in reader.get(pinned)["content"]
        (edit / "tasks" / TASK / "index.md").unlink()
        git(["add", "-A"], cwd=edit)
        git(["-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "withdraw task"], cwd=edit)
        git(["push", "-q", "origin", "main"], cwd=edit)
        assert feed.sync()["entries"] == 0
        assert reader.query("Middle task failure")["results"] == []
        with pytest.raises((ValueError, KeyError), match="unknown|expired"):
            reader.get(pinned)
    finally:
        producer.close()
        reader.close()


def test_exact_file_bytes_and_complete_index_required(tmp_path):
    store = Store(tmp_path / "producer", "npu")
    try:
        append_parts(store, "A complete public material block.\n")
        batch_id, revision, batch, _, _ = build(store)
        first_block = next(file for file in batch["files"] if "/blocks/" in file["path"])
        path = staging_path(store.root, batch_id, revision) / "files" / first_block["path"]
        path.write_text("tampered material", encoding="utf-8")
        with pytest.raises(ValueError, match="identity mismatch"):
            load_batch_payload(store, store.batch(batch_id))
        incomplete = dict(batch, files=[file for file in batch["files"] if file is not first_block])
        from mindie_knowledge.community.batch import batch_revision
        incomplete["revision"] = batch_revision(incomplete["files"], "npu", None, incomplete["entry_refs"])
        with pytest.raises(CommunityError, match="package"):
            validate_batch(incomplete)
    finally:
        store.close()


def test_pending_frozen_revision_survives_draft_continuation(settings, state_dir, transport, tmp_path):
    store = Store(tmp_path / "producer", "npu")
    try:
        old = append_parts(store, "Initial public batch before the interruption.\n")
        batch_id, _, _, _, _ = build(store)
        newer = append_parts(store, "Continuation arrived while the earlier send remained pending.\n")
        assert newer["revision"] != old["revision"] and build(store) is None
        frozen = load_batch_payload(store, store.batch(batch_id))
        assert old["revision"] in frozen["entry_refs"][0]
        assert all("Continuation arrived" not in (file.get("content") or "") for file in frozen["files"])
        first = submit_batch(frozen, settings, state_dir, transport=transport)
        mark(store, batch_id, first)
        store.compact_confirmed(batch_id)
        assert store.get(store.ref(TASK))["revision"] == newer["revision"]
        continued = build(store)[2]
        second = submit_batch(continued, settings, state_dir, transport=transport)
        assert second["status"] == "updated" and second["pr_url"] == first["pr_url"]
    finally:
        store.close()


def test_replacement_package_deletes_only_proven_superseded_blocks(settings, state_dir, transport, tmp_path):
    import subprocess
    from mindie_knowledge.loop import documents
    from mindie_knowledge.community.publish import _prepare_files
    store = Store(tmp_path / "producer", "npu")
    try:
        append_parts(store, "An obsolete fixture body needing an explicit replacement.\n")
        batch_id, _, initial, _, _ = build(store)
        first = submit_batch(initial, settings, state_dir, transport=transport)
        mark(store, batch_id, first)
        replacement = documents.make_entry(entry_id=TASK, domain="npu", kind="experience",
                                           title="Corrected task", summary="A source correction replaced the prior material.",
                                           content="Corrected public source material.")
        with store._write_txn():
            corrected = store.materials.put_document(replacement, source="draft")
            store._bind_material_draft(corrected, generation="g1")
        updated = build(store)[2]
        deleted = [file for file in updated["files"] if file.get("delete")]
        assert len(deleted) == 1
        # A remotely edited block cannot be removed using an obsolete digest.
        conflict_tree = tmp_path / "conflict"
        target = conflict_tree / deleted[0]["path"]
        target.parent.mkdir(parents=True)
        target.write_text("A later maintainer correction", encoding="utf-8")
        _, conflict = _prepare_files(dict(files=deleted), conflict_tree)
        assert "declared deletion base" in conflict and target.exists()
        second = submit_batch(updated, settings, state_dir, transport=transport)
        assert second["status"] == "updated"
        mark(store, batch_id, second)
        assert deleted[0]["path"] not in {file["path"] for file in store.sent_package_files(TASK)}
        cache = state_dir / "git" / settings["repository"].replace("/", "_")
        missing = subprocess.run(["git", "-C", str(cache), "cat-file", "-e", f"{second['head_sha']}:{deleted[0]['path']}"],
                                 capture_output=True)
        assert missing.returncode != 0
    finally:
        store.close()


def test_committed_feed_cleanup_error_is_separate_and_retried(settings, state_dir, transport, remote_url, tmp_path, monkeypatch):
    producer = Store(tmp_path / "producer", "npu")
    reader, feed = consumer(tmp_path, settings, remote_url)
    try:
        append_parts(producer, "A published replay observation.\n")
        receipt = submit_batch(build(producer)[2], settings, state_dir, transport=transport)
        merge(transport, settings, receipt)
        original = reader.materials._prune_task
        monkeypatch.setattr(reader.materials, "_prune_task", lambda *_a, **_k: (_ for _ in ()).throw(PermissionError("synthetic material cleanup refusal")))
        installed = feed.sync()
        assert installed["status"] == "synced" and installed["cleanup_status"] == "failed"
        assert reader.get(reader.ref(TASK))["content"] == "A published replay observation.\n"
        monkeypatch.setattr(reader.materials, "_prune_task", original)
        recovered = feed.sync()
        assert recovered["status"] == "unchanged" and "cleanup_status" not in recovered
    finally:
        producer.close()
        reader.close()


def test_installed_feed_is_reported_when_secondary_receipt_write_fails(settings, state_dir, transport, remote_url, tmp_path, monkeypatch):
    producer = Store(tmp_path / "producer", "npu")
    reader, feed = consumer(tmp_path, settings, remote_url)
    try:
        append_parts(producer, "The public file installation completed.\n")
        receipt = submit_batch(build(producer)[2], settings, state_dir, transport=transport)
        merge(transport, settings, receipt)
        original = reader.feed_set
        def fail_receipt(key, value):
            if key == "feed:" + feed.ident and value.get("status") == "synced":
                raise OSError("synthetic receipt disk failure")
            return original(key, value)
        monkeypatch.setattr(reader, "feed_set", fail_receipt)
        installed = feed.sync()
        assert installed["status"] == "synced" and installed["receipt_status"] == "failed"
        assert reader.get(reader.ref(TASK))["content"] == "The public file installation completed.\n"
    finally:
        producer.close()
        reader.close()


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_feed_rejects_unusual_task_path_and_preserves_primary_error(settings, state_dir, transport,
                                                                 remote_url, tmp_path, monkeypatch,
                                                                 cleanup_failure):
    from pathlib import Path
    producer = Store(tmp_path / "producer", "npu")
    reader, feed = consumer(tmp_path, settings, remote_url)
    try:
        original = append_parts(producer, "A valid previously published observation.\n")
        receipt = submit_batch(build(producer)[2], settings, state_dir, transport=transport)
        merge(transport, settings, receipt)
        assert feed.sync()["status"] == "synced"
        edit = tmp_path / "unusual-path-edit"
        git(["clone", "--bare", "-q", remote_url, str(edit)])
        commit = commit_tree_file(edit, "HEAD", f"tasks/{TASK}/blocks/unreferenced\nblock.md",
                                  b"Not referenced by the task index.\n")
        git(["push", "-q", "origin", f"{commit}:refs/heads/main"], cwd=edit)
        real_unlink = Path.unlink
        def refuse_listing(path, *args, **kwargs):
            if cleanup_failure and path == feed.dir / "listing.tmp":
                raise PermissionError("synthetic listing cleanup refusal")
            return real_unlink(path, *args, **kwargs)
        monkeypatch.setattr(Path, "unlink", refuse_listing)
        result = feed.sync()
        assert result["status"] == "invalid"
        assert "unsupported task package path" in result["detail"]
        if cleanup_failure:
            assert "synthetic listing cleanup refusal" in result["detail"]
        assert reader.get(reader.ref(TASK))["revision"] == original["revision"]
    finally:
        producer.close()
        reader.close()


@pytest.mark.parametrize("remote_change", ["unreferenced-block", "symlink-index", "crlf-index"])
def test_remote_package_changes_are_never_overwritten(settings, state_dir, transport,
                                                      remote_url, tmp_path, remote_change, monkeypatch):
    from mindie_knowledge.community import gitops
    from mindie_knowledge.community.ledger import Ledger
    store = Store(tmp_path / "producer", "npu")
    try:
        append_parts(store, "Original task evidence.\n")
        batch_id, _, initial, _, _ = build(store)
        first = submit_batch(initial, settings, state_dir, transport=transport)
        assert first["status"] == "submitted"
        mark(store, batch_id, first)
        branch = "mindie-contrib/npu/" + batch_id
        work = tmp_path / "maintainer"
        git(["clone", "--quiet", "-b", branch, remote_url, str(work)])
        index = work / "tasks" / TASK / "index.md"
        if remote_change == "unreferenced-block":
            # A retained file outside the index is not ours to silently delete.
            commit = commit_tree_file(work, "HEAD", f"tasks/{TASK}/blocks/foreign\nblock.md",
                                      b"Independent maintainer material.\n")
        elif remote_change == "symlink-index":
            commit = commit_tree_file(work, "HEAD", f"tasks/{TASK}/index.md",
                                      b"../../README.md", mode="120000")
        else:
            commit = commit_tree_file(work, "HEAD", f"tasks/{TASK}/index.md",
                                      index.read_bytes().replace(b"\n", b"\r\n"))
        git(["update-ref", "HEAD", commit], cwd=work)
        git(["push", "origin", "HEAD"], cwd=work)
        git(["push", "origin", "HEAD:refs/pull/1/head"], cwd=work)
        before = git(["rev-parse", "HEAD"], cwd=work)
        append_parts(store, "Later local evidence awaiting conflict review.\n")
        next_batch = build(store)[2]
        def no_write(*args, **kwargs):
            pytest.fail("remote package conflict reached a write or unsupported checkout")
        for operation in ("apply_files", "stage_and_commit", "push_branch"):
            monkeypatch.setattr(gitops, operation, no_write)
        monkeypatch.setattr(transport, "update_pull_request", no_write)
        if remote_change != "crlf-index":
            monkeypatch.setattr(gitops, "checkout_new", no_write)
            monkeypatch.setattr(gitops, "checkout_existing", no_write)
        refused = submit_batch(next_batch, settings, state_dir, transport=transport)
        assert refused["status"] == "needs_review", json.dumps(refused, sort_keys=True)
        with closing(Ledger(state_dir)) as ledger:
            row = ledger.get_publication(batch_id, next_batch["revision"])
            assert row["status"] == "needs_review"
            steps = {step["step"] for step in ledger.steps_for(batch_id, next_batch["revision"])}
            assert not steps.intersection({"git:apply-files", "git:commit", "git:push", "github:update-pr"})
        assert git(["ls-remote", remote_url, f"refs/heads/{branch}"]).split()[0] == before
        assert len(transport.list_open_pull_requests(settings["repository"], deadline=Deadline(120, 60))) == 1
    finally:
        store.close()
