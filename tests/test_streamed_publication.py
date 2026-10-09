"""Long-task publication keeps bodies on disk and rechecks exact frozen bytes."""
from contextlib import closing
import gc
import os
from pathlib import Path
import time
import tracemalloc
from types import SimpleNamespace

import pytest

from mindie_knowledge.community.batch import scan_outbound, validate_batch, render_pr_body
from mindie_knowledge.community.gitops import apply_files
from mindie_knowledge.loop.export import build_batch
from mindie_knowledge.loop.store import Store, digest
from mindie_knowledge.materials.file_source import FileText
from mindie_knowledge.materials.publication import load_batch_payload, staging_path


def append(store, count):
    task_id = 'c' * 64
    text = ('Synthetic public evidence remains unverified. ' * 400)[:16000]
    blocks = [dict(block_id=digest(['part', index]), text=text, title='Synthetic trial',
                   summary='The result remains uncertain.', source_range={'part': index})
              for index in range(count)]
    with store._write_txn():
        task = store.materials.append_batch(task_id, blocks, 'No acceptance claim.',
                                           title='Long synthetic task', source='draft')
        store._bind_material_draft(task['entry'], generation='fixture')
    return task_id, len(text.encode()) * count


def test_single_long_task_freeze_reload_validate_and_apply_do_not_retain_body_bytes(tmp_path, record_property):
    with closing(Store(tmp_path / 'producer', 'demo')) as store:
        task_id, body_bytes = append(store, 1024)
        gc.collect()
        tracemalloc.start()
        try:
            batch_id, _, batch, _, _ = build_batch(store, settings=SimpleNamespace(generation='fixture'))
            loaded = load_batch_payload(store, store.batch(batch_id))
            checked = validate_batch(loaded)
            scan_outbound(checked, 'Public title', 'Public body', 'Public commit')
            destination = tmp_path / 'worktree'
            apply_files(destination, checked['files'])
            gc.collect()
            retained, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert retained < body_bytes // 2, (retained, peak, body_bytes)
        assert peak < body_bytes * 2, (retained, peak, body_bytes)
        for name, value in [('body_bytes', body_bytes), ('retained_python_bytes', retained), ('peak_python_bytes', peak)]:
            record_property(name, value)
        assert len(checked['files']) == 1025
        assert all(isinstance(item, FileText) for item in batch['files'])
        assert len(list((destination / 'tasks' / task_id / 'blocks').glob('*.md'))) == 1024
        assert len(render_pr_body(checked)) < 16000
        assert 'additional files' in render_pr_body(checked)


def test_frozen_payload_survives_current_generation_removal_and_rejects_late_mutation(tmp_path):
    with closing(Store(tmp_path / 'producer', 'demo')) as store:
        task_id, _ = append(store, 2)
        batch_id, revision, batch, _, _ = build_batch(store, settings=SimpleNamespace(generation='fixture'))
        original = next(item for item in batch['files'] if '/blocks/' in item['path'])
        expected = original['content']
        # A candidate refers to frozen files, never the mutable current pointer.
        import shutil
        shutil.rmtree(store.materials.root / 'tasks' / task_id)
        assert original['content'] == expected
        checked = validate_batch(load_batch_payload(store, store.batch(batch_id)))
        frozen = staging_path(store.root, batch_id, revision) / 'files' / original['path']
        frozen.write_text('Different bytes appeared after validation.')
        with pytest.raises(ValueError, match='identity mismatch'):
            apply_files(tmp_path / 'destination', checked['files'])


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='POSIX pipe fixture')
def test_nonregular_frozen_source_fails_before_opening_a_pipe(tmp_path):
    path = tmp_path / 'body.md'
    os.mkfifo(path)
    source = FileText(path, root=tmp_path, sha256='a' * 64, metadata={'path': 'body.md'})
    started = time.monotonic()
    with pytest.raises(ValueError, match='regular file'):
        source['content']
    assert time.monotonic() - started < 1
