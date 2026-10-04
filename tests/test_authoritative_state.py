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


def test_authority_marker_has_canonical_bytes_on_creation_and_adoption(tmp_path):
    from mindie_knowledge.owned_state import open_database
    path = tmp_path / 'fixture.sqlite3'
    marker = path.with_name(path.name + '.owner')
    def initialize(db):
        db.execute('CREATE TABLE fixture (key TEXT PRIMARY KEY)')
    for existing in (False, True):
        with closing(open_database(path, schema='fixture/1', required={'fixture': {'key'}},
                                   initialize=initialize)) as db:
            assert marker.read_bytes() == b'fixture/1\n'
            assert db.execute('SELECT * FROM fixture').fetchall() == []
        if not existing:
            marker.unlink()  # A valid existing database may acquire its marker.


@pytest.mark.parametrize('damage', ['missing', 'empty', 'missing_entries', 'missing_attempts',
                                   'missing_schema', 'wrong_schema', 'lost_rows', 'missing_marker_and_db'])
def test_runtime_damage_preserves_all_material_and_never_rebuilds(tmp_path, damage):
    with closing(Store(tmp_path, 'demo')) as store:
        store.create_draft(kind='experience', title='Recovery fixture', summary='Still uncertain',
                           content='SYNTHETIC full failure and correction must survive')
        root = store.root
    original = bodies(root)
    path = root / 'state-v4.sqlite3'
    if damage in {'missing', 'missing_marker_and_db'}:
        path.unlink()
        if damage == 'missing_marker_and_db':
            path.with_name(path.name + '.owner').unlink()
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


def _remove_constraints(path, table):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(f'ALTER TABLE {table} RENAME TO damaged_original')
        db.execute(f'CREATE TABLE {table} AS SELECT * FROM damaged_original')
        db.execute('DROP TABLE damaged_original')


@pytest.mark.parametrize('table', ['entries', 'material_summary_attempts', 'publication'])
def test_same_columns_without_authoritative_constraints_cannot_reopen(tmp_path, table):
    if table == 'publication':
        Ledger(tmp_path).close()
        path, reopen = tmp_path / LEDGER_NAME, lambda: Ledger(tmp_path)
    else:
        Store(tmp_path, 'demo').close()
        path, reopen = tmp_path / 'demo/state-v4.sqlite3', lambda: Store(tmp_path, 'demo')
    _remove_constraints(path, table)
    before = path.read_bytes()
    with pytest.raises(ValueError, match=table):
        reopen()
    assert path.read_bytes() == before


@pytest.mark.parametrize('kind', ['runtime', 'summary', 'publication'])
@pytest.mark.parametrize('damage', ['missing_db', 'replaced_db', 'missing_marker', 'replaced_marker', 'corrupt_marker', 'constraints'])
def test_cached_authority_cannot_read_or_authorize_effect_after_damage(tmp_path, kind, damage):
    if kind == 'publication':
        owner = Ledger(tmp_path)
        path, table = tmp_path / LEDGER_NAME, 'publication'
        owner.record_intent(batch_id='known', revision='a' * 64, domain='demo', repository='o/r', branch='known')
        read = lambda: owner.get_publication('known', 'a' * 64)
        authorize = lambda: owner.record_step('known', 'a' * 64, 'git:push')
    else:
        owner = Store(tmp_path, 'demo')
        path = owner.root / 'state-v4.sqlite3'
        if kind == 'summary':
            summary = SummaryLedger(owner.db)
            table = 'material_summary_attempts'
            read = lambda: summary.get('not-started')
            authorize = lambda: summary.claim('not-started')
        else:
            table = 'entries'
            read = lambda: owner.status()
            authorize = lambda: owner.create_draft(kind='experience', title='must not appear', summary='s', content='synthetic')
    marker = path.with_name(path.name + '.owner')
    try:
        closed = False
        if damage == 'missing_db':
            closed = damage_database(owner.db, path, read=read)
        elif damage == 'replaced_db':
            replacement = path.with_name('replacement.sqlite3')
            with closing(sqlite3.connect(replacement)) as copy:
                owner.db.backup(copy)
            closed = damage_database(owner.db, path, read=read, replacement=replacement)
        elif damage == 'missing_marker':
            marker.unlink()
        elif damage == 'replaced_marker':
            replacement = marker.with_name('replacement.owner')
            replacement.write_bytes(marker.read_bytes())
            os.replace(replacement, marker)
        elif damage == 'corrupt_marker':
            marker.write_text('wrong\n')
        else:
            _remove_constraints(path, table)
        # POSIX exercises cached public reads and effect intents after real
        # live-file damage. Windows prevents that damage; after the explicit
        # native protection check above, validate this same owner's guard.
        operations = (owner.db.assert_authority,) if closed else (read, authorize)
        for operation in operations:
            with pytest.raises((OSError, ValueError)) as caught:
                operation()
            if damage in {'missing_db', 'missing_marker'}:
                assert isinstance(caught.value, FileNotFoundError)
            else:
                assert isinstance(caught.value, ValueError)
    finally:
        owner.close()


def test_valid_additional_columns_do_not_look_like_authority_loss(tmp_path):
    with closing(Ledger(tmp_path)) as ledger:
        with closing(sqlite3.connect(tmp_path / LEDGER_NAME)) as other, other:
            other.execute('ALTER TABLE publication ADD COLUMN previous_optional_note TEXT')
        ledger.record_intent(batch_id='known', revision='a' * 64, domain='demo', repository='o/r', branch='known')
        assert ledger.get_publication('known', 'a' * 64)['status'] == 'intent'


def test_cached_runtime_rechecks_schema_identity_value(tmp_path):
    with closing(Store(tmp_path, 'demo')) as store:
        with closing(sqlite3.connect(store.root / 'state-v4.sqlite3')) as damaged, damaged:
            damaged.execute("UPDATE meta SET value='wrong' WHERE key='schema'")
        with pytest.raises(ValueError, match='runtime schema'):
            store.status()


def test_invalid_parent_is_not_a_never_initialized_read(tmp_path):
    from mindie_knowledge.owned_state import open_database
    parent = tmp_path / 'not-a-directory'
    parent.write_text('synthetic')
    with pytest.raises(NotADirectoryError):
        open_database(parent / 'state.sqlite3', schema='test/1', required={},
                      initialize=lambda db: None, initialize_missing=False)


def test_partial_index_with_same_shape_wrong_predicate_is_not_valid_state(tmp_path):
    with closing(Store(tmp_path, 'demo')) as store:
        store.create_draft(kind='experience', title='Preserved', summary='Uncertain', content='Synthetic full body')
        root = store.root
    original = bodies(root)
    path = root / 'state-v4.sqlite3'
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('DROP INDEX material_summary_window')
        db.execute("CREATE INDEX material_summary_window ON material_summary_attempts(created) WHERE status='prepared'")
    damaged = path.read_bytes()
    with pytest.raises(ValueError, match='material_summary_attempts'):
        Store(tmp_path, 'demo')
    assert path.read_bytes() == damaged and bodies(root) == original


def test_step_table_without_declared_autoincrement_preserves_receipts_and_fails(tmp_path):
    with closing(Ledger(tmp_path)) as ledger:
        ledger.record_intent(batch_id='known', revision='a' * 64, domain='demo', repository='o/r', branch='known')
        ledger.record_step('known', 'a' * 64, 'git:push')
    path = tmp_path / LEDGER_NAME
    with closing(sqlite3.connect(path)) as db, db:
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name='publication_step'").fetchone()[0]
        db.execute('ALTER TABLE publication_step RENAME TO original_step')
        db.execute(sql.replace('AUTOINCREMENT', ''))
        db.execute('INSERT INTO publication_step SELECT * FROM original_step')
        db.execute('DROP TABLE original_step')
    damaged = path.read_bytes()
    with pytest.raises(ValueError, match='publication_step'):
        Ledger(tmp_path)
    assert path.read_bytes() == damaged
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('SELECT step FROM publication_step').fetchone()[0] == 'git:push'
