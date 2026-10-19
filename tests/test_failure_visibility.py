"""Failures must preserve work and remain distinguishable from empty success."""
from contextlib import closing
import sqlite3
import pytest
from mindie_knowledge.loop.activation import Admission, AdmissionUnavailable
from mindie_knowledge.loop.store import Store
from mindie_knowledge.community.transport import FileTransport
from mindie_knowledge.materials.references import MaterialReadError


@pytest.mark.parametrize('suffix', ['', 'missing/nested'])
def test_invalid_diagnostic_root_is_not_no_pending_incidents(tmp_path, monkeypatch, suffix):
    from mindie_knowledge.loop import agent_diagnostics
    parent = tmp_path / 'not-a-directory'
    parent.write_bytes(b'unchanged authority ancestor')
    monkeypatch.setenv('MINDIE_DIAGNOSTICS_ROOT', str(parent / suffix))
    with pytest.raises(NotADirectoryError):
        agent_diagnostics.pending()
    assert parent.read_bytes() == b'unchanged authority ancestor'
    assert list(tmp_path.iterdir()) == [parent]


def test_unused_diagnostic_read_creates_no_state(tmp_path, monkeypatch):
    from mindie_knowledge.loop import agent_diagnostics
    monkeypatch.setenv('MINDIE_DIAGNOSTICS_ROOT', str(tmp_path / 'missing' / 'nested'))
    assert agent_diagnostics.pending() is None
    assert list(tmp_path.iterdir()) == []



def test_delivery_projection_can_load_without_installed_core(tmp_path):
    import subprocess
    import sys
    from mindie_knowledge.loop import agent_diagnostics
    blocked = tmp_path / 'not-a-directory'
    blocked.write_bytes(b'unchanged ancestor')
    program = '''import os, runpy, sys
module = runpy.run_path(sys.argv[1])
os.environ['MINDIE_DIAGNOSTICS_ROOT'] = sys.argv[2]
assert module['pending']() is None
os.environ['MINDIE_DIAGNOSTICS_ROOT'] = sys.argv[3]
try:
    module['pending']()
except NotADirectoryError:
    pass
else:
    raise AssertionError('invalid root became no pending incidents')
'''
    result = subprocess.run([sys.executable, '-I', '-S', '-c', program, agent_diagnostics.__file__,
                             str(tmp_path / 'missing' / 'nested'), str(blocked)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert blocked.read_bytes() == b'unchanged ancestor'
    assert list(tmp_path.iterdir()) == [blocked]


@pytest.mark.parametrize('damage', ['empty', 'table'])
def test_diagnostic_delivery_damage_is_not_no_pending_incidents(tmp_path, monkeypatch, damage):
    from mindie_knowledge.loop import agent_diagnostics
    monkeypatch.setenv('MINDIE_DIAGNOSTICS_ROOT', str(tmp_path / 'diagnostics'))
    agent_diagnostics.enqueue('mindie-knowledge', 'capture', 'projection', 'invalid_record',
                              dict(logging_failed=True))
    path = agent_diagnostics.root() / 'agent-delivery.sqlite3'
    if damage == 'empty':
        path.write_bytes(b'')
    else:
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('DROP TABLE pending')
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(ValueError, match='missing'):
        agent_diagnostics.pending()
    assert (path.read_bytes() if path.exists() else None) == before


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
        # WAL can safely read the preceding committed snapshot during an
        # exclusive writer. DELETE mode supplies a real blocked read here.
        db.execute('PRAGMA journal_mode=DELETE')
        db.execute('BEGIN EXCLUSIVE')
        with pytest.raises(AdmissionUnavailable):
            gate.leases()
    finally:
        db.rollback(); db.close()
    assert len(gate.leases()) == 1


@pytest.mark.parametrize('bad', ['{broken', '[]'])
def test_corrupt_material_pointer_is_not_empty_success(tmp_path, bad):
    with closing(Store(tmp_path / 'store', 'test')) as store:
        store.create_draft(kind='experience', title='Preserved', summary='Observation', content='material canary')
        path = store.materials.root / 'current.json'
        path.write_text(bad)
        with pytest.raises(MaterialReadError) as caught:
            store.query('material')
        assert caught.value.code == 'material_corrupt'
        assert isinstance(caught.value.__cause__, ValueError)
        assert path.read_text() == bad


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
        from mindie_knowledge.materials.publication import freeze_batch
        descriptor = freeze_batch(store.root, dict(batch_id='batch', revision='a' * 64, files=[], entry_refs=[]))
        store.create_batch(batch_id='batch', revision='a' * 64, batch=descriptor, entry_ids=[], vote_keys=[])
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


def test_outbox_reports_a_new_failure_after_recovery_without_repeating_unchanged_fault(tmp_path, monkeypatch):
    from mindie_knowledge.loop import agent_diagnostics
    from conftest import write_settings
    from mindie_knowledge.loop.engine import Engine
    monkeypatch.setenv('MINDIE_DIAGNOSTICS_ROOT', str(tmp_path / 'diagnostics'))
    shared = write_settings(tmp_path / 'sharing.json', consent_choice='contribute', enabled=True,
                            repository='owner/repo', roots=[tmp_path])
    with closing(Store(tmp_path / 'store', 'test')) as store:
        engine = Engine(store, settings_path=shared.path)
        ticks = []
        def read_pending(**kwargs):
            ticks.append(1)
            if len(ticks) in {1, 2, 4}:
                raise OSError('synthetic receipt access failure')
            return []
        def next_tick(_):
            if len(ticks) == 5:
                engine.stop.set()
            return engine.stop.is_set()
        monkeypatch.setattr(store, 'outbox_unresolved', read_pending)
        monkeypatch.setattr(engine.stop, 'wait', next_tick)
        engine._outbox_loop()
        errors = [item for item in agent_diagnostics.pending()['items']
                  if item['code'] == 'publication_worker_failed']
        assert len(errors) == 1 and errors[0]['count'] == 2


def test_store_close_preserves_committed_effect_error_over_cleanup(tmp_path, monkeypatch):
    store = Store(tmp_path / 'store', 'test')
    close_materials = store.materials.close
    original = OSError('pointer promotion failed after metadata commit')
    original.metadata_committed = True
    def promotion_failed():
        raise original
    def index_close_failed():
        raise ValueError('derived cleanup failed')
    monkeypatch.setattr(store, '_finish_material_writes', promotion_failed)
    monkeypatch.setattr(store.materials, 'close', index_close_failed)
    try:
        with pytest.raises(OSError) as caught:
            store.close()
        assert caught.value is original and caught.value.metadata_committed
        assert 'ValueError' in ' '.join(caught.value.__notes__)
        with pytest.raises(sqlite3.ProgrammingError):
            store.db.execute('SELECT 1')
    finally:
        close_materials()  # The intentionally failed cleanup must not leak handles.


def test_reconciliation_read_failure_is_delivered_without_overwriting_original_intent(tmp_path, monkeypatch):
    from mindie_knowledge.loop import agent_diagnostics
    from mindie_knowledge.loop.engine import Engine
    from conftest import write_settings
    from test_publication_authority import prepare
    monkeypatch.setenv('MINDIE_DIAGNOSTICS_ROOT', str(tmp_path / 'diagnostics'))
    settings = write_settings(tmp_path / 'sharing.json', consent_choice="contribute", roots=[tmp_path])
    with closing(Store(tmp_path / 'store', 'test')) as store:
        _, batch_id = prepare(store, settings, 'Synthetic uncertain publication.')
        store.mark_batch(batch_id, 'unknown', attempted=True, detail='original push outcome unknown')
        agent_diagnostics.acknowledge(agent_diagnostics.pending())
        original = store.batch(batch_id)
        engine = Engine(store, settings_path=settings.path)
        def unavailable(*args, **kwargs):
            raise OSError('synthetic read failure')
        engine.community['reconcile_batch'] = unavailable
        for _ in range(2):
            engine._reconcile(original)
        assert store.batch(batch_id) == original
        faults = agent_diagnostics.pending()['items']
        assert [(item['stage'], item['code'], item['count']) for item in faults] == [
            ('reconcile', 'publication_reconciliation_failed', 1)]
        engine.community['reconcile_batch'] = lambda *a, **kw: dict(status='unknown', detail='read succeeded; no proof')
        engine._reconcile(original)
        engine.community['reconcile_batch'] = unavailable
        engine._reconcile(store.batch(batch_id))
        assert agent_diagnostics.pending()['items'][0]['count'] == 2


def test_reconciled_remote_result_survives_local_receipt_failure(tmp_path, monkeypatch):
    from mindie_knowledge.loop.engine import Engine
    from conftest import write_settings
    from test_publication_authority import prepare
    settings = write_settings(tmp_path / 'sharing.json', consent_choice="contribute", roots=[tmp_path])
    with closing(Store(tmp_path / 'store', 'test')) as store:
        _, batch_id = prepare(store, settings, 'Synthetic publication with remote proof.')
        store.mark_batch(batch_id, 'unknown', attempted=True, detail='original push outcome unknown')
        original = store.batch(batch_id)
        engine = Engine(store, settings_path=settings.path)
        receipt = dict(status='submitted', batch_id=batch_id, revision=original['revision'],
                       head_sha='a' * 40, pr_url='https://example.invalid/pull/1')
        engine.community['reconcile_batch'] = lambda *a, **kw: receipt
        error = OSError('synthetic local receipt failure')
        def fail_record(*a, **kw):
            raise error
        monkeypatch.setattr(store, 'mark_batch', fail_record)
        with pytest.raises(OSError) as caught:
            engine._reconcile(original)
        assert caught.value is error and caught.value.mindie_publication_result == receipt
        assert 'submitted' in ' '.join(caught.value.__notes__)
        assert store.batch(batch_id) == original


def test_frozen_payload_failure_delivers_its_stage_before_any_write(tmp_path, monkeypatch):
    from mindie_knowledge.loop import agent_diagnostics
    from mindie_knowledge.loop.engine import Engine
    from mindie_knowledge.materials.publication import staging_path
    from conftest import write_settings
    from test_publication_authority import prepare
    monkeypatch.setenv('MINDIE_DIAGNOSTICS_ROOT', str(tmp_path / 'diagnostics'))
    settings = write_settings(tmp_path / 'sharing.json', consent_choice="contribute", roots=[tmp_path])
    with closing(Store(tmp_path / 'store', 'test')) as store:
        _, batch_id = prepare(store, settings, 'Synthetic frozen contribution.')
        row = store.batch(batch_id)
        staging_path(store.root, batch_id, row['revision']).joinpath('manifest.json').unlink()
        engine = Engine(store, settings_path=settings.path)
        writes = []
        engine.community['submit_batch'] = lambda *a, **kw: writes.append(a)
        engine._submit(row)
        assert not writes and store.batch(batch_id)['status'] == 'unavailable'
        assert store.batch(batch_id)['attempted'] is None
        assert any(item['stage'] == 'payload' and item['code'] == 'publication_payload_unavailable'
                   for item in agent_diagnostics.pending()['items'])


def test_payload_error_remains_primary_when_failure_recording_also_fails(tmp_path, monkeypatch):
    from mindie_knowledge.loop.engine import Engine
    from mindie_knowledge.materials import publication
    from conftest import write_settings
    from test_publication_authority import prepare
    settings = write_settings(tmp_path / 'sharing.json', consent_choice="contribute", roots=[tmp_path])
    with closing(Store(tmp_path / 'store', 'test')) as store:
        _, batch_id = prepare(store, settings, 'Synthetic local failure before writing.')
        original = store.batch(batch_id)
        engine = Engine(store, settings_path=settings.path)
        primary, secondary = FileNotFoundError('frozen payload missing'), OSError('receipt unavailable')
        def fail_payload(*a):
            raise primary
        def fail_record(*a, **kw):
            raise secondary
        monkeypatch.setattr(publication, 'load_batch_payload', fail_payload)
        monkeypatch.setattr(store, 'mark_batch', fail_record)
        with pytest.raises(FileNotFoundError) as caught:
            engine._submit(original)
        assert caught.value is primary and caught.value.__cause__ is secondary
        assert 'payload failure receipt also failed: OSError' in caught.value.__notes__
        assert store.batch(batch_id) == original
