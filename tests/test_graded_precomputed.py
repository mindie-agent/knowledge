"""Cold-query reuse, transactional publication and source-integrity boundaries."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from graded_fixture import OBSERVED, unit
from mindie_knowledge.graded import index as index_module
from mindie_knowledge.graded.repository import CATALOG_KEY, INDEX_KEY
from mindie_knowledge.graded.units import GradedUnit
from mindie_knowledge.loop.store import Store
from mindie_knowledge.materials.references import ReadReferenceError


def read_rows(store):
    return dict(store.db.execute("SELECT key,value FROM state WHERE key IN (?,?)", (CATALOG_KEY, INDEX_KEY)))


def forbid_recomputation(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("a current precomputed index must not parse YAML or tokenize bodies")
    monkeypatch.setattr(GradedUnit, "from_text", classmethod(unexpected))
    monkeypatch.setattr(index_module, "token_counts", unexpected)


def test_reopened_query_preserves_scores_bodies_and_inheritance_without_recomputation(store, tmp_path, monkeypatch):
    parent = unit("unit-parent", body="Graph capture common premise.")
    child = unit(parents=[parent.id])
    store.graded.install([parent, child])
    before = store.query("graph", conditions=OBSERVED)
    counts = store.graded._index.counts
    reader = Store(tmp_path, store.domain)
    try:
        with monkeypatch.context() as guard:
            forbid_recomputation(guard)
            guard.setattr(reader.graded, "_read", lambda descriptor: (_ for _ in ()).throw(
                AssertionError("query must not re-read source blobs")))
            after = reader.query("graph", conditions=OBSERVED)
            assert {k: v for k, v in after.items() if k != "query_elapsed_ms"} == {
                k: v for k, v in before.items() if k != "query_elapsed_ms"}
            assert reader.graded._index.counts == counts
        row = next(row for row in after["results"] if row["id"] == child.id)
        assert row["ref"].endswith("@" + str(child.revision))
        assert reader.explain(row["ref"])["content"] == child.body
    finally:
        reader.close()


def test_existing_reader_observes_other_process_replacement_and_withdrawal(store, tmp_path):
    original = unit()
    store.graded.install_feed([original], feed_ident="fixture-feed", source_revision="a" * 40)
    observed = store.query("graph", conditions=OBSERVED)["results"][0]
    revised = GradedUnit.from_parts({**original.metadata, "revision": 2}, original.body + "Corrected graph premise.", original.path)
    command = """import json,sys
from mindie_knowledge.graded.units import GradedUnit
from mindie_knowledge.loop.store import Store
rows=json.load(sys.stdin)
store=Store(sys.argv[1], 'vllm-ascend')
try:
    store.graded.install_feed([GradedUnit.from_parts(**row) for row in rows], feed_ident='fixture-feed', source_revision='b'*40)
finally:
    store.close()
"""
    for incoming in ([revised], []):
        subprocess.run([sys.executable, "-c", command, str(tmp_path)], input=json.dumps([
            {"frontmatter": item.metadata, "body": item.body, "path": item.path} for item in incoming]),
            text=True, capture_output=True, check=True, timeout=10, cwd=Path(__file__).resolve().parents[1])
        found = store.query("graph", conditions=OBSERVED)["results"]
        assert [row["id"] for row in found] == [item.id for item in incoming]
        if incoming:
            assert found[0]["revision"] == 2 and found[0]["excerpt"] == revised.body
        with pytest.raises(ReadReferenceError):
            store.explain(observed["ref"])


def test_concurrent_publication_between_hot_check_and_snapshot_read_uses_one_generation(store, tmp_path, monkeypatch):
    original = unit()
    store.graded.install([original])
    corrected = GradedUnit.from_parts(original.metadata, original.body + "Corrected graph premise.", original.path)
    reader = Store(tmp_path, store.domain)
    current = reader.graded._current
    calls = []

    def publish_after_read():
        raw = current()
        calls.append(raw)
        if len(calls) == 2:  # available(), then the _load() hot-cache check
            store.graded.install([corrected])
        return raw

    monkeypatch.setattr(reader.graded, "_current", publish_after_read)
    try:
        assert reader.query("graph", conditions=OBSERVED)["results"][0]["excerpt"] == corrected.body
    finally:
        reader.close()


def test_missing_derived_index_is_rebuilt_from_authoritative_storage_once(store, tmp_path):
    original = unit()
    store.graded.install([original])
    with store._write_txn():
        store.db.execute("DELETE FROM state WHERE key=?", (INDEX_KEY,))
    reader = Store(tmp_path, store.domain)
    try:
        assert reader.query("graph", conditions=OBSERVED)["results"][0]["excerpt"] == original.body
        assert INDEX_KEY in read_rows(reader)
    finally:
        reader.close()
    reopened = Store(tmp_path, store.domain)
    try:
        assert reopened.query("graph", conditions=OBSERVED)["results"][0]["excerpt"] == original.body
    finally:
        reopened.close()


def test_failed_catalog_write_rolls_back_precomputed_index_and_current_generation(store, tmp_path):
    original = unit()
    store.graded.install([original])
    before = read_rows(store)
    store.db.execute("""CREATE TEMP TRIGGER interrupt_catalog BEFORE INSERT ON state
        WHEN NEW.key='graded:current' BEGIN SELECT RAISE(ABORT, 'controlled catalog failure'); END""")
    changed = GradedUnit.from_parts(original.metadata, original.body + "Corrected graph premise.", original.path)
    with pytest.raises(sqlite3.IntegrityError, match="controlled catalog failure"):
        store.graded.install([changed])
    assert read_rows(store) == before
    reader = Store(tmp_path, store.domain)
    try:
        assert reader.query("graph", conditions=OBSERVED)["results"][0]["excerpt"] == original.body
    finally:
        reader.close()


def test_stale_derived_index_generation_is_rebuilt_explicitly(store, tmp_path):
    original = unit()
    store.graded.install([original])
    with store._write_txn():
        snapshot = json.loads(read_rows(store)[INDEX_KEY])
        snapshot["generation"] = "stale-generation"
        store.db.execute("UPDATE state SET value=? WHERE key=?",
                         (json.dumps(snapshot, sort_keys=True), INDEX_KEY))
    reader = Store(tmp_path, store.domain)
    try:
        assert reader.query("graph", conditions=OBSERVED)["results"][0]["excerpt"] == original.body
        assert json.loads(read_rows(reader)[INDEX_KEY])["generation"] != "stale-generation"
    finally:
        reader.close()
