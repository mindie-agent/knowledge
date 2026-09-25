"""Derived FTS5 search index: parity with the lexical path, readiness,
resumable backfill, and write-path consistency. Real SQLite, no network."""

import json
import sqlite3
import threading
import time

import pytest

from mindie_knowledge.loop import settings as settings_mod
from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.documents import make_entry, render_entry
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import IndexNotReady, Store
from mindie_knowledge.loop.transport import Service, rpc
from mindie_knowledge.markdown import Document
from mindie_knowledge.retrieval import lexical_search_streaming

from conftest import make_admission, write_settings

PRODUCER = "a" * 64


def _legacy_hits(store, query, limit=5, conditions=None):
    """The pre-index lexical path over exactly the visible corpus."""
    docs = []
    for row in store.db.execute(
        "SELECT * FROM entries WHERE feed_active=1 OR "
        "(draft_revision IS NOT NULL AND published_revision IS NULL)"
    ):
        doc = json.loads(row["doc"])
        if (
            doc["kind"] == "knowledge"
            and conditions
            and any(
                key in doc["conditions"] and doc["conditions"][key] != value
                for key, value in conditions.items()
            )
        ):
            continue
        docs.append(Document(
            layer=doc["kind"], title=doc["title"],
            content=doc["summary"] + "\n" + doc["content"],
            slug=doc["entry_id"], path=None, uri=doc["entry_id"],
        ))
    return lexical_search_streaming(query, lambda: iter(docs), limit=len(docs) or 1)


def _corpus(store):
    store.create_draft(kind="experience", title="RMSNorm operator check",
                       summary="torch_npu.npu_rms_norm fails on 2.10.0.",
                       content="torch_npu.npu_rms_norm(x, w, epsilon=1e-6) "
                               "breaks on torch==2.10.0+cpu; 显存泄漏 observed.",
                       owner=PRODUCER)
    store.create_draft(kind="experience", title="Device mapping",
                       summary="container device order",
                       content="physical id is not the logical id", owner=PRODUCER)
    store.create_draft(kind="knowledge", title="Graph capture notes",
                       summary="ACL graph capture conditions",
                       content="capture behavior on graph mode; 显存 pressure",
                       conditions={"torch_npu_version": "2.10.0.post2"},
                       owner=PRODUCER)
    store.create_draft(kind="knowledge", title="Graph capture older",
                       summary="ACL graph capture conditions old",
                       content="capture behavior on older stack",
                       conditions={"torch_npu_version": "2.9"},
                       owner=PRODUCER)


def test_index_matches_lexical_recall_and_ranking(tmp_path):
    store = Store(tmp_path, "test")
    try:
        _corpus(store)
        for query in ("torch_npu.npu_rms_norm", "rms_norm", "显存", "泄漏",
                      "2.10.0+cpu", "device mapping", "capture"):
            legacy = _legacy_hits(store, query)
            fresh = store.query(query, limit=5)["results"]
            legacy_ids = [hit.uri for hit in legacy]
            fresh_ids = [store._parse_ref(item["ref"])[0] for item in fresh]
            assert fresh_ids == legacy_ids[:len(fresh_ids)], query
            assert fresh and legacy
            assert fresh_ids[0] == legacy_ids[0]  # same top hit
        # scores remain ordering signals, not the old numeric scale
        scores = [r["score"] for r in store.query("capture")["results"]]
        assert scores == sorted(scores, reverse=True)
    finally:
        store.close()


def test_conditions_filter_and_overlay_survive_indexing(tmp_path):
    store = Store(tmp_path, "test")
    try:
        _corpus(store)
        hits = store.query("capture", conditions={"torch_npu_version": "2.9"},
                           limit=5)["results"]
        titles = [h["title"] for h in hits]
        assert "Graph capture older" in titles
        assert "Graph capture notes" not in titles
        # Draft overlay on a published entry: published body wins, labeled.
        row = store.db.execute(
            "SELECT entry_id FROM entries WHERE title='Device mapping'"
        ).fetchone()
        entry_id = row[0]
        doc = store.get(store.ref(entry_id))
        store.install_feed([doc], feed_ident="f" * 64)
        store.append_observation(entry_id, "later private note", marker="ab" * 16,
                                 producer=PRODUCER)
        hits = store.query("device mapping")["results"]
        assert hits[0]["supplemental"] is True
        assert store.get(hits[0]["ref"])["content"] == doc["content"]
    finally:
        store.close()


def test_cold_store_readiness_and_resumable_backfill(tmp_path):
    store = Store(tmp_path, "test")
    _corpus(store)
    for i in range(100):
        store.create_draft(kind="experience", title=f"Cold case {i}",
                           summary="s", content=f"cold body {i} rms_norm",
                           owner=PRODUCER)
    # Simulate a store whose derived index was never built (pre-index store).
    store._search_reset()
    store.close()
    store = Store(tmp_path, "test")
    try:
        # The inline slice is gone by design: a query never builds or
        # tokenizes content; it honestly rejects until the worker advances.
        with pytest.raises(IndexNotReady, match="rebuilt"):
            store.query("rms_norm")
        status = store.search_index_status()
        assert status["complete"] is False and status["indexed"] == 0
        # One bounded slice runs, then interruption: resume continues.
        assert store.advance_search_index(max_entries=2) is False
        store.close()
        store = Store(tmp_path, "test")
        assert store.search_index_status()["complete"] is False
        assert store.advance_search_index(max_entries=2) is False
        while not store.advance_search_index(max_entries=256):
            pass
        assert store.search_index_status()["complete"] is True
        assert store.query("rms_norm")["results"]
        assert store.query("cold body 99")["results"]
    finally:
        store.close()


def test_hooks_keep_index_current_and_compaction_drops_it(tmp_path):
    settings = write_settings(tmp_path / "community.json", enabled=True,
                              roots=[tmp_path])
    store = Store(tmp_path / "store", "test")
    try:
        from mindie_knowledge.loop.export import build_batch

        doc = store.create_draft(kind="experience", title="Hook case",
                                 summary="hook summary", content="hook body",
                                 owner=PRODUCER, generation=settings.generation)
        assert store.query("hook body")["results"]
        store.append_observation(doc["entry_id"], "hook later paragraph",
                                 marker="cd" * 16, producer=PRODUCER,
                                 generation=settings.generation)
        assert "hook later" in store.get(store.ref(doc["entry_id"]))["content"]
        assert store.query("hook later")["results"]  # append re-indexed
        built = build_batch(store, settings=settings)
        store.mark_batch(built[0], "submitted", pr_url="https://x/pr/1",
                         head_sha="a" * 40)
        store.compact_confirmed(built[0])
        # A sent, compacted, never-published entry leaves ordinary search.
        assert store.query("hook body")["results"] == []
        # ...and a cleanup must never leave a searchable ghost behind.
        assert store.db.execute("SELECT count(*) FROM search_map").fetchone()[0] == 0
        # The compacted sent body is deliberately gone (durable cleanup);
        # the pinned revision no longer resolves.
        with pytest.raises(ValueError, match="unknown pinned revision"):
            store.get(store.ref(doc["entry_id"], doc["revision"]))
    finally:
        store.close()


def test_withdrawn_entry_leaves_the_index_at_the_switch(tmp_path):
    store = Store(tmp_path, "test")
    try:
        doc = make_entry(entry_id="1" * 64, domain="test", kind="experience",
                         title="Withdrawn case", summary="s",
                         content="withdrawable body")
        store.install_feed([doc], feed_ident="f" * 64)
        assert store.query("withdrawable")["results"]
        store.install_feed([], feed_ident="f" * 64)  # upstream deletion
        assert store.query("withdrawable")["results"] == []
        assert store.get(store.ref("1" * 64))["withdrawn"] is True
    finally:
        store.close()


def test_query_never_retokenizes_bodies(tmp_path, monkeypatch):
    store = Store(tmp_path, "test")
    try:
        _corpus(store)
        import mindie_knowledge.retrieval as retrieval_mod

        calls = []
        real_tokens = retrieval_mod.tokens

        def counting(text):
            calls.append(len(text))
            return real_tokens(text)

        monkeypatch.setattr(retrieval_mod, "tokens", counting)
        monkeypatch.setattr(
            "mindie_knowledge.loop.store.tokens", counting, raising=False,
        )
        store.query("rms_norm")
        # Only the query text is tokenized at query time — never a body.
        assert len(calls) <= 1
        monkeypatch.setattr(retrieval_mod, "tokens", real_tokens)
        store.query("rms_norm")
    finally:
        store.close()


def test_corrupt_index_resets_derived_data_only(tmp_path):
    store = Store(tmp_path, "test")
    try:
        _corpus(store)
        assert store.query("rms_norm")["results"]
        captures_before = store.db.execute("SELECT count(*) FROM captures").fetchone()[0]
        revisions_before = store.db.execute("SELECT count(*) FROM revisions").fetchone()[0]
        # Simulate a damaged derived table.
        store.db.execute("DROP TABLE search_index")
        store.db.commit()
        with pytest.raises(ValueError, match="rebuilt"):
            store.query("rms_norm")
        assert store.db.execute("SELECT count(*) FROM revisions").fetchone()[0] == revisions_before
        assert store.db.execute("SELECT count(*) FROM captures").fetchone()[0] == captures_before
        # Background slice rebuilds it; authoritative tables untouched.
        while not store.advance_search_index():
            pass
        assert store.query("rms_norm")["results"]
    finally:
        store.close()


def test_backfill_apply_verifies_against_the_current_version(tmp_path, monkeypatch):
    """A draft that races the off-lock tokenization must never be overwritten
    by the stale derived snapshot, never lost from the index, and never make
    the index report complete while its current version is unindexed."""
    store = Store(tmp_path, "test")
    try:
        doc = store.create_draft(kind="experience", title="Race case",
                                 summary="s", content="version one body",
                                 owner=PRODUCER)
        store._search_reset()  # force a rebuild window
        import mindie_knowledge.retrieval as retrieval_mod

        real_index_text = retrieval_mod.index_text
        fired = {"once": True}

        def racing_index_text(text):
            if fired["once"]:
                fired["once"] = False
                # A concurrent append lands between the off-lock snapshot and
                # the apply transaction.
                store.append_observation(doc["entry_id"], "concurrent correction",
                                         marker="ef" * 16, producer=PRODUCER)
            return real_index_text(text)

        monkeypatch.setattr(retrieval_mod, "index_text", racing_index_text)
        assert store.advance_search_index() is False  # raced: not complete
        assert store.search_index_status()["requeued"] == 1
        # The hook already indexed the current body; the stale snapshot was
        # discarded rather than overwriting it.
        monkeypatch.setattr(retrieval_mod, "index_text", real_index_text)
        while not store.advance_search_index():
            pass
        hits = store.query("concurrent correction")["results"]
        assert hits
        # The indexed content is the CURRENT version (append-only body now
        # carries the correction), never the discarded stale snapshot.
        current = store.get(store.ref(doc["entry_id"]))
        assert "concurrent correction" in current["content"]
    finally:
        store.close()


def test_common_term_with_all_mismatched_conditions_stays_bounded(tmp_path):
    """A common term matching every entry with all conditions mismatched must
    return an empty result set without re-sorting or re-reading bodies."""
    store = Store(tmp_path, "test")
    try:
        for i in range(200):
            store.create_draft(
                kind="knowledge", title=f"Common case {i}",
                summary="sharedterm summary", content="sharedterm body",
                conditions={"torch_npu_version": "2.9"}, owner=PRODUCER,
            )
        start = time.monotonic()
        hits = store.query("sharedterm", conditions={"torch_npu_version": "9.9"},
                           limit=5)
        elapsed = time.monotonic() - start
        assert hits["results"] == []
        assert elapsed < 1.0  # SQL-level filtering: one bounded statement
        matching = store.query("sharedterm",
                               conditions={"torch_npu_version": "2.9"}, limit=5)
        assert len(matching["results"]) == 5
    finally:
        store.close()


def test_index_stores_no_document_text(tmp_path):
    """The contentless index holds only the inverted term index: no body and
    no token-stream text copy is retrievable from it."""
    store = Store(tmp_path, "test")
    try:
        store.create_draft(kind="experience", title="No text copy",
                           summary="s", content="the body itself is never stored",
                           owner=PRODUCER)
        rows = store.db.execute("SELECT tokens FROM search_index").fetchall()
        assert rows and all(row[0] is None for row in rows)
    finally:
        store.close()


def test_underscore_identifier_semantics_preserved(tmp_path):
    store = Store(tmp_path, "test")
    try:
        store.create_draft(kind="experience", title="Qualified name",
                           summary="s", content="torch_npu.npu_rms_norm failed",
                           owner=PRODUCER)
        store.create_draft(kind="experience", title="Adjacent words",
                           summary="s", content="rms norm were split apart",
                           owner=PRODUCER)
        # tokens() deliberately emits identifier components too, so parity
        # with the legacy path is the contract: identical recall set and
        # identical ordering (BM25 length normalization included).
        legacy = _legacy_hits(store, "rms_norm")
        fresh = store.query("rms_norm")["results"]
        legacy_ids = [hit.uri for hit in legacy]
        fresh_ids = [store._parse_ref(item["ref"])[0] for item in fresh]
        assert fresh_ids == legacy_ids
        assert len(fresh_ids) == 2  # both the identifier and the split words
    finally:
        store.close()


def test_query_serves_over_real_rpc_after_migration(tmp_path):
    """The real Service/rpc loopback path: an old store migrates in the
    background on the existing worker and queries succeed within the shared
    5s client budget."""
    project = tmp_path / "proj"
    project.mkdir()
    settings = write_settings(tmp_path / "community.json", enabled=False,
                              roots=[project])
    admission = make_admission(tmp_path, project_root=project)
    store = Store(tmp_path / "store", "test")
    _corpus(store)
    store._search_reset()  # force migration at service start
    engine = Engine(store, agent_command=None, settings_path=tmp_path / "community.json",
                    admission=Admission(admission))
    service = Service(engine, admission=Admission(admission))
    thread = threading.Thread(target=service.serve, daemon=True)
    thread.start()
    try:
        identity = dict(_session_id="manual-A", _session_verified=True)
        # While the derived index is incomplete, the RPC is an honest
        # transient read-rejection — never a fake empty result.
        first = None
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                first = rpc(service.connection, "query",
                            dict(query="rms_norm", **identity), timeout=5)
                break
            except ValueError as exc:
                assert "rebuilt" in str(exc)
                time.sleep(0.05)
        assert first is not None and first["results"]
        # The migration completed in the background without any user action.
        assert store.search_index_status()["complete"]
        page = rpc(service.connection, "explain",
                   dict(identity, ref=first["results"][0]["ref"]), timeout=5)
        assert page["content"]
    finally:
        service.close()
        thread.join(5)
        store.close()


def test_capture_proceeds_while_index_migration_is_pending(tmp_path):
    """A legal new capture during an unfinished index migration must not be
    terminated by the optional retrieval context: the authorized increment is
    organized normally (no failed region, no lost material), the optional
    refs are simply absent this round. Component evidence with the
    deterministic runner double; not a native model acceptance."""
    project = tmp_path / "proj"
    project.mkdir()
    settings = write_settings(tmp_path / "community.json", enabled=True,
                              roots=[project])
    adapter = make_admission(tmp_path, project_root=project)
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import json,sys\n"
        "p=json.load(sys.stdin)\n"
        "assert p['retrieved_refs']==[]\n"
        "assert any('not ready' in n for n in p['coverage']['notes'])\n"
        "print(json.dumps({'entries':[{'entry_id':None,'title':'Cold capture',"
        "'summary':'s','content':p['increment'][:100],'conditions':{}}]}))\n"
    )
    store = Store(tmp_path / "store", "test")
    _corpus(store)
    store._search_reset()  # cold migration pending while the capture arrives
    engine = Engine(
        store,
        agent_command=[__import__("sys").executable, str(runner)],
        settings_path=tmp_path / "community.json",
        admission=Admission(adapter),
    )
    result = engine.capture(session_id="manual-A", turn_id="t1",
                            summary="Fresh material during migration.")
    engine._process(result["id"])
    row = store.capture_row(result["id"])
    assert row["status"] == "organized", row["detail"]
    assert store.coverage_gaps() == []  # no region was failed or consumed
    # The rest of the library is still migrating, so retrieval honestly
    # reports not-ready; the new draft itself is indexed by the write hook.
    with pytest.raises(IndexNotReady):
        store.query("Fresh material")
    titles = [h["title"] for h in store.draft_headers(owner=None,
                                                      generation=row["generation"])]
    assert "Cold capture" in titles
    store.close()
