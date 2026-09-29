"""Latest-only drafts: bounded retention, pending sends and atomic updates."""
import json
from contextlib import closing

import pytest

from conftest import write_settings
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.export import build_batch
from mindie_knowledge.loop.store import Store, digest


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
        sent_body = json.loads(pending["batch"])["files"][0]["content"]
        new, _ = store.append_observation(old["entry_id"], "Later unsent observation",
                                          marker="a" * 32, generation=settings.generation)
        assert store._revision_doc(old["entry_id"], old["revision"]) is None
        sent = []
        engine = Engine(store, settings_path=path)
        engine.community = {"submit_batch": lambda batch, *a, **kw: (
            sent.append(batch) or {"status": "submitted", "head_sha": "a" * 40,
                                   "pr_url": "https://example.invalid/pull/1"})}
        engine._submit(pending)
        assert len(sent) == 1 and sent[0]["files"][0]["content"] == sent_body
        assert "Later unsent observation" not in sent_body
        assert store.drafts_changed(generation=settings.generation) == [new]
        assert "content" not in json.loads(store.batch(batch_id)["batch"])["files"][0]


def test_existing_store_discards_old_drafts_once_and_keeps_published_cache(tmp_path):
    root = tmp_path / "store"
    with closing(Store(root, "test")) as store:
        old = draft(store)
        new, _ = store.append_observation(old["entry_id"], "Latest", marker="a" * 32,
                                          generation="allowed")
        published = dict(old, entry_id="b" * 64)
        from mindie_knowledge.loop.documents import revision_of
        published["revision"] = revision_of(published)
        store.install_feed([published], feed_ident="f" * 64)
        with store._write_txn():
            store._insert_revision(old, "draft", 0)
            store.grant("draft", old["entry_id"], old["revision"], "allowed")
            store.db.execute("DELETE FROM meta WHERE key='latest-draft-only'")
    with closing(Store(root, "test")) as store:
        assert store._revision_doc(old["entry_id"], old["revision"]) is None
        assert store._revision_doc(new["entry_id"], new["revision"]) == new
        assert store.get(store.ref(published["entry_id"]))["content"] == published["content"]
        assert not store.granted("draft", old["entry_id"], old["revision"], "allowed")


def test_failed_append_rolls_back_latest_body_and_retention(store, monkeypatch):
    old = draft(store)
    prune = store._prune_draft_history
    def fail(entry_id):
        prune(entry_id)
        raise OSError("synthetic transaction failure")
    monkeypatch.setattr(store, "_prune_draft_history", fail)
    with pytest.raises(OSError):
        store.append_observation(old["entry_id"], "Not committed", marker="c" * 32,
                                 generation="allowed")
    assert store.drafts_changed(generation="allowed") == [old]
    assert store.db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 1
