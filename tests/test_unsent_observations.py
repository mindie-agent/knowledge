"""Unsent-observation lifecycle: reconciliation with moved remotes,
flush chunking and per-vote admission scanning. Real SQLite, no network."""

import json

import pytest

from mindie_knowledge.loop import documents, settings as settings_mod
from mindie_knowledge.loop.export import build_batch
from mindie_knowledge.loop.store import Store

from conftest import write_settings
from package_fixture import install_documents, package_for

PRODUCER = "b" * 64


def _sent_entry(store, settings, *, content="the sent body", title="Sent case"):
    """A draft confirmed as sent (submitted + head receipt), uncompacted."""
    doc = store.create_draft(kind="experience", title=title, summary="s",
                             content=content, owner=PRODUCER,
                             generation=settings.generation)
    built = build_batch(store, settings=settings)
    store.mark_batch(built[0], "submitted", pr_url="https://x/pr/1",
                     head_sha="a" * 40)
    return doc, built


def test_rewritten_whole_document_cannot_guess_unsent_parts_after_remote_correction(tmp_path):
    """A whole-document rewrite lacks stable sent blocks and cannot be safely
    separated into old removed text and new observations.
    """
    settings = write_settings(tmp_path / "community.json", enabled=True,
                              roots=[tmp_path])
    store = Store(tmp_path / "store", "test")
    try:
        doc, built = _sent_entry(store, settings,
                                 content="Kept paragraph.\n\nRemoved paragraph.")
        updated, appended = store.append_observation(
            doc["entry_id"], "Unsent observation B.", marker="dd" * 16,
            producer=PRODUCER, generation=settings.generation,
        )
        assert appended
        # The remote moved: bot removed a paragraph and rewrote the title,
        # then it merged and synced as the published body.
        published = documents.make_entry(
            entry_id=doc["entry_id"], domain="test", kind="experience",
            title="Edited title", summary="edited summary",
            content="Kept paragraph.",
        )
        install_documents(store, [published], feed_ident="f" * 64)
        row = store._row(doc["entry_id"])
        draft = store._revision_doc(doc["entry_id"], row["draft_revision"])
        assert draft["title"] == doc["title"]
        assert "Removed paragraph." in draft["content"]
        assert "Unsent observation B." in draft["content"]
        visible = store.get(store.ref(doc["entry_id"]))
        assert visible["title"] == "Edited title" and visible["content"] == "Kept paragraph."
        with pytest.raises(ValueError, match="not an append-only extension"):
            store.rebase_draft_on_published(doc["entry_id"])
        with pytest.raises(ValueError, match="not an append-only extension"):
            build_batch(store, settings=settings)
        assert store._row(doc["entry_id"])["draft_revision"] == updated["revision"]
    finally:
        store.close()


def test_feed_sync_drops_only_a_byte_identical_published_candidate(tmp_path):
    """An exact main package retires its matching candidate without body history."""
    settings = write_settings(tmp_path / "community.json", enabled=True,
                              roots=[tmp_path])
    store = Store(tmp_path / "store", "test")
    try:
        doc, built = _sent_entry(store, settings, content="Base body.")
        # Observation A was sent and confirmed in a second batch.
        store.append_observation(doc["entry_id"], "Observation A.",
                                 marker="ee" * 16, producer=PRODUCER,
                                 generation=settings.generation)
        built2 = build_batch(store, settings=settings)
        store.mark_batch(built2[0], "submitted", pr_url="https://x/pr/2",
                         head_sha="b" * 40)
        published = store.get(store.ref(doc["entry_id"]))
        install_documents(store, [published], feed_ident="f" * 64)
        row = store._row(doc["entry_id"])
        assert row["draft_revision"] is None
        current = store.get(store.ref(doc["entry_id"]))
        assert "Base body." in current["content"] and "Observation A." in current["content"]
        assert store.drafts_changed(generation=settings.generation) == []
    finally:
        store.close()


def test_flush_chunks_oversized_flush_without_dropping(tmp_path, monkeypatch):
    """A flush bigger than the per-batch envelope splits into sequential
    automatic batches; nothing is dropped and no batch is user-managed."""
    settings = write_settings(tmp_path / "community.json", enabled=True,
                              roots=[tmp_path])
    store = Store(tmp_path / "store", "test")
    try:
        from mindie_knowledge.loop import export as export_mod

        docs = [
            store.create_draft(kind="experience", title=f"Case {i}",
                               summary="s", content=f"Body {i}." + "x" * 100,
                               owner=PRODUCER, generation=settings.generation)
            for i in range(3)
        ]
        from mindie_knowledge.loop.store import canonical
        costs = [len(canonical([dict(item) for item in export_mod._task_files(store, doc)]).encode()) for doc in docs]
        monkeypatch.setattr(export_mod, "MAX_BATCH_BYTES", sum(sorted(costs)[-2:]))
        first = build_batch(store, settings=settings)
        assert first is not None
        first_ids = first[3]
        assert len(first_ids) == 2  # exact serialized accounting, deterministic
        store.mark_batch(first[0], "submitted", pr_url="https://x/pr/1",
                         head_sha="b" * 40)
        second = build_batch(store, settings=settings)
        assert second is not None and len(second[3]) == 1
        store.mark_batch(second[0], "submitted", pr_url="https://x/pr/2",
                         head_sha="c" * 40)
        assert sorted(first[3] + second[3]) == sorted(d["entry_id"] for d in docs)
        assert build_batch(store, settings=settings) is None
    finally:
        store.close()


def test_frozen_package_preserves_exact_utf8_bytes_and_metadata_only_receipt(tmp_path):
    from mindie_knowledge.community.batch import batch_revision
    from mindie_knowledge.materials.publication import load_batch_payload
    import hashlib
    store = Store(tmp_path, "test")
    try:
        settings = write_settings(tmp_path / "community.json", enabled=True, roots=[tmp_path])
        store.create_draft(kind="experience", title="Measure", summary="s",
                          content='Body with "escapes" ü and a newline.\nNext line.',
                          owner=PRODUCER, generation=settings.generation)
        built = build_batch(store, settings=settings)
        payload = load_batch_payload(store, store.batch(built[0]))
        assert payload == built[2]
        assert payload["revision"] == batch_revision(payload["files"], "test", payload["base_commit"], payload["entry_refs"])
        for item in payload["files"]:
            assert hashlib.sha256(item["content"].encode("utf-8")).hexdigest() == item["sha256"]
        stored = json.loads(store.batch(built[0])["batch"])
        assert all("content" not in item for item in stored["files"])
        assert 'Body with "escapes"' not in "\n".join(store.db.iterdump())
    finally:
        store.close()


def test_install_feed_membership_switch_scales_past_sql_variable_ceiling(tmp_path):
    """A real switch exceeds the lowered SQLite variable ceiling without a giant IN list."""
    store = Store(tmp_path, "test")
    try:
        import sqlite3
        store.db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 64)
        docs = [
            documents.make_entry(entry_id=f"{i:05x}" + "0" * 59, domain="test",
                                 kind="experience", title=f"Entry {i}",
                                 summary="s", content=f"body {i}")
            for i in range(130)
        ]
        install_documents(store, iter(docs), feed_ident="f" * 64)
        assert store.query("129")["results"]
        install_documents(store, docs[:100], feed_ident="f" * 64)
        assert store.get(store.ref(docs[120]["entry_id"]))["withdrawn"] is True
        assert store.query("50")["results"]
        assert store.query("129")["results"] == []
    finally:
        store.close()


def test_publishable_vote_reason_is_scanned_at_admission(store):
    doc = store.create_draft(kind="experience", title="Voted", summary="s",
                             content="body", owner=PRODUCER)
    with pytest.raises(ValueError, match="privacy scan"):
        store.record_vote(root_hash="9" * 64, ref=store.ref(doc["entry_id"], doc["revision"]),
                          rating="down", reason="token: ghp_" + "A" * 30,
                          publishable=True, generation="gen-1")
    # A local-only vote with the same text stays private and is never scanned.
    local = store.record_vote(root_hash="9" * 64, ref=store.ref(doc["entry_id"], doc["revision"]),
                              rating="down", reason="token: ghp_" + "A" * 30,
                              publishable=False)
    assert local["publishable"] is False
    assert store.unbatched_votes(generation="gen-1") == []
