"""Resource contracts use work counts, not flaky wall-clock limits."""
import time
from contextlib import closing

import pytest

from mindie_knowledge.loop.budget import MaintenanceBudget
from mindie_knowledge.loop.documents import render_entry
from mindie_knowledge.loop.store import Store
from mindie_knowledge.loop.transcript_capture import fallback_header
from mindie_knowledge.retrieval import index_text, tokens


def draft(store, generation='allowed'):
    return store.create_draft(kind='experience', title='Measured case', summary='Observation',
                              content='Public observation 测量成功. ' * 3000, generation=generation,
                              owner='a' * 64)


def test_idle_eligibility_does_not_read_bodies_and_keeps_consent(store, monkeypatch):
    doc = draft(store)
    assert store.drafts_changed(generation='allowed') == [doc]
    monkeypatch.setattr(store, '_revision_doc', lambda *a: pytest.fail('idle poll loaded a body'))
    assert store.has_changed_drafts(generation='allowed')
    assert not store.has_changed_drafts(generation='other')
    store.quarantine_entry(doc['entry_id'], kind='content-scan', detail='held')
    assert not store.has_changed_drafts(generation='allowed')
    assert not store.has_changed_drafts()


def test_retry_limit_applies_after_due_filter_and_cancel_preserves_uncertain_receipt(store):
    for i in range(5):
        store.create_batch(batch_id=str(i), revision=str(i), batch={'body': 'x' * 100000},
                           entry_ids=[], vote_keys=[])
        if i < 4:
            store.mark_batch(str(i), 'unknown', attempted=True)
    with store.db:
        store.db.execute('UPDATE outbox SET next_attempt=? WHERE batch_id IN (\'0\',\'1\')',
                         (time.time() + 3600,))
    assert {row['batch_id'] for row in store.outbox_unresolved(limit=2, due_only=True)} == {'2', '3'}
    assert store.cancel_pending('revoked') == 1
    assert store.batch('4')['status'] == 'disabled'
    assert all(store.batch(str(i))['status'] == 'unknown' for i in range(4))


def test_latest_draft_survives_restart_without_mirror_or_history(tmp_path):
    root = tmp_path / 'store'
    with closing(Store(root, 'test')) as store:
        original = draft(store)
        current, _ = store.append_observation(original['entry_id'], 'New public observation.',
                                             marker='b' * 32, producer='a' * 64, generation='allowed')
        old_ref = store.ref(original['entry_id'], original['revision'])
        new_ref = store.ref(current['entry_id'], current['revision'])
        expected = render_entry(current)
        assert not list((root / 'drafts').glob('*.md'))
    with closing(Store(root, 'test')) as reopened:
        with pytest.raises(ValueError, match='unknown pinned revision'):
            reopened.get(old_ref)
        restored = reopened.get(new_ref)
        assert restored.pop('withdrawn') is False
        assert render_entry(restored) == expected


def test_poll_work_does_not_scale_with_completed_history(store):
    MaintenanceBudget(store)
    with store.db:
        store.db.executemany(
            "INSERT INTO captures(id,root_session,session,turn,summary,status,detail,created) "
            "VALUES(?,'r','s','t','','organized','',0)", ((str(i),) for i in range(20000)))
        store.db.executemany("INSERT INTO transcript_tasks "
                             "(task_key,entry_id,capture_id,body_digest,summary_status,summary_detail,updated,summary_due) "
                             "VALUES(?,?,'c','d','complete','',0,0)",
                             ((str(i), str(i)) for i in range(20000)))
        store.db.executemany("INSERT INTO maintenance_attempts(id,session,role,started,status) "
                             "VALUES(?,'s','organize',0,'succeeded')", ((str(i),) for i in range(20000)))
    # SQLite's VM instructions count actual query work, independent of hardware.
    steps = []
    store.db.set_progress_handler(lambda: steps.append(1) or 0, 100)
    try:
        assert store.due_capture() is None
        assert store.due_application() is None
        assert store.db.execute("SELECT * FROM transcript_tasks WHERE summary_status='pending' "
                                "AND summary_due<=? ORDER BY updated LIMIT 1", (time.time(),)).fetchone() is None
    finally:
        store.db.set_progress_handler(None, 0)
    assert len(steps) < 20, 'idle queries traversed completed history'


def test_token_stream_keeps_order_aliases_and_large_identifiers():
    phrase = 'torch_npu.npu_rms_norm 2.10.0+cpu 显存泄漏'
    expected = ['torch_npu.npu_rms_norm', 'torch_npu', 'torch', 'npu',
                'npu_rms_norm', 'rms', 'norm', '2.10.0+cpu', 'cpu', '显存', '存泄', '泄漏']
    assert tokens(phrase) == expected
    assert index_text((phrase + ' ') * 1000) == ' '.join(expected * 1000)
    long_name = 'namespace.' + 'a' * 20000
    assert long_name in tokens(long_name)
    assert 'a' * 20000 in tokens(long_name)


@pytest.mark.parametrize('body', ['  \n### User\r\n公开说明\u2028Final answer\n',
                                '字' * 100000, '### Heading\n' * 10000 + 'Last line',
                                'a' * 1498 + '\n字后文', ''],
                         ids=['unicode-lines', 'large-line', 'headers', 'utf8-boundary', 'empty'])
def test_fallback_excerpt_matches_original_format(body):
    lines = [s.strip() for s in body.splitlines() if s.strip() and not s.startswith('### ')]
    excerpt = lambda s, n: s.encode()[:n].decode('utf-8', 'ignore').strip()
    assert fallback_header(body) == (excerpt(lines[0] if lines else 'Public conversation', 240)[:120],
                                     'Conversation excerpt: ' + excerpt('\n'.join(lines), 1500))


def test_existing_feed_history_is_pruned_on_upgrade(tmp_path):
    from mindie_knowledge.loop.documents import make_entry
    root = tmp_path / 'upgrade'
    first = make_entry(entry_id='f' * 64, domain='test', kind='experience',
                       title='Old version', summary='old summary', content='old body canary')
    latest = make_entry(entry_id='f' * 64, domain='test', kind='experience',
                        title='Current version', summary='current summary', content='current body')
    with closing(Store(root, 'test')) as store:
        store.install_feed([latest], feed_ident='test')
        # The previous runtime retained every feed revision and only had the
        # draft-only migration marker. Recreate that on-disk schema/state.
        with store._write_txn():
            store._insert_revision(first, 'feed', time.time())
            store.db.execute("DELETE FROM meta WHERE key='latest-body-only'")
            store.db.execute("INSERT OR REPLACE INTO meta VALUES('latest-draft-only','1')")
        mirrors = store.root / 'drafts'
        mirrors.mkdir(exist_ok=True)
        (mirrors / (first['entry_id'] + '.md')).write_text('obsolete mirror body')
    with closing(Store(root, 'test')) as store:
        assert store.get(store.ref(latest['entry_id']))['content'] == 'current body'
        assert store.db.execute('SELECT count(*) FROM revisions').fetchone()[0] == 1
        assert not list((store.root / 'drafts').glob('*.md'))
        with pytest.raises(ValueError, match='unknown pinned revision'):
            store.get(store.ref(first['entry_id'], first['revision']))
    with closing(Store(root, 'test')) as store:
        assert store.db.execute('SELECT count(*) FROM revisions').fetchone()[0] == 1
