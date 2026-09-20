"""Real SQLite and privacy scanner: failed material stays consumed on restart."""
import pytest
from mindie_knowledge.loop.store import Store
from mindie_knowledge.loop import settings
from mindie_knowledge.loop.export import build_batch


def test_rejected_material_does_not_rebuild_on_idle_or_restart(tmp_path):
    shared = settings.write(tmp_path / 'sharing.json', enabled=True,
                            repository='owner/repo', project_roots=[tmp_path])
    store = Store(tmp_path / 'data', 'test')
    doc = store.create_draft(kind='experience', title='Private endpoint',
                             summary='Endpoint observation',
                             content='Private endpoint: ' + '10.3.' + '5.7',
                             generation=shared.generation)
    with pytest.raises(ValueError, match='redaction scan'):
        build_batch(store, settings=shared)
    assert build_batch(store, settings=shared) is None
    assert store.status()['export_attempts'][0]['status'] == 'failed'
    assert store.status()['outbox'] == []
    assert not (store.root / 'outbox' / 'staging').exists()
    store.close()
    store = Store(tmp_path / 'data', 'test')
    assert build_batch(store, settings=shared) is None
    assert store.get(store.ref(doc['entry_id']))['content'] == doc['content']
    store.create_draft(kind='experience', title='New bounded observation',
                       summary='New material remains eligible', content='Public observation.',
                       generation=shared.generation)
    # New material gets one new attempt; the old unsafe body still blocks it.
    with pytest.raises(ValueError, match='redaction scan'):
        build_batch(store, settings=shared)
    assert build_batch(store, settings=shared) is None
    assert len(store.status()['export_attempts']) == 2
    store.close()
