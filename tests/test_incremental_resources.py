"""Release gates count actual work; sample sizes are not runtime deadlines."""
import hashlib
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from mindie_knowledge.loop.store import Store
from mindie_knowledge.loop.transcript_capture import summarize_due, _admit_summary
from mindie_knowledge.materials.catalog import SourcePointers
from mindie_knowledge.materials.reme_checkpoint import ReMeCheckpoint
from mindie_knowledge.materials.store import MaterialCleanupError
from mindie_knowledge.materials.summarizer import SummaryLedger


def ident(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def increment(store, task, part):
    stream = ident(('stream', task))
    prior = store.material_stream(stream)
    start = prior['source_cursor'] if prior else 0
    body = f'Complete public observation {part}. 中段证据与不确定性。\n'
    end = start + len(body.encode())
    return store.commit_material_increment(
        stream_key=stream, entry_id=task,
        prepared=dict(batch_id=ident((task, part)), redaction_state={}, blocks=[dict(
            block_id=ident(('block', task, part)), text=body, source_range=dict(start=start, end=end),
            title='Observation', summary='Uncertainty remains.')]),
        start=start, end=end, source_identity='synthetic',
        authorization=dict(generation='synthetic', id=ident(part)), owner=ident('owner'), observed_stream=prior)


@pytest.mark.parametrize('completed', [1000, 20000])
def test_real_summary_idle_and_admission_paths_exclude_completed_history(store, completed):
    SummaryLedger(store.db)
    with store.db:
        store.db.executemany('INSERT INTO material_batches VALUES(?,?,?,?,?,?,?,?)',
            ((ident(i), 's', ident(i), '["b"]', 'complete', '', '{}', i) for i in range(completed)))
        store.db.executemany('INSERT INTO transcript_tasks VALUES(?,?,?,?,?,?,?,?,?)',
            ((ident(i), ident(i), 'c', 'd', 'complete', '', i, 0, '{}') for i in range(completed)))
        store.db.executemany('INSERT INTO material_summary_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            ((ident(i), ident(i), ident(i), 'body', 'digest', '{}', '["b"]',
              'complete', None, None, 0, 0) for i in range(completed)))
    engine = SimpleNamespace(store=store, last_activity=-100,
        begin_work=lambda: pytest.fail('idle scheduler admitted a completed batch'),
        _settings=lambda: SimpleNamespace(as_dict=lambda: {}))
    steps = []
    store.db.set_progress_handler(lambda: steps.append(1) or 0, 1)
    try:
        summarize_due(engine)
        _admit_summary(engine)
    finally:
        store.db.set_progress_handler(None, 0)
    assert len(steps) < 200, 'the real idle path scanned completed history'


def test_append_reads_one_manifest_and_no_old_body_or_global_pointer_map(store, monkeypatch):
    task = ident('long-task')
    for i in range(64):
        increment(store, task, i)
    reads = []
    original = Path.read_bytes
    def traced(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_bytes', traced)
    monkeypatch.setattr(SourcePointers, '__iter__', lambda _self: pytest.fail('append enumerated all tasks'))
    increment(store, task, 64)
    assert len([path for path in reads if path.parent.name == 'manifests']) == 1
    assert not [path for path in reads if path.parent.name == 'blocks']
    marker = json.loads((store.materials.root / 'current.json').read_text())
    assert set(marker) == {'schema', 'generation'}
    assert marker['schema'] == 'mindie-material-current/2'
    assert store.db.execute('SELECT count(*) FROM revisions').fetchone()[0] == 1


def test_warm_query_visits_selected_files_and_single_addition_checkpoints_one_block(store, monkeypatch):
    for i in range(64):
        store.create_draft(entry_id=ident(i), kind='experience', title='Public observation',
            summary='Reference material.', content=('needle ' if i == 0 else '') + 'calibration evidence')
    store.query('needle', limit=1)
    reads, stats, writes = [], [], []
    read, stat, commit = Path.read_bytes, Path.stat, ReMeCheckpoint.commit
    def rb(path, *args, **kwargs):
        if path.suffix == '.md':
            reads.append(path)
        return read(path, *args, **kwargs)
    def st(path, *args, **kwargs):
        if path.parent.name == 'blocks':
            stats.append(path)
        return stat(path, *args, **kwargs)
    def checkpoint(self, changed, deleted, generation):
        writes.append((set(changed), set(deleted)))
        return commit(self, changed, deleted, generation)
    monkeypatch.setattr(Path, 'read_bytes', rb)
    monkeypatch.setattr(Path, 'stat', st)
    monkeypatch.setattr(ReMeCheckpoint, 'commit', checkpoint)
    class NoScan(dict):
        def items(self):
            pytest.fail('warm query enumerated the current directory')
    catalog = store.materials._current_catalog()
    catalog.tasks = NoScan(catalog.tasks)
    assert store.query('needle', limit=1)['results']
    assert reads == [] and len(set(stats)) == 1 and writes == []
    catalog.tasks = dict(catalog.tasks)
    increment(store, ident('new-task'), 0)
    reads.clear(); stats.clear()
    assert store.query('中段证据', limit=1)['results']
    assert len(reads) == 1 and reads[0].parent.name == 'blocks'
    assert len(writes) == 1 and len(writes[0][0]) == 1 and not writes[0][1]


def test_restart_restores_term_counts_without_rechunking_old_body(tmp_path, monkeypatch):
    root = tmp_path / 'store'
    with closing(Store(root, 'test')) as store:
        increment(store, ident('task'), 0)
        expected = store.query('中段证据')['results'][0]['ref']
    from mindie_knowledge.materials.reme_index import VerifiedMarkdownFileChunker, TechnicalTokenizerV1
    tokenize = TechnicalTokenizerV1._tokenize_one
    def no_body_tokens(self, text, **kwargs):
        assert text == '中段证据', 'restart retokenized old full chunks'
        return tokenize(self, text, **kwargs)
    monkeypatch.setattr(TechnicalTokenizerV1, '_tokenize_one', no_body_tokens)
    async def no_chunk(*_args):
        pytest.fail('restart rechunked unchanged material')
    monkeypatch.setattr(VerifiedMarkdownFileChunker, 'chunk', no_chunk)
    with closing(Store(root, 'test')) as store:
        assert store.query('中段证据')['results'][0]['ref'] == expected


def test_another_local_writer_advances_directory_and_checkpoint(tmp_path):
    root = tmp_path / 'store'
    with closing(Store(root, 'test')) as first:
        increment(first, ident('first'), 0)
        first.query('observation')
        with closing(Store(root, 'test')) as second:
            increment(second, ident('second'), 0)
            assert len(second.query('observation')['results']) == 2
        assert len(first.query('observation')['results']) == 2
        assert first.materials._index._generation == first.materials.current_generation()


def test_reader_waits_for_other_writer_to_finish_catalog_marker_promotion(tmp_path, monkeypatch):
    root = tmp_path / 'store'
    with closing(Store(root, 'test')) as writer, closing(Store(root, 'test')) as reader:
        increment(writer, ident('first'), 0)
        assert len(reader.query('observation')['results']) == 1
        committing, release, reading = threading.Event(), threading.Event(), threading.Event()
        atomic = writer.materials._atomic
        acquire = reader.materials._write_lock.acquire
        def pause_marker(path, text):
            if path.name == 'current.json':
                committing.set()
                assert release.wait(5), 'test did not release the writer'
            return atomic(path, text)
        def observe_reader(**kwargs):
            reading.set()
            return acquire(**kwargs)
        monkeypatch.setattr(writer.materials, '_atomic', pause_marker)
        monkeypatch.setattr(reader.materials._write_lock, 'acquire', observe_reader)
        with ThreadPoolExecutor(max_workers=2) as pool:
            writing = pool.submit(increment, writer, ident('second'), 0)
            try:
                assert committing.wait(5), 'writer did not reach the promotion boundary'
                query = pool.submit(reader.query, 'observation')
                assert reading.wait(5), 'reader did not inspect the material lock'
                assert not query.done(), 'reader observed an in-progress catalog generation'
            finally:
                release.set()
            writing.result(timeout=5)
            assert len(query.result(timeout=5)['results']) == 2


def test_marker_failure_keeps_committed_material_and_repairs_only_exact_pending_generation(store, monkeypatch):
    original = store.materials._atomic
    task = ident('marker-failure')
    def fail_marker(path, text):
        if path.name == 'current.json':
            raise OSError('synthetic marker replacement failure')
        return original(path, text)
    monkeypatch.setattr(store.materials, '_atomic', fail_marker)
    with pytest.raises(MaterialCleanupError, match='catalog committed') as caught:
        increment(store, task, 0)
    assert caught.value.metadata_committed is True
    catalog = store.materials._current_catalog()
    assert catalog._meta('pending_marker') == catalog.generation
    monkeypatch.setattr(store.materials, '_atomic', original)
    assert store.query('中段证据')['results'][0]['entry_id'] == task
    assert catalog._meta('pending_marker') is None
    (store.materials.root / 'current.json').unlink()
    with pytest.raises(RuntimeError, match='Current material'):
        store.query('中段证据')


def test_failed_index_transaction_cannot_serve_uncommitted_memory(store, monkeypatch):
    increment(store, ident('first'), 0)
    store.query('observation')
    increment(store, ident('second'), 0)
    commit = ReMeCheckpoint.commit
    def failed(*_args):
        raise OSError('synthetic checkpoint failure')
    monkeypatch.setattr(ReMeCheckpoint, 'commit', failed)
    with pytest.raises(RuntimeError, match='Current material'):
        store.query('observation')
    monkeypatch.setattr(ReMeCheckpoint, 'commit', commit)
    assert len(store.query('observation')['results']) == 2


@pytest.mark.parametrize('separate_reader', [False, True])
def test_failed_promotion_cannot_serve_cached_older_visibility(tmp_path, monkeypatch, separate_reader):
    root = tmp_path / 'store'
    with closing(Store(root, 'test')) as writer:
        increment(writer, ident('first'), 0)
        reader = Store(root, 'test') if separate_reader else writer
        try:
            assert len(reader.query('observation')['results']) == 1
            generation = writer.materials.current_generation()
            original = writer.materials.retain_snapshot
            def fail_promotion(*_args, **_kwargs):
                raise OSError('synthetic pointer promotion failure')
            monkeypatch.setattr(writer.materials, 'retain_snapshot', fail_promotion)
            with pytest.raises(OSError, match='pointer promotion') as caught:
                increment(writer, ident('second'), 0)
            assert caught.value.metadata_committed is True
            assert writer.materials.current_generation() == generation
            assert writer.db.execute('SELECT count(*) FROM entries').fetchone()[0] == 2
            with pytest.raises(RuntimeError, match='Current material'):
                reader.query('observation')
            monkeypatch.setattr(writer.materials, 'retain_snapshot', original)
            writer._finish_material_writes()
            assert len(reader.query('observation')['results']) == 2
        finally:
            monkeypatch.undo()
            if separate_reader:
                reader.close()


def test_failed_chunk_read_restores_deletions_from_committed_checkpoint(store):
    from mindie_knowledge.loop import documents
    first = store.create_draft(entry_id=ident('replaced'), kind='experience', title='Evidence',
                               summary='Reference.', content='Original observation.')
    store.query('observation')
    replacement = documents.make_entry(entry_id=first['entry_id'], domain=store.domain, kind='experience',
        title='Correction', summary='Reference.', content='Replacement observation.')
    with store._write_txn():
        saved = store.materials.put_document(replacement)
        store._bind_material_draft(saved, generation='synthetic')
    task = store.materials.read_task(first['entry_id'])
    relative = f"tasks/{first['entry_id']}/blocks/{task['blocks'][0]['block_id']}.md"
    path = store.materials.root / relative
    valid = path.read_bytes()
    path.write_bytes(b'corrupt required block')
    with pytest.raises(RuntimeError, match='Current material'):
        store.query('observation')
    path.write_bytes(valid)
    assert len(store.query('replacement')['results']) == 1
    assert {row[0] for row in store.materials._index.checkpoint.db.execute('SELECT path FROM files')} == {relative}


def test_summary_budget_counts_known_calls_and_keeps_unknown_calls_conservative(store):
    from mindie_knowledge.loop.budget import BudgetExceeded
    SummaryLedger(store.db)
    now = time.time()
    rows = [(ident(i), ident('task'), ident(i), 'body', 'digest', '{}', '["b"]', 'failed',
             json.dumps(dict(model_calls=0, usage=None, billing_status='not_called')), None, now, now)
            for i in range(40)]
    with store.db:
        store.db.executemany('INSERT INTO material_summary_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', rows)
    engine = SimpleNamespace(store=store, _settings=lambda: SimpleNamespace(as_dict=lambda: dict(
        summary_budget=dict(calls_per_hour=1, input_tokens_per_hour=32768))))
    _admit_summary(engine)
    with store.db:
        store.db.execute('INSERT INTO material_summary_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (ident('unknown'), ident('task'), ident('unknown'), 'body', 'digest', '{}', '["b"]',
             'outcome_unknown', None, 'interrupted', now, now))
    with pytest.raises(BudgetExceeded, match='rolling budget'):
        _admit_summary(engine)
    assert store.db.execute("SELECT status FROM material_summary_attempts WHERE attempt_id=?", (ident('unknown'),)).fetchone()[0] == 'outcome_unknown'


@pytest.mark.parametrize("corruption", ["version", "generation", "source_revision", "pending_marker", "tasks", "database"])
def test_existing_catalog_corruption_never_resets_as_first_use(tmp_path, corruption):
    import sqlite3
    from mindie_knowledge.materials import MaterialStore
    root = tmp_path / "catalog-corruption"
    with closing(MaterialStore(root, "demo")) as materials:
        task = materials.append_batch(ident("task"), [{"block_id": ident("block"),
            "text": "Current evidence.", "source_range": {}, "title": "Evidence", "summary": "Fallible."}],
            "Current navigation", title="Evidence", promote=True)
        assert materials.visible_tasks()
    marker = (root / "current.json").read_bytes()
    if corruption == "database":
        (root / ".current-catalog.sqlite3").unlink()
    else:
        with closing(sqlite3.connect(root / ".current-catalog.sqlite3")) as db, db:
            if corruption == "tasks":
                db.execute("DROP TABLE tasks")
            else:
                db.execute("DELETE FROM meta WHERE key=?", (corruption,))
    with closing(MaterialStore(root, "demo")) as materials:
        with pytest.raises(ValueError, match="existing current material catalog"):
            materials.visible_tasks()
    assert (root / "current.json").read_bytes() == marker
