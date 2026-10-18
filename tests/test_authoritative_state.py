"""Lost authority never deletes bodies or grants a second external effect."""
from contextlib import closing
import os
import sqlite3

import pytest

from mindie_knowledge.loop.store import Store
from mindie_knowledge.community.ledger import Ledger, LEDGER_NAME
from mindie_knowledge.materials.summarizer import SummaryLedger
from authority_support import damage_database


def bodies(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*.md')}


@pytest.mark.parametrize('damage', ['missing', 'empty', 'missing_entries', 'missing_attempts',
                                   'missing_schema', 'wrong_schema', 'lost_rows'])
def test_runtime_damage_preserves_all_material_and_never_rebuilds(tmp_path, damage):
    with closing(Store(tmp_path, 'demo')) as store:
        store.create_draft(kind='experience', title='Recovery fixture', summary='Still uncertain',
                           content='SYNTHETIC full failure and correction must survive')
        root = store.root
    original = bodies(root)
    path = root / 'state-v4.sqlite3'
    if damage == 'missing':
        path.unlink()
    elif damage == 'empty':
        path.write_bytes(b'')
    else:
        with closing(sqlite3.connect(path)) as db, db:
            db.execute({
                'missing_entries': 'DROP TABLE entries',
                'missing_attempts': 'DROP TABLE material_summary_attempts',
                'missing_schema': "DELETE FROM meta WHERE key='schema'",
                'wrong_schema': "UPDATE meta SET value='wrong' WHERE key='schema'",
                'lost_rows': 'DELETE FROM entries',
            }[damage])
    damaged = path.read_bytes() if path.exists() else None
    with pytest.raises(ValueError, match='state|metadata'):
        Store(tmp_path, 'demo')
    assert bodies(root) == original
    assert (path.read_bytes() if path.exists() else None) == damaged


@pytest.mark.parametrize('damage', ['missing', 'empty', 'missing_table'])
def test_unknown_publication_ledger_cannot_be_recreated(tmp_path, damage):
    with closing(Ledger(tmp_path)) as ledger:
        ledger.record_intent(batch_id='synthetic', revision='a' * 64, domain='demo',
                             repository='owner/repo', branch='branch')
        ledger.finish_publication('synthetic', 'a' * 64, status='unknown')
    path = tmp_path / LEDGER_NAME
    if damage == 'missing':
        path.unlink()
    elif damage == 'empty':
        path.write_bytes(b'')
    else:
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('DROP TABLE publication')
    damaged = path.read_bytes() if path.exists() else None
    with pytest.raises(ValueError, match='state'):
        Ledger(tmp_path)
    assert (path.read_bytes() if path.exists() else None) == damaged


def test_summary_reader_never_creates_a_lost_paid_attempt_table(tmp_path):
    with closing(Store(tmp_path, 'demo')) as store:
        with closing(sqlite3.connect(store.root / 'state-v4.sqlite3')) as damaged, damaged:
            damaged.execute('DROP TABLE material_summary_attempts')
        with pytest.raises(ValueError, match='material_summary_attempts'):
            SummaryLedger(store.db)
        with closing(sqlite3.connect(store.root / 'state-v4.sqlite3')) as damaged:
            assert not damaged.execute("SELECT 1 FROM sqlite_master WHERE name='material_summary_attempts'").fetchone()


@pytest.mark.parametrize('raw', ['[]', 'null', 'false', '"value"', '{broken'])
def test_corrupt_feed_receipt_is_not_fresh_discovery(tmp_path, raw):
    with closing(Store(tmp_path, 'demo')) as store:
        with store.db:
            store.db.execute('INSERT INTO feed_state VALUES(?,?)', ('feed-candidate:fixture', raw))
        with pytest.raises(ValueError, match='feed state'):
            store.feed_get('feed-candidate:fixture')


def test_valid_additional_columns_do_not_look_like_authority_loss(tmp_path):
    with closing(Ledger(tmp_path)) as ledger:
        with closing(sqlite3.connect(tmp_path / LEDGER_NAME)) as other, other:
            other.execute('ALTER TABLE publication ADD COLUMN previous_optional_note TEXT')
        ledger.record_intent(batch_id='known', revision='a' * 64, domain='demo', repository='o/r', branch='known')
        assert ledger.get_publication('known', 'a' * 64)['status'] == 'intent'


@pytest.mark.parametrize('suffix', ['state.sqlite3', 'missing/nested/state.sqlite3'])
def test_invalid_parent_is_not_a_never_initialized_read(tmp_path, suffix):
    from mindie_knowledge.owned_state import open_database
    parent = tmp_path / 'not-a-directory'
    parent.write_text('synthetic')
    with pytest.raises(NotADirectoryError):
        open_database(parent / suffix, schema='test/1', required={},
                      initialize=lambda db: None, initialize_missing=False)
    assert parent.read_text() == 'synthetic'


def test_never_initialized_nested_read_does_not_create_directories(tmp_path):
    from mindie_knowledge.owned_state import open_database
    parent = tmp_path / 'missing' / 'nested'
    assert open_database(parent / 'state.sqlite3', schema='test/1', required={},
                         initialize=lambda db: None, initialize_missing=False) is None
    assert list(tmp_path.iterdir()) == []

