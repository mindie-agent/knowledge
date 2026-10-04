"""Current file packages and frozen contributions across resumed work.

The continuation-after-apparent-completion scenario is orthogonalized from
the long K3 task cases; no historical transcript or business value is copied.
"""
import json
from contextlib import closing

import pytest

from conftest import write_settings
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.export import build_batch
from mindie_knowledge.loop.store import Store, digest
from mindie_knowledge.materials.publication import load_batch_payload


def draft(store, generation="allowed"):
    return store.create_draft(kind="experience", title="Latest case", summary="Public result",
                              content="First observation", generation=generation)


def test_many_appends_and_header_updates_keep_one_body_and_grant(store):
    doc = draft(store)
    for i in range(32):
        doc, _ = store.append_observation(doc["entry_id"], f"Observation {i}",
                                          marker=f"{i:032x}", generation="allowed")
    assert store.update_draft_header(doc["entry_id"], expected_body=digest(doc["content"]),
                                    title="Updated title", summary="Current result",
                                    generation="allowed")
    assert store.db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 1
    assert store.db.execute("SELECT count(*) FROM grants WHERE kind='draft'").fetchone()[0] == 1
    current = store.drafts_changed(generation="allowed")[0]
    assert all(f"Observation {i}" in current["content"] for i in range(32))
    assert current["title"] == "Updated title"


def test_pending_send_owns_old_payload_after_draft_advances(tmp_path):
    path = tmp_path / "community.json"
    settings = write_settings(path, roots=[tmp_path])
    with closing(Store(tmp_path / "store", "test")) as store:
        old = draft(store, settings.generation)
        batch_id, *_ = build_batch(store, settings=settings)
        pending = store.batch(batch_id)
        descriptor = json.loads(pending["batch"])
        assert all("content" not in item for item in descriptor["files"])
        frozen = load_batch_payload(store, pending)
        sent_files = frozen["files"]
        new, _ = store.append_observation(old["entry_id"], "Later unsent observation",
                                          marker="a" * 32, generation=settings.generation)
        assert store._revision_doc(old["entry_id"], old["revision"]) is None
        sent = []
        engine = Engine(store, settings_path=path)
        engine.community = {"submit_batch": lambda batch, *a, **kw: (
            sent.append(batch) or {"status": "submitted", "head_sha": "a" * 40,
                                   "pr_url": "https://example.invalid/pull/1"})}
        engine._submit(pending)
        assert len(sent) == 1 and sent[0]["files"] == sent_files
        assert all("Later unsent observation" not in item["content"] for item in sent_files)
        assert store.drafts_changed(generation=settings.generation) == [new]
        assert "content" not in json.loads(store.batch(batch_id)["batch"])["files"][0]


def test_restart_keeps_current_packages_and_does_not_open_old_database(tmp_path):
    root = tmp_path / "store"
    previous = root / "test" / "store-v3.sqlite3"
    previous.parent.mkdir(parents=True)
    previous.write_bytes(b"An inert previous database; never imported by the new file authority.")
    old_bytes = previous.read_bytes()
    with closing(Store(tmp_path / "author", "test")) as author:
        published = author.create_draft(kind="experience", title="Published correction",
                                       summary="The early result was corrected.",
                                       content="Public correction retained for independent readers.",
                                       entry_id="b" * 64)
        package = author.materials.export_task(published["entry_id"])
    with closing(Store(root, "test")) as store:
        old = draft(store)
        new, _ = store.append_observation(old["entry_id"], "Latest", marker="a" * 32,
                                          generation="allowed")
        store.install_feed([package], feed_ident="f" * 64, source_revision="c" * 40)
    with closing(Store(root, "test")) as store:
        assert store._revision_doc(old["entry_id"], old["revision"]) is None
        assert store._revision_doc(new["entry_id"], new["revision"]) == new
        assert store.get(store.ref(published["entry_id"]))["content"] == published["content"]
        assert not store.granted("draft", old["entry_id"], old["revision"], "allowed")
        for table in ("entries", "revisions"):
            assert all(not json.loads(row[0]).get("content")
                       for row in store.db.execute(f"SELECT doc FROM {table}"))
        assert previous.read_bytes() == old_bytes


def test_failed_append_rolls_back_latest_body_and_retention(store, monkeypatch):
    old = draft(store)
    prune = store._prune_body_history
    def fail(entry_id):
        prune(entry_id)
        raise OSError("synthetic transaction failure")
    monkeypatch.setattr(store, "_prune_body_history", fail)
    with pytest.raises(OSError):
        store.append_observation(old["entry_id"], "Not committed", marker="c" * 32,
                                 generation="allowed")
    assert store.drafts_changed(generation="allowed") == [old]
    assert store.db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 1
