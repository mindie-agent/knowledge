"""Failures must preserve work and remain distinguishable from empty success."""
from contextlib import closing
import sqlite3
import pytest
from mindie_knowledge.loop.activation import Admission, AdmissionUnavailable
from mindie_knowledge.loop.store import Store
from mindie_knowledge.community.transport import FileTransport


@pytest.mark.parametrize('operation', ['leases', 'active_lease', 'check', 'resolve'])
def test_corrupt_admission_is_not_inactive(tmp_path, operation):
    path = tmp_path / 'admission.sqlite3'
    path.write_bytes(b'broken database')
    gate = Admission(path)
    args = [] if operation == 'leases' else ['session']
    with pytest.raises(AdmissionUnavailable):
        getattr(gate, operation)(*args)
    assert path.read_bytes() == b'broken database'


def test_locked_admission_is_not_empty(tmp_path):
    gate = Admission(tmp_path / 'admission.sqlite3')
    gate.activate('active', project_root=str(tmp_path))
    db = sqlite3.connect(gate.path)
    try:
        db.execute('BEGIN EXCLUSIVE')
        with pytest.raises(AdmissionUnavailable):
            gate.leases()
    finally:
        db.rollback(); db.close()
    assert len(gate.leases()) == 1


@pytest.mark.parametrize('name,read', [('search_backfill', '_search_backfill_state'),
                                       ('search_requeue', '_search_requeue_read')])
def test_corrupt_index_progress_is_not_reset(tmp_path, name, read):
    with closing(Store(tmp_path / 'store', 'test')) as store:
        with store._write_txn():
            store.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (name, '{broken'))
        with pytest.raises(ValueError, match='invalid stored search'):
            getattr(store, read)()
        assert store.db.execute('SELECT value FROM meta WHERE key=?', (name,)).fetchone()[0] == '{broken'


def test_corrupt_export_attempt_cannot_be_retried_as_new(tmp_path):
    with closing(Store(tmp_path / 'store', 'test')) as store:
        with store._write_txn():
            store.db.execute('INSERT INTO state VALUES (?,?)', ('export:fixture', '{broken'))
        with pytest.raises(ValueError, match='invalid stored export'):
            store.reserve_export('fixture')
        assert store.db.execute("SELECT value FROM state WHERE key='export:fixture'").fetchone()[0] == '{broken'


@pytest.mark.parametrize('bad', ['{broken', '[]', '{"repos":[]}'])
def test_file_transport_never_overwrites_corrupt_state(tmp_path, bad):
    path = tmp_path / 'remote.json'
    api = FileTransport(path)
    path.write_text(bad)
    with pytest.raises(ValueError):
        api.seed('owner/repo', permissions=True)
    assert path.read_text() == bad


def test_corrupt_batch_cannot_receive_a_success_receipt(tmp_path):
    with closing(Store(tmp_path / 'store', 'test')) as store:
        store.create_batch(batch_id='batch', revision='revision', batch={'entry_refs': []}, entry_ids=[], vote_keys=[])
        with store._write_txn():
            store.db.execute("UPDATE outbox SET batch='{broken' WHERE batch_id='batch'")
        with pytest.raises(ValueError, match='invalid stored outbox batch'):
            store.mark_batch('batch', 'submitted', head_sha='a' * 40)
        row = store.batch('batch')
        assert row['batch'] == '{broken' and row['status'] == 'pending'


@pytest.mark.parametrize('module_name', ['loop.process', 'community.common'])
def test_process_reader_error_is_not_eof(monkeypatch, module_name):
    import importlib
    import sys
    module = importlib.import_module('mindie_knowledge.' + module_name)
    original = module._reader
    def broken(stream, tag, chunks, stop, errors):
        errors.append(OSError('private read failure'))
        chunks.put((tag, None))
    monkeypatch.setattr(module, '_reader', broken)
    command = [sys.executable, '-c', "print('fixture')"]
    if module_name == 'loop.process':
        with pytest.raises(RuntimeError, match='output read failed') as caught:
            module.bounded_run(command, '', timeout=2, max_output=4096)
        assert caught.value.mindie_category == 'invalid_result'
    else:
        with pytest.raises(module.UnknownOutcome, match='output read failed'):
            module.run_argv(command, timeout=2)


def test_summary_worker_failure_is_visible_in_status(tmp_path, monkeypatch):
    from mindie_knowledge.loop.engine import Engine
    from mindie_knowledge.loop import transcript_capture
    with closing(Store(tmp_path / 'store', 'test')) as store:
        engine = Engine(store)
        monkeypatch.setattr(engine.stop, 'wait', lambda _: False)
        def broken(_):
            raise OSError('private filesystem detail')
        monkeypatch.setattr(transcript_capture, 'summarize_due', broken)
        engine._summary_loop()
        status = engine.status()
        assert status['background_errors'] == {'summary': 'OSError'}
        assert status['errors'] == ['summary worker stopped: OSError']
