"""Unsent-observation lifecycle: pagination, rebase onto moved remotes,
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


def test_explain_paginates_long_bodies_with_explicit_continuation(tmp_path):
    store = Store(tmp_path, "vllm-ascend")
    try:
        body = "x" * (store.EXPLAIN_PAGE_CHARS * 3)
        doc = store.create_draft(kind="experience", title="Long case",
                                 summary="s", content=body, owner=PRODUCER)
        page = store.explain(store.ref(doc["entry_id"]))
        assert len(page["content"]) == store.EXPLAIN_PAGE_CHARS
        assert page["content_offset"] == 0
        assert page["content_length"] == len(body)
        assert page["next_offset"] == store.EXPLAIN_PAGE_CHARS
        rest = store.explain(store.ref(doc["entry_id"]),
                             offset=page["next_offset"],
                             limit=store.EXPLAIN_MAX_LIMIT)
        assert len(rest["content"]) == len(body) - store.EXPLAIN_PAGE_CHARS
        assert rest["next_offset"] is None
        short = store.create_draft(kind="experience", title="Short case",
                                   summary="s", content="short body",
                                   owner=PRODUCER)
        whole = store.explain(store.ref(short["entry_id"]))
        assert whole["content"] == "short body" and whole["next_offset"] is None
        with pytest.raises(ValueError, match="limit"):
            store.explain(store.ref(short["entry_id"]),
                          limit=store.EXPLAIN_MAX_LIMIT + 1)
    finally:
        store.close()


def test_feed_sync_preserves_unsent_candidate_without_merging_remote_corrections(tmp_path):
    """Current public material wins retrieval; local work remains a separate
    candidate. Changed upstream bytes are reconciled by exact-base conflict,
    never by pretending two independently indexed packages were merged.
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
        assert store.rebase_draft_on_published(doc["entry_id"])["revision"] == updated["revision"]
        built2 = build_batch(store, settings=settings)
        item = next(f for f in built2[2]["files"] if f["path"].endswith("/index.md"))
        assert item["base_sha256"] == store.sent_receipt(doc["entry_id"])["sha256"]
        assert item["base_sha256"] != __import__("hashlib").sha256(package_for(published)["files"]["index.md"].encode()).hexdigest()
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
        costs = [len(canonical(export_mod._task_files(store, doc)).encode()) for doc in docs]
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
        store.record_vote(root_hash="9" * 64, ref=store.ref(doc["entry_id"]),
                          rating="down", reason="token: ghp_" + "A" * 30,
                          publishable=True, generation="gen-1")
    # A local-only vote with the same text stays private and is never scanned.
    local = store.record_vote(root_hash="9" * 64, ref=store.ref(doc["entry_id"]),
                              rating="down", reason="token: ghp_" + "A" * 30,
                              publishable=False)
    assert local["publishable"] is False
    assert store.unbatched_votes(generation="gen-1") == []
