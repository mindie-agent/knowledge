"""Development resets are repeatable; release makes persisted state durable."""
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from mindie_knowledge import state_layout
from mindie_knowledge.loop.store import Store


def draft(store, title):
    return store.create_draft(kind='experience', title=title, summary='synthetic',
                              content='SYNTHETIC evidence that must survive compatible upgrades')


def test_legacy_development_state_is_inert_and_format_changes_can_repeat(tmp_path, monkeypatch):
    legacy = tmp_path / 'demo'
    legacy.mkdir()
    old = legacy / 'state-v4.sqlite3'
    old.write_bytes(b'opaque old development database')
    retained = []
    for version in (1, 2, 3):
        monkeypatch.setattr(state_layout, 'FORMAT', version)
        with closing(Store(tmp_path, 'demo')) as store:
            assert store.status()['entries'] == {}
            draft(store, f'format {version}')
            retained.append(store.root)
    assert old.read_bytes() == b'opaque old development database'
    assert all((root / state_layout.DATABASE).is_file() for root in retained)
    assert all(list((root / 'materials').rglob('*.md')) for root in retained)
    assert json.loads((legacy / 'state-layout.json').read_text())['release_version'] is None


def test_compatible_release_upgrade_reuses_data_and_never_unseals(tmp_path, monkeypatch):
    with closing(Store(tmp_path, 'demo')) as store:
        draft(store, 'keep this material')
        root = store.root
        with store.db:
            store.db.execute('INSERT INTO state VALUES(?,?)', ('synthetic-cursor', '1234'))
            store.db.execute('INSERT INTO feed_state VALUES(?,?)', ('synthetic-feed', '{"commit":"known"}'))
    bodies = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*.md')}
    for release in ('1.0.0', '1.1.0', None):
        monkeypatch.setattr(state_layout, 'RELEASE_VERSION', release)
        with closing(Store(tmp_path, 'demo')) as store:
            assert store.root == root
            assert store.status()['entries'] == {'draft': 1}
            assert store.db.execute('SELECT value FROM state WHERE key=?', ('synthetic-cursor',)).fetchone()[0] == '1234'
            assert store.feed_get('synthetic-feed') == {'commit': 'known'}
        assert {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*.md')} == bodies
    assert json.loads((tmp_path / 'demo/state-layout.json').read_text())['release_version'] == '1.1.0'
    monkeypatch.setattr(state_layout, 'FORMAT', 2)
    before = (root / state_layout.DATABASE).read_bytes()
    with pytest.raises(ValueError, match='released.*migration'):
        Store(tmp_path, 'demo')
    assert (root / state_layout.DATABASE).read_bytes() == before
    assert not (tmp_path / 'demo/state-v2').exists()


def test_release_upgrade_preserves_paid_and_uncertain_publication_receipts(tmp_path, monkeypatch):
    from mindie_knowledge.materials.summarizer import SummaryLedger, SummaryConflict
    from mindie_knowledge.community.ledger import Ledger
    from test_material_summarizer import request, returned
    monkeypatch.setattr(state_layout, 'RELEASE_VERSION', '1.0.0')
    with closing(Store(tmp_path, 'demo')) as store:
        root = store.root
        with store._write_txn():
            ledger = SummaryLedger(store.db)
            req = request('K3-04')
            paid = ledger.prepare(req)
            ledger.claim(paid['attempt_id'])
            response = returned(req)
            ledger.record(paid['attempt_id'], response)
            uncertain = ledger.prepare(request('K3-03'))
            ledger.claim(uncertain['attempt_id'])
            ledger.recover()
        with closing(Ledger(root / 'outbox')) as publisher:
            publisher.record_intent(batch_id='fixture', revision='a' * 64, domain='demo',
                                    repository='owner/repo', branch='fixture')
            publisher.finish_publication('fixture', 'a' * 64, status='unknown')
    monkeypatch.setattr(state_layout, 'RELEASE_VERSION', '1.1.0')
    with closing(Store(tmp_path, 'demo')) as store:
        with store._write_txn():
            ledger = SummaryLedger(store.db)
            assert json.loads(ledger.get(paid['attempt_id'])['response']) == response
            assert ledger.prepare(req)['attempt_id'] == paid['attempt_id']
            assert ledger.get(uncertain['attempt_id'])['status'] == 'outcome_unknown'
            for attempt in (paid, uncertain):
                with pytest.raises(SummaryConflict):
                    ledger.claim(attempt['attempt_id'])
        with closing(Ledger(store.root / 'outbox')) as publisher:
            publisher.record_intent(batch_id='fixture', revision='a' * 64, domain='demo',
                                    repository='owner/repo', branch='fixture')
            assert publisher.get_publication('fixture', 'a' * 64)['status'] == 'unknown'


@pytest.mark.parametrize('damage', ['missing', 'invalid', 'missing-database'])
def test_lost_layout_cannot_be_treated_as_first_use(tmp_path, monkeypatch, damage):
    monkeypatch.setattr(state_layout, 'RELEASE_VERSION', '1.0.0')
    with closing(Store(tmp_path, 'demo')) as store:
        draft(store, 'preserved')
        root = store.root
    layout = tmp_path / 'demo/state-layout.json'
    if damage == 'missing':
        layout.unlink()
    elif damage == 'invalid':
        layout.write_text('{}')
    else:
        (root / state_layout.DATABASE).unlink()
    monkeypatch.setattr(state_layout, 'FORMAT', 2)
    with pytest.raises(ValueError, match='layout|initialized'):
        Store(tmp_path, 'demo')
    assert list(root.rglob('*.md'))
    assert not (tmp_path / 'demo/state-v2').exists()


def test_compatibility_check_is_read_only(tmp_path, monkeypatch):
    assert state_layout.state_root(tmp_path, 'demo') == tmp_path / 'demo/state-v1'
    assert not list(tmp_path.iterdir())
    monkeypatch.setattr(state_layout, 'RELEASE_VERSION', '1.0.0')
    Store(tmp_path, 'demo').close()
    layout = tmp_path / 'demo/state-layout.json'
    before = layout.read_bytes()
    monkeypatch.setattr(state_layout, 'RELEASE_VERSION', '1.1.0')
    assert state_layout.state_root(tmp_path, 'demo').is_dir()
    assert layout.read_bytes() == before


def test_layout_publication_failure_remains_visible_and_preserves_initialized_data(tmp_path, monkeypatch):
    from mindie_knowledge import markdown
    def fail(*args):
        raise OSError('synthetic layout publication failure')
    with monkeypatch.context() as patch:
        patch.setattr(markdown, '_atomic_write_text', fail)
        with pytest.raises(OSError, match='publication failure'):
            Store(tmp_path, 'demo')
    assert (tmp_path / 'demo/state-v1' / state_layout.DATABASE).is_file()
    with pytest.raises(ValueError, match='layout is missing'):
        Store(tmp_path, 'demo')


def test_layout_write_error_survives_later_cleanup_error(tmp_path, monkeypatch):
    from pathlib import Path
    from mindie_knowledge import markdown
    def replace(*args):
        raise OSError('primary layout write failure')
    def unlink(*args, **kwargs):
        raise OSError('secondary cleanup failure')
    monkeypatch.setattr(markdown.os, 'replace', replace)
    monkeypatch.setattr(Path, 'unlink', unlink)
    with pytest.raises(OSError, match='primary layout write failure') as caught:
        markdown._atomic_write_text(tmp_path / 'layout.json', '{}')
    assert any('cleanup' in note for note in caught.value.__notes__)


def test_concurrent_first_open_waits_for_layout_publication(tmp_path):
    first = '''import sys
from mindie_knowledge.loop.store import Store
open_state = Store._open_state
def held(self, *args):
    open_state(self, *args)
    print('database-ready', flush=True)
    sys.stdin.readline()
Store._open_state = held
Store(sys.argv[1], 'demo').close()
'''
    second = '''import sys
from mindie_knowledge.loop.store import Store
from mindie_knowledge.loop.locks import StartLock
acquire = StartLock.acquire
def observed(self, **kwargs):
    if self.path.name == 'state-layout.lock':
        print('lock-attempt', flush=True)
    return acquire(self, **kwargs)
StartLock.acquire = observed
Store(sys.argv[1], 'demo').close()
'''
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    processes = []
    try:
        a = subprocess.Popen([sys.executable, '-c', first, str(tmp_path)], env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(a)
        assert a.stdout.readline().strip() == 'database-ready'
        b = subprocess.Popen([sys.executable, '-c', second, str(tmp_path)], env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(b)
        assert b.stdout.readline().strip() == 'lock-attempt'
        _, error = a.communicate('release\n', timeout=10)
        assert a.returncode == 0, error
        _, error = b.communicate(timeout=10)
        assert b.returncode == 0, error
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
