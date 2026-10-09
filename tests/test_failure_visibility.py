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


@pytest.mark.parametrize('target_exists', [False, True])
def test_diagnostic_root_distinguishes_valid_and_dangling_directory_links(tmp_path, monkeypatch, target_exists):
    import os
    from mindie_knowledge.loop import agent_diagnostics
    target, link = tmp_path / 'target', tmp_path / 'link'
    if target_exists:
        target.mkdir()
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        if os.name == 'nt' and exc.winerror == 1314:
            pytest.skip('native directory symlinks require Windows privilege')
        raise
    monkeypatch.setenv('MINDIE_DIAGNOSTICS_ROOT', str(link / 'missing' / 'nested'))
    if target_exists:
        assert agent_diagnostics.pending() is None
        assert list(target.iterdir()) == []
    else:
        with pytest.raises(FileNotFoundError):
            agent_diagnostics.pending()
        assert not target.exists()
    assert link.is_symlink()


def test_unavailable_diagnostic_ancestors_preserve_original_missing_error(tmp_path, monkeypatch):
    from pathlib import Path
    from mindie_knowledge.loop import agent_diagnostics
    monkeypatch.setenv('MINDIE_DIAGNOSTICS_ROOT', str(tmp_path / 'unavailable'))
    path = agent_diagnostics.root() / 'agent-delivery.sqlite3'
    missing = FileNotFoundError('original projection path unavailable')
    original_lstat, original_stat = Path.lstat, Path.stat
    def unavailable_path(candidate, *args, **kwargs):
        if candidate == path:
            raise missing
        if candidate in path.parents:
            raise FileNotFoundError('ancestor root unavailable')
        return original_lstat(candidate, *args, **kwargs)
    def unavailable_ancestor(candidate, *args, **kwargs):
        if candidate in path.parents:
            raise FileNotFoundError('ancestor root unavailable')
        return original_stat(candidate, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', unavailable_path)
    monkeypatch.setattr(Path, 'stat', unavailable_ancestor)
    with pytest.raises(FileNotFoundError) as caught:
        agent_diagnostics.pending()
    assert caught.value is missing
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


def test_diagnostic_marker_has_canonical_bytes_on_creation_and_adoption(tmp_path, monkeypatch):
    from mindie_knowledge.loop import agent_diagnostics
    monkeypatch.setenv('MINDIE_DIAGNOSTICS_ROOT', str(tmp_path / 'diagnostics'))
    marker = agent_diagnostics.root() / 'agent-delivery.sqlite3.owner'
    for expected_count in (1, 2):
        agent_diagnostics.enqueue('mindie-knowledge', 'capture', 'projection', 'invalid_record',
                                  dict(logging_failed=True))
        assert marker.read_bytes() == b'mindie-agent-delivery/1\n'
        assert agent_diagnostics.pending()['items'][0]['count'] == expected_count
        if expected_count == 1:
            marker.unlink()


@pytest.mark.parametrize('damage', ['missing', 'empty', 'table'])
def test_diagnostic_delivery_damage_is_not_no_pending_incidents(tmp_path, monkeypatch, damage):
    from mindie_knowledge.loop import agent_diagnostics
    monkeypatch.setenv('MINDIE_DIAGNOSTICS_ROOT', str(tmp_path / 'diagnostics'))
    agent_diagnostics.enqueue('mindie-knowledge', 'capture', 'projection', 'invalid_record',
                              dict(logging_failed=True))
    path = agent_diagnostics.root() / 'agent-delivery.sqlite3'
    if damage == 'missing':
        path.unlink()
    elif damage == 'empty':
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
    from mindie_knowledge.loop import agent_diagnostics, settings
    from mindie_knowledge.loop.engine import Engine
    monkeypatch.setenv('MINDIE_DIAGNOSTICS_ROOT', str(tmp_path / 'diagnostics'))
    shared = settings.write(tmp_path / 'sharing.json', enabled=True,
                            repository='owner/repo', project_roots=[tmp_path])
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
