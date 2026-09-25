"""Unsent-observation lifecycle: pagination, rebase onto moved remotes,
flush chunking and per-vote admission scanning. Real SQLite, no network."""

import json

import pytest

from mindie_knowledge.loop import documents, settings as settings_mod
from mindie_knowledge.loop.export import build_batch
from mindie_knowledge.loop.store import Store

from conftest import write_settings

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


def test_feed_sync_reseats_draft_onto_moved_remote_keeping_unsent(tmp_path):
    """A bot/maintainer edit that lands on main and syncs down re-seats the
    local draft: the published body wins, the bot-removed submitted paragraph
    is dropped, and only the still-unsent observation survives locally."""
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
        store.install_feed([published], feed_ident="f" * 64)
        row = store._row(doc["entry_id"])
        draft = store._revision_doc(doc["entry_id"], row["draft_revision"])
        assert draft["title"] == "Edited title"  # remote header authoritative
        assert "Kept paragraph." in draft["content"]
        assert "Removed paragraph." not in draft["content"]
        assert "Unsent observation B." in draft["content"]
        # The next batch publishes exactly the published body plus the unsent
        # observation — the removed paragraph is not resurrected.
        built2 = build_batch(store, settings=settings)
        item = next(f for f in built2[2]["files"] if f["path"].startswith("cases/"))
        assert "Removed paragraph." not in item["content"]
        assert "Unsent observation B." in item["content"]
        assert "Edited title" in item["content"]
        assert item["base_sha256"] == store.sent_receipt(doc["entry_id"])["sha256"]
    finally:
        store.close()


def test_feed_sync_drops_fully_sent_draft_after_remote_caught_up(tmp_path):
    """A submitted observation the bot removed upstream is not kept locally
    either: with nothing unsent left, the local copy converges to the
    published body instead of resurrecting the removed block."""
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
        assert store.sent_markers(doc["entry_id"]) == {"ee" * 16}
        # Upstream removed the submitted observation before merging.
        published = documents.make_entry(
            entry_id=doc["entry_id"], domain="test", kind="experience",
            title="Sent case", summary="s", content="Base body.",
        )
        store.install_feed([published], feed_ident="f" * 64)
        row = store._row(doc["entry_id"])
        assert row["draft_revision"] is None
        current = store.get(store.ref(doc["entry_id"]))
        assert current["content"] == "Base body."
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
        # Serialized accounting: two ~492-byte entry files plus framing fit
        # under 1600 bytes; the third waits for the next automatic batch.
        monkeypatch.setattr(export_mod, "MAX_BATCH_BYTES", 1600)
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


def test_serialized_size_accounting_is_exact(tmp_path):
    """The planner's proforma measure equals the real canonical batch bytes."""
    from mindie_knowledge.loop.export import (
        _batch_serialized_size, _canonical_len, lineage_of,
    )

    store = Store(tmp_path, "test")
    try:
        settings = write_settings(tmp_path / "community.json", enabled=True,
                                  roots=[tmp_path])
        doc = store.create_draft(kind="experience", title="Measure",
                                 summary="s", content="Body with \"escapes\" ü",
                                 owner=PRODUCER, generation=settings.generation)
        from mindie_knowledge.loop.export import _filename
        from mindie_knowledge.loop.documents import render_entry

        file_dict = dict(path=_filename(doc), content=render_entry(doc),
                         sha256="0" * 64, base_sha256=None, sent_markers=["ab" * 16])
        ref = store.ref(doc["entry_id"], doc["revision"])
        lineage = lineage_of("test", settings.generation)
        summary = "test: 1 entries, 0 votes"
        measured = _batch_serialized_size(
            [_canonical_len(file_dict)], [_canonical_len(ref)],
            summary=summary, domain="test", lineage=lineage,
        )
        actual = dict(schema="mindie-contribution/1", batch_id=lineage,
                      revision="0" * 64, domain="test", base_commit="0" * 64,
                      entry_refs=[ref], files=[file_dict], summary=summary)
        from mindie_knowledge.loop.store import canonical

        assert measured == len(canonical(actual).encode("utf-8"))
    finally:
        store.close()


def test_install_feed_membership_switch_scales_past_sql_variable_ceiling(tmp_path):
    """33k entries in one atomic switch: no giant NOT IN variable list."""
    store = Store(tmp_path, "test")
    try:
        docs = [
            documents.make_entry(entry_id=f"{i:05x}" + "0" * 59, domain="test",
                                 kind="experience", title=f"Entry {i}",
                                 summary="s", content=f"body {i}")
            for i in range(33_000)
        ]
        store.install_feed(iter(docs), feed_ident="f" * 64)
        assert store.query("32999")["results"]
        store.install_feed(docs[:100], feed_ident="f" * 64)
        assert store.get(store.ref(docs[2000]["entry_id"]))["withdrawn"] is True
        assert store.query("50")["results"]
        assert store.query("32999")["results"] == []
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
