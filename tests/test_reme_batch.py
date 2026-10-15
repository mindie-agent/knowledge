"""ReMe native batch updates preserve sequential results and recovery."""
from contextlib import closing
import hashlib

import pytest

from mindie_knowledge.materials import MaterialStore
from mindie_knowledge.materials.reme_index import StrictFileStore
import mindie_knowledge.materials.reme_index as index_module


def ident(value):
    return hashlib.sha256(value.encode()).hexdigest()


def package(author, name, words):
    task = ident(name)
    author.append_batch(
        task,
        [dict(block_id=ident(name + str(i)), text=text, source_range={"part": i},
              title="Evidence " + name, summary="Reference observations.")
         for i, text in enumerate(words)],
        "Evidence is provisional.", title="Task " + name, status="complete", promote=True,
    )
    return author.export_task(task)


def test_batch_matches_sequential_scores_order_and_incremental_results(tmp_path, monkeypatch):
    with closing(MaterialStore(tmp_path / "author", "demo")) as author:
        first = [package(author, str(i), ["calibration common " * (i + 1),
                                        "graph replay corrected " * (5 - i)]) for i in range(4)]
        changed = package(author, "new", ["new calibration result", "graph correction"])
    original = StrictFileStore.upsert
    seen = []

    async def sequential(self, values):
        for value in values:
            await original(self, [value])

    async def batch(self, values):
        seen.append(len(values))
        await original(self, values)

    def exercise(root):
        observed = []
        with closing(MaterialStore(root, "demo")) as store:
            for packages in (first, [*first[1:], changed], [changed]):
                store.install_packages(packages)
                cold = store.search("calibration graph", limit=5)
                assert store.search("calibration graph", limit=5) == cold
                observed.append(cold)
        with closing(MaterialStore(root, "demo")) as store:
            assert store.search("calibration graph", limit=5) == observed[-1]
        return observed

    monkeypatch.setattr(StrictFileStore, "upsert", sequential)
    expected = exercise(tmp_path / "sequential")
    monkeypatch.setattr(StrictFileStore, "upsert", batch)
    actual = exercise(tmp_path / "batch")
    assert actual == expected
    assert seen == [8, 2]  # Removed blocks, warm reads and restart add no work.


def test_partial_upsert_failure_restores_committed_checkpoint(tmp_path, monkeypatch):
    with closing(MaterialStore(tmp_path / "author", "demo")) as author:
        first = package(author, "first", ["initial calibration"])
        second = package(author, "second", ["new calibration", "late correction"])
    with closing(MaterialStore(tmp_path / "reader", "demo")) as store:
        store.install_packages([first])
        previous = store.search("calibration")
        checkpoint = store._index.checkpoint
        generation = checkpoint.generation
        records = dict(checkpoint.records())
        store.install_packages([first, second])
        original = StrictFileStore.upsert

        async def fail_after_one(self, values):
            await original(self, values[:1])
            raise OSError("injected partial native upsert failure")

        monkeypatch.setattr(StrictFileStore, "upsert", fail_after_one)
        with pytest.raises(OSError, match="partial native upsert"):
            store.search("calibration")
        assert checkpoint.generation == generation
        assert dict(checkpoint.records()) == records
        assert store._index.store is None
        assert store._index._chunk_body_terms == {}
        assert store._index._body_term_pool == {}
        monkeypatch.setattr(StrictFileStore, "upsert", original)
        found = store.search("calibration")
        assert {item["entry_id"] for item in found} == {first["task_id"], second["task_id"]}
        assert any(item["ref"] == previous[0]["ref"] for item in found)


def test_chunk_terms_reuse_preserves_results_across_queries_changes_and_restart(tmp_path, monkeypatch):
    with closing(MaterialStore(tmp_path / "author", "demo")) as author:
        first = package(author, "first", ["calibration alpha", "calibration beta"])
        second = package(author, "second", ["calibration gamma"])
    original = index_module.body_tokens
    processed = []

    def counted(body):
        processed.append(body)
        return original(body)

    root = tmp_path / "cached"
    with closing(MaterialStore(tmp_path / "uncached", "demo")) as reference:
        with closing(MaterialStore(root, "demo")) as cached:
            previous = {}
            for packages in ([first], [first, second], [second]):
                reference.install_packages(packages)
                cached.install_packages(packages)
                for number, query in enumerate(("calibration", "alpha calibration", "gamma calibration")):
                    if reference._index is not None:
                        reference._index.clear_query_terms()
                    expected = reference.search(query)
                    processed.clear()
                    with monkeypatch.context() as patch:
                        patch.setattr(index_module, "body_tokens", counted)
                        assert cached.search(query) == expected
                    if number:
                        assert processed == []
                    current = cached._index._chunk_body_terms
                    for ident in set(previous) & set(current):
                        assert current[ident] is previous[ident]
                    assert set(current) == set(cached._index.store.file_chunks)
                    pooled = cached._index._body_term_pool
                    assert set(pooled) == {term for _, terms in current.values() for term in terms}
                    assert all(term is pooled[term] for _, terms in current.values() for term in terms)
                previous = dict(current)
            index = cached._index
        assert index._chunk_body_terms == {}
        assert index._body_term_pool == {}

        expected = reference.search("calibration")
        with closing(MaterialStore(root, "demo")) as restarted:
            processed.clear()
            with monkeypatch.context() as patch:
                patch.setattr(index_module, "body_tokens", counted)
                assert restarted.search("calibration") == expected
                assert len(processed) == len(restarted._index.store.file_chunks)
                processed.clear()
                assert restarted.search("calibration") == expected
                assert processed == []


def test_cached_chunk_terms_do_not_hide_changed_selected_authority(tmp_path):
    with closing(MaterialStore(tmp_path / "author", "demo")) as author:
        candidate = package(author, "selected", ["calibration authority"])
    with closing(MaterialStore(tmp_path / "reader", "demo")) as store:
        store.install_packages([candidate])
        assert store.search("calibration")
        assert store._index._chunk_body_terms
        relative = next(path for path in candidate["files"] if path.startswith("blocks/"))
        target = store.root / "tasks" / candidate["task_id"] / relative
        target.write_text("Changed authority after a cached query.", encoding="utf-8")
        with pytest.raises(ValueError, match="hash mismatch"):
            store.search("calibration")
        assert store.index_status()["phase"] == "failed"
        assert store._index._chunk_body_terms == {}
        assert store._index._body_term_pool == {}


def test_citation_adjacent_body_term_need_not_exist_in_bm25_vocabulary(tmp_path):
    with closing(MaterialStore(tmp_path / "author", "demo")) as author:
        candidate = package(author, "adjacent", [
            "calibration mindie://demo/" + ident("cited task") + ":x",
        ])
    with closing(MaterialStore(tmp_path / "reader", "demo")) as store:
        store.install_packages([candidate])
        expected = store.search("calibration")
        terms = next(iter(store._index._chunk_body_terms.values()))[1]
        assert "x" in terms
        assert "x" not in store._index.store.keyword_index.vocab
        assert store.search("calibration") == expected
        store._index.clear_query_terms()
        assert store.search("calibration") == expected
