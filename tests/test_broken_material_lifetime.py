"""A draft whose material body expired or went missing while its SQL rows
remain must not hot-loop the outbox, must not pin the service alive, and
must stay visible as a fault. Real SQLite, synthetic clock, no network.
"""

import hashlib
import time

import pytest

from mindie_knowledge.loop import engine as engine_mod
from mindie_knowledge.loop import transcript_capture
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store
from mindie_knowledge.loop.transcript_capture import summarize_due

from conftest import write_settings


def _ident(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def _capture_increment(store, task, generation, part=0):
    body = 'Complete public observation. 中段证据与不确定性。\n'
    stream = _ident(('stream', task))
    prior = store.material_stream(stream)
    start = prior['source_cursor'] if prior else 0
    end = start + len(body.encode())
    return store.commit_material_increment(
        stream_key=stream, entry_id=task,
        prepared=dict(batch_id=_ident((task, 'batch', part)), redaction_state={}, blocks=[dict(
            block_id=_ident((task, 'block', part)), text=body,
            source_range=dict(start=start, end=end),
            title='Observation', summary='Uncertainty remains.')]),
        start=start, end=end, source_identity='synthetic',
        authorization=dict(generation=generation, id=_ident('authorization')),
        owner=_ident('owner'), observed_stream=prior)


def _break_draft_manifest(store, entry_id):
    revision = store._row(entry_id)['draft_revision']
    store.materials._manifest_path(entry_id, revision).unlink()


def test_missing_draft_material_settles_summary_visibly_and_service_idles(tmp_path, monkeypatch):
    settings = write_settings(tmp_path / 'community.json', consent_choice='contribute',
                              enabled=True, roots=[tmp_path])
    store = Store(tmp_path / 'store', 'test')
    try:
        engine = Engine(store, settings_path=str(tmp_path / 'community.json'))
        task = _ident('task-a')
        _capture_increment(store, task, settings.generation)
        _capture_increment(store, task, settings.generation, part=1)
        assert engine.index_queue.has_due()
        _break_draft_manifest(store, task)
        engine.last_activity = time.monotonic() - 100
        reported = []
        monkeypatch.setattr(transcript_capture, 'diagnostic_failure',
                            lambda *args, **kwargs: reported.append((args, kwargs)))
        summarize_due(engine)
        assert engine.index_queue.has_due() is False
        batches = {row['detail'] for row in store.db.execute('SELECT status, detail FROM material_batches')}
        assert batches == {'KeyError', 'draft material expired or missing'}
        task_row = store.db.execute('SELECT summary_status, summary_detail FROM transcript_tasks').fetchone()
        assert task_row['summary_status'] == 'failed' and 'KeyError' in task_row['summary_detail']
        assert reported and reported[0][1].get('category') == 'summary_failed'
        idle = engine.stop_if_idle()
        engine.operations.resume()
        engine.stop.clear()
        assert idle['idle'] is True
    finally:
        store.close()


def test_outbox_flush_failure_backs_off_and_success_resets(tmp_path, monkeypatch):
    settings = write_settings(tmp_path / 'community.json', consent_choice='contribute',
                              enabled=True, roots=[tmp_path])
    store = Store(tmp_path / 'store', 'test')
    try:
        engine = Engine(store, settings_path=str(tmp_path / 'community.json'))
        doc = store.create_draft(kind='experience', title='Draft', summary='s',
                                 content='body whose manifest disappears', owner=_ident('owner'),
                                 generation=settings.generation)
        _break_draft_manifest(store, doc['entry_id'])
        now = [1000.0]
        monkeypatch.setattr(engine_mod.time, 'monotonic', lambda: now[0])
        engine.last_activity = 0.0
        attempts = []
        original_flush = engine._flush

        def failing_flush():
            attempts.append(now[0])
            original_flush()

        budget = [0]

        def wait(_seconds):
            now[0] += 1.0
            budget[0] -= 1
            if budget[0] <= 0:
                engine.stop.set()
            return engine.stop.is_set()

        monkeypatch.setattr(engine.stop, 'wait', wait)
        monkeypatch.setattr(engine, '_flush', failing_flush)
        budget[0] = 40
        engine._outbox_loop()
        assert attempts == [1000.0, 1002.0, 1006.0, 1014.0, 1030.0]
        assert any('KeyError' in detail for detail in engine.errors)
        assert engine.background_errors.get('outbox') == 'KeyError'

        # Recovery: once the durable cause is fixed, one successful flush
        # resumes normal scheduling without a service restart.
        engine.stop.clear()
        monkeypatch.setattr(engine, '_flush', lambda: attempts.append(now[0]))
        budget[0] = 35
        engine._outbox_loop()
        assert attempts[5:] == [1062.0 + i for i in range(13)]
        assert 'outbox' not in engine.background_errors

        engine.stop.clear()
        monkeypatch.setattr(engine, '_flush', failing_flush)
        budget[0] = 5
        engine._outbox_loop()
        assert attempts[-2:] == [1075.0, 1077.0]
    finally:
        store.close()
