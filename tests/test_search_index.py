"""Actual pinned ReMe retrieval contracts over current Markdown packages."""
import json
from contextlib import closing
from pathlib import Path

import pytest

from mindie_knowledge.loop.store import Store
from mindie_knowledge.materials.references import MaterialReadError

PRODUCER = 'a' * 64


def draft(store, title='Operator check', content='torch_npu.npu_rms_norm(x, w) 显存泄漏 on 2.10.0+cpu', **kw):
    return store.create_draft(kind=kw.pop('kind', 'experience'), title=title,
                              summary='An observation, not acceptance.', content=content,
                              owner=PRODUCER, **kw)


def package(author, **kw):
    doc = draft(author, **kw)
    return author.materials.export_task(doc['entry_id'])


def test_actual_reme_recalls_qualified_apis_versions_and_chinese(store):
    wanted = draft(store)
    draft(store, title='Different operator', content='torch_npu.npu_sum(x) on 2.9')
    for query in ('torch_npu.npu_rms_norm', 'npu_rms_norm', 'rms_norm', '显存', '泄漏', '2.10.0+cpu'):
        response = store.query(query)
        assert response['retrieval'] == 'reme-bm25'
        assert response['results'][0]['entry_id'] == wanted['entry_id'], query
    assert store.query('pu_rm')['results'] == []
    assert store.query('undocumented_word')['results'] == []
    scores = [r['score'] for r in store.query('torch_npu')['results']]
    assert scores == sorted(scores, reverse=True)


def test_conditions_filter_before_limit(store):
    for i in range(10):
        draft(store, kind='knowledge', title=f'Graph capture incompatible {i}',
              content='graph capture graph capture', conditions={'stack': 'new'})
    wanted = draft(store, kind='knowledge', title='Graph capture applicable',
                   content='graph capture', conditions={'stack': 'old'})
    hits = store.query('graph capture', limit=1, conditions={'stack': 'old'})['results']
    assert [x['entry_id'] for x in hits] == [wanted['entry_id']]


def test_feed_overlay_returns_published_revision_header_and_body(tmp_path):
    with closing(Store(tmp_path / 'author', 'test')) as author, closing(Store(tmp_path / 'reader', 'test')) as store:
        p = package(author, title='Published observation', content='calibration original')
        store.install_feed([p], feed_ident='f' * 64)
        doc = p['entry']
        store.restore_draft(doc['entry_id'], author._revision_doc(doc['entry_id'], doc['revision']))
        store.append_observation(doc['entry_id'], 'later unsent unverifiedclaim', marker='b' * 32,
                                 header={'title': 'Later draft header', 'summary': 'Local uncertainty'})
        hit = store.query('calibration')['results'][0]
        assert hit['title'] == 'Published observation'
        assert hit['revision'] == doc['revision'] and hit['supplemental']
        assert store.explain(hit['ref'])['content'] == 'calibration original'
        assert store.query('unverifiedclaim')['results'] == []


def test_withdrawal_removes_searchable_body_and_old_pinned_read(tmp_path):
    with closing(Store(tmp_path / 'author', 'test')) as author, closing(Store(tmp_path / 'reader', 'test')) as store:
        p = package(author, content='withdrawable body')
        store.install_feed([p], feed_ident='f' * 64)
        pinned = store.query('withdrawable')['results'][0]['ref']
        store.install_feed([], feed_ident='f' * 64)
        assert store.query('withdrawable')['results'] == []
        assert store.get(store.ref(p['task_id']))['withdrawn']
        with pytest.raises(ValueError, match='withdrawn'):
            store.explain(pinned)


def test_warm_queries_do_not_rechunk_unchanged_material(store, monkeypatch):
    from mindie_knowledge.materials.reme_index import MarkdownFileChunker
    doc = draft(store)
    store.query('rms_norm')
    original = MarkdownFileChunker.chunk
    changed = []
    async def traced(self, path, *args, **kwargs):
        changed.append(Path(path))
        return await original(self, path, *args, **kwargs)
    monkeypatch.setattr(MarkdownFileChunker, 'chunk', traced)
    store.query('rms_norm')
    store.query('显存')
    assert changed == []
    store.append_observation(doc['entry_id'], 'subsequent correction marker', marker='b' * 32)
    assert store.query('subsequent correction')['results']
    assert len(changed) == 1
    assert 'correction' in changed[0].read_text(encoding='utf-8')


def test_readiness_is_unverified_after_restart_until_actual_reme_load(tmp_path):
    root = tmp_path / 'store'
    with closing(Store(root, 'test')) as store:
        draft(store)
        assert store.search_index_status()['complete'] is False
        assert store.advance_search_index()['complete'] is True
    with closing(Store(root, 'test')) as store:
        assert store.search_index_status()['phase'] == 'checkpointed-unverified'
        assert store.query('rms_norm')['results']
        assert store.search_index_status()['complete'] is True


def test_corrupt_derived_snapshot_is_visible_and_rebuild_only_discards_cache(tmp_path):
    root = tmp_path / 'store'
    with closing(Store(root, 'test')) as store:
        doc = draft(store)
        store.query('rms_norm')
        files = {p: p.read_bytes() for p in store.materials.root.rglob('*.md')}
        stamp = store.materials.root / '.reme-index' / 'snapshot.json'
    stamp.write_text('{broken')
    with closing(Store(root, 'test')) as store:
        with pytest.raises(MaterialReadError) as caught:
            store.query('rms_norm')
        assert caught.value.code == 'material_corrupt'
        assert isinstance(caught.value.__cause__, ValueError)
        assert store.search_index_status()['phase'] == 'failed'
        assert stamp.read_text() == '{broken'
        store.materials.rebuild_index()
        assert store.query('rms_norm')['results'][0]['entry_id'] == doc['entry_id']
        assert {p: p.read_bytes() for p in files} == files


def test_metadata_database_has_no_material_body_or_second_lexical_index(store):
    doc = draft(store, content='unique-body-canary-for-file-authority')
    store.query('unique-body-canary-for-file-authority')
    tables = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {'search_map', 'search_fts', 'feed_staging'}.intersection(tables)
    for table in ('entries', 'revisions'):
        for row in store.db.execute(f'SELECT doc FROM {table}'):
            assert not json.loads(row[0]).get('content')
            assert 'unique-body-canary' not in row[0]
    assert 'unique-body-canary' in store.get(store.ref(doc['entry_id']))['content']


def test_current_pointer_mismatch_cannot_be_reported_as_empty_success(store):
    doc = draft(store)
    path = store.materials.root / 'current.json'
    path.write_text('{}')
    with pytest.raises(MaterialReadError) as caught:
        store.query('rms_norm')
    assert caught.value.code == 'material_corrupt'
    assert isinstance(caught.value.__cause__, ValueError)
    assert 'current material' in str(caught.value.__cause__)
