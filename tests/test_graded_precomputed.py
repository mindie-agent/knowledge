"""Cold-query reuse, transactional publication and source-integrity boundaries."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from graded_fixture import OBSERVED, unit
from mindie_knowledge.graded import index as index_module
from mindie_knowledge.graded.repository import CATALOG_KEY, INDEX_KEY
from mindie_knowledge.graded.units import GradedUnit, canonical
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
    read = reader.graded._read_blob
    read_ids = []

    def checked_read(descriptor):
        read_ids.append(descriptor["id"])
        return read(descriptor)

    try:
        with monkeypatch.context() as guard:
            forbid_recomputation(guard)
            guard.setattr(reader.graded, "_read_blob", checked_read)
            after = reader.query("graph", conditions=OBSERVED)
            assert {k: v for k, v in after.items() if k != "query_elapsed_ms"} == {
                k: v for k, v in before.items() if k != "query_elapsed_ms"}
            assert reader.graded._index.counts == counts
            assert set(read_ids) == {parent.id, child.id} and len(read_ids) == 2
        assert reader.explain(next(row for row in after["results"] if row["id"] == child.id)["ref"])["content"] == child.body
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


def make_legacy(store):
    catalog = json.loads(read_rows(store)[CATALOG_KEY])
    del catalog["index_sha256"]
    with store._write_txn():
        store.db.execute("UPDATE state SET value=? WHERE key=?", (canonical(catalog), CATALOG_KEY))
        store.db.execute("DELETE FROM state WHERE key=?", (INDEX_KEY,))


def test_legacy_catalog_is_rebuilt_once_inside_first_query(store, tmp_path, monkeypatch):
    original = unit()
    store.graded.install([original])
    make_legacy(store)
    reader = Store(tmp_path, store.domain)
    try:
        assert reader.query("graph", conditions=OBSERVED)["results"][0]["excerpt"] == original.body
        rows = read_rows(reader)
        assert hashlib.sha256(rows[INDEX_KEY].encode()).hexdigest() == json.loads(rows[CATALOG_KEY])["index_sha256"]
    finally:
        reader.close()
    forbid_recomputation(monkeypatch)
    reopened = Store(tmp_path, store.domain)
    try:
        assert reopened.query("graph", conditions=OBSERVED)["results"][0]["excerpt"] == original.body
    finally:
        reopened.close()


def test_legacy_upgrade_rereads_catalog_after_writer_lock_and_preserves_new_publication(store, tmp_path, monkeypatch):
    original = unit()
    store.graded.install([original])
    make_legacy(store)
    corrected = GradedUnit.from_parts(original.metadata, original.body + "Corrected graph premise.", original.path)
    reader = Store(tmp_path, store.domain)
    transaction = reader._write_txn
    published = []

    @contextmanager
    def publish_before_lock():
        if not published:
            store.graded.install([corrected])
            published.append(True)
        with transaction():
            yield

    monkeypatch.setattr(reader, "_write_txn", publish_before_lock)
    try:
        assert reader.query("graph", conditions=OBSERVED)["results"][0]["excerpt"] == corrected.body
        assert store.query("graph", conditions=OBSERVED)["results"][0]["excerpt"] == corrected.body
    finally:
        reader.close()


@pytest.mark.parametrize("fault", ["missing_index", "index_digest", "blob_digest", "projection_content"])
def test_declared_index_or_source_corruption_is_visible_without_repair(store, tmp_path, fault):
    store.graded.install([unit()])
    rows = read_rows(store)
    catalog = json.loads(rows[CATALOG_KEY])
    with store._write_txn():
        if fault == "missing_index":
            store.db.execute("DELETE FROM state WHERE key=?", (INDEX_KEY,))
        elif fault == "index_digest":
            store.db.execute("UPDATE state SET value=? WHERE key=?", (rows[INDEX_KEY] + " ", INDEX_KEY))
        elif fault == "projection_content":
            snapshot = json.loads(rows[INDEX_KEY])
            snapshot["units"][0]["body"] += "Unbound graph claim.\n"
            raw = canonical(snapshot)
            catalog["index_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
            store.db.execute("UPDATE state SET value=? WHERE key=?", (raw, INDEX_KEY))
            store.db.execute("UPDATE state SET value=? WHERE key=?", (canonical(catalog), CATALOG_KEY))
        else:
            path = store.root / "graded/blobs" / (catalog["units"][0]["blob"] + ".md")
            path.write_bytes(path.read_bytes() + b"Unbound graph claim.\n")
    damaged = read_rows(store)
    reader = Store(tmp_path, store.domain)
    try:
        with pytest.raises(ValueError, match="missing|digest mismatch|content disagree"):
            reader.query("graph", conditions=OBSERVED)
        assert read_rows(reader) == damaged
    finally:
        reader.close()


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
