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


@pytest.mark.parametrize("waiting", ["pending", "unknown", "unavailable"])
def test_waiting_for_old_write_does_not_consume_new_material(tmp_path, waiting):
    shared = settings.write(tmp_path / 'sharing.json', enabled=True,
                            repository='owner/repo', project_roots=[tmp_path])
    store = Store(tmp_path / 'data', 'test')
    store.create_draft(kind='experience', title='First', summary='First case',
                       content='First public observation.', generation=shared.generation)
    first = build_batch(store, settings=shared)
    store.mark_batch(first[0], waiting, attempted=waiting != 'pending')
    new = store.create_draft(kind='experience', title='Second', summary='Second case',
                             content='New public observation.', generation=shared.generation)
    attempts = len(store.status()['export_attempts'])
    assert build_batch(store, settings=shared) is None
    assert len(store.status()['export_attempts']) == attempts
    assert [d['entry_id'] for d in store.drafts_changed(generation=shared.generation)] == [new['entry_id']]
    store.close()
    store = Store(tmp_path / 'data', 'test')
    assert build_batch(store, settings=shared) is None  # durable wait, no rewrite
    store.mark_batch(first[0], 'submitted')  # original write now confirmed
    second = build_batch(store, settings=shared)
    assert second is not None and second[1] != first[1]
    assert new['entry_id'] in second[3]
    assert store.drafts_changed(generation=shared.generation) == []
    assert build_batch(store, settings=shared) is None
    store.close()
