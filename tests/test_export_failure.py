"""Real SQLite and privacy scanner: content rejections stay quarantined to
the offending entry while transient local failures resume with backoff."""
import time

import pytest
from mindie_knowledge.loop.store import Store
from mindie_knowledge.loop import settings
from mindie_knowledge.loop.export import build_batch


def test_unsafe_material_is_quarantined_without_blocking_valid_material(tmp_path):
    shared = settings.write(tmp_path / 'sharing.json', enabled=True,
                            repository='owner/repo', project_roots=[tmp_path])
    store = Store(tmp_path / 'data', 'test')
    unsafe = store.create_draft(kind='experience', title='Private endpoint',
                                summary='Endpoint observation',
                                content='Private endpoint: ' + '10.3.' + '5.7',
                                generation=shared.generation)
    valid = store.create_draft(kind='experience', title='New bounded observation',
                               summary='New material remains eligible',
                               content='Public observation.',
                               generation=shared.generation)
    # The unsafe entry never fails a build for unrelated valid material: the
    # batch carries only the clean entry and is staged normally.
    built = build_batch(store, settings=shared)
    assert built is not None
    _batch_id, _revision, batch, entry_ids, _votes = built
    assert entry_ids == [valid['entry_id']]
    assert [f['path'] for f in batch['files']] == [f"cases/{valid['entry_id']}.md"]
    assert store.quarantined_entries() == {unsafe['entry_id']: 'content-scan'}
    # Nothing is rebuilt on idle ticks or after a restart: the quarantined
    # entry is out of the selection, not parked behind a failing fingerprint.
    assert build_batch(store, settings=shared) is None
    assert not any(
        'failed' == attempt.get('status')
        for attempt in store.status()['export_attempts']
    )
    store.close()
    store = Store(tmp_path / 'data', 'test')
    assert store.quarantined_entries() == {unsafe['entry_id']: 'content-scan'}
    assert store.drafts_changed(generation=shared.generation) == []
    assert build_batch(store, settings=shared) is None
    # The quarantined content stays local and readable; it is never deleted.
    assert store.get(store.ref(unsafe['entry_id']))['content'] == unsafe['content']
    store.close()


def test_all_unsafe_material_quarantines_without_a_batch(tmp_path):
    shared = settings.write(tmp_path / 'sharing.json', enabled=True,
                            repository='owner/repo', project_roots=[tmp_path])
    store = Store(tmp_path / 'data', 'test')
    doc = store.create_draft(kind='experience', title='Private endpoint',
                             summary='Endpoint observation',
                             content='Private endpoint: ' + '10.3.' + '5.7',
                             generation=shared.generation)
    assert build_batch(store, settings=shared) is None
    # The rejection is quarantined to the entry and visible — the fingerprint
    # itself never needs a failing record to suppress retries.
    assert store.quarantined_entries() == {doc['entry_id']: 'content-scan'}
    assert store.status()['outbox'] == []
    assert not (store.root / 'outbox' / 'staging').exists()
    store.close()
    store = Store(tmp_path / 'data', 'test')
    # A content rejection is never retried automatically: the entry stays out
    # of the selection across restarts and no new build is attempted.
    assert build_batch(store, settings=shared) is None
    assert store.status()['export_attempts'] == []
    assert store.drafts_changed(generation=shared.generation) == []
    store.close()


def test_transient_staging_failure_resumes_with_backoff(tmp_path, monkeypatch):
    shared = settings.write(tmp_path / 'sharing.json', enabled=True,
                            repository='owner/repo', project_roots=[tmp_path])
    store = Store(tmp_path / 'data', 'test')
    doc = store.create_draft(kind='experience', title='Unblocked case',
                             summary='survives a disk hiccup',
                             content='Public observation.', generation=shared.generation)
    from mindie_knowledge.loop import export as export_mod

    calls = []
    real_write = export_mod._atomic_write_text

    def failing_write(target, text):
        calls.append(str(target))
        raise OSError('disk temporarily not writable')

    monkeypatch.setattr(export_mod, '_atomic_write_text', failing_write)
    with pytest.raises(OSError, match='disk temporarily not writable'):
        build_batch(store, settings=shared)
    attempt = store.status()['export_attempts'][0]
    assert attempt['status'] == 'failed' and attempt['class'] == 'transient'
    assert attempt['next_check'] > time.time()
    assert store.status()['outbox'] == []
    # While the backoff is due nothing rebuilds every idle tick...
    assert build_batch(store, settings=shared) is None
    monkeypatch.setattr(export_mod, '_atomic_write_text', real_write)
    assert build_batch(store, settings=shared) is None  # still backing off
    # ...and once the local problem is fixed and the check is due, the exact
    # same material is staged without any organizer replay or cursor reset.
    with store._write_txn():
        import json as _json
        key = next(k for k, in store.db.execute(
            "SELECT key FROM state WHERE key LIKE 'export:%'"))
        record = _json.loads(store.db.execute(
            "SELECT value FROM state WHERE key=?", (key,)).fetchone()[0])
        record['next_check'] = time.time() - 1
        store.db.execute("UPDATE state SET value=? WHERE key=?",
                         (_json.dumps(record), key))
    store.close()
    store = Store(tmp_path / 'data', 'test')  # restart: backoff state persisted
    rebuilt = build_batch(store, settings=shared)
    assert rebuilt is not None and rebuilt[3] == [doc['entry_id']]
    assert store.status()['export_attempts'][0]['status'] == 'staged'
    assert store.drafts_changed(generation=shared.generation) == []
    store.close()


def test_interrupted_export_reservation_is_not_a_permanent_lock(tmp_path):
    """A crash between reserve and finish leaves an ``attempted`` record with
    no outbox batch and no remote write; the build resumes after recovery."""
    shared = settings.write(tmp_path / 'sharing.json', enabled=True,
                            repository='owner/repo', project_roots=[tmp_path])
    store = Store(tmp_path / 'data', 'test')
    doc = store.create_draft(kind='experience', title='Crash window case',
                             summary='interrupted before staging',
                             content='Public observation.', generation=shared.generation)
    drafts = store.drafts_changed(generation=shared.generation)
    from mindie_knowledge.loop.store import digest
    fingerprint = digest([shared.generation,
                          sorted((d['entry_id'], d['revision']) for d in drafts),
                          []])
    # Simulate the pre-recovery crash window: reserved, never finished, and
    # (legacy) no next_check recorded at all.
    assert store.reserve_export(fingerprint)
    with store._write_txn():
        store.db.execute("UPDATE state SET value=? WHERE key=?",
                         ('{"status": "attempted"}', 'export:' + fingerprint))
    store.close()
    store = Store(tmp_path / 'data', 'test')
    rebuilt = build_batch(store, settings=shared)
    assert rebuilt is not None and rebuilt[3] == [doc['entry_id']]
    assert store.batch(rebuilt[0])['status'] == 'pending'
    store.close()


def test_unavailable_retry_backoff_and_unknown_transition_resets(tmp_path):
    """Unavailable batches resubmit with persisted backoff and no exhaustion;
    a transition into unknown resets the counter so the first reconciliation
    is never skipped by an inherited count."""
    shared = settings.write(tmp_path / 'sharing.json', enabled=True,
                            repository='owner/repo', project_roots=[tmp_path])
    store = Store(tmp_path / 'data', 'test')
    store.create_draft(kind='experience', title='Retry case', summary='s',
                       content='Public observation.', generation=shared.generation)
    built = build_batch(store, settings=shared)
    batch_id = built[0]
    store.mark_batch(batch_id, 'unavailable', attempted=True,
                     detail='git clone failed: could not resolve host')
    assert store.retry_due(batch_id)  # first resubmit is due immediately
    assert not store.retry_due(batch_id)  # then the persisted backoff applies
    row = store.batch(batch_id)
    assert row['reconciliations'] == 1 and row['next_attempt'] > time.time()
    # Simulate many prior backoffs: the counter keeps growing without ever
    # latching (and without overflowing the exponent).
    with store._write_txn():
        store.db.execute(
            "UPDATE outbox SET reconciliations=?, next_attempt=? WHERE batch_id=?",
            (2000, time.time() - 1, batch_id),
        )
    assert store.retry_due(batch_id)
    assert store.batch(batch_id)['next_attempt'] > time.time() + 3500  # hourly cap
    # A retried send whose outcome became uncertain: the transition resets
    # the attempt counter so reconciliation is not skipped by an inherited
    # count — but it never erases the already reserved retry floor.
    reserved_floor = store.batch(batch_id)['next_attempt']
    store.mark_batch(batch_id, 'unknown', detail='push outcome unknown')
    row = store.batch(batch_id)
    assert row['reconciliations'] == 0
    assert row['next_attempt'] == reserved_floor  # retained, not erased
    assert not store.reconcile_due(batch_id)  # the reserved floor still governs
    with store._write_txn():
        store.db.execute(
            "UPDATE outbox SET next_attempt=? WHERE batch_id=?",
            (time.time() - 1, batch_id),
        )
    assert store.reconcile_due(batch_id)
    row = store.batch(batch_id)
    assert row['reconciliations'] == 1  # fresh count after the reset
    assert not store.reconcile_due(batch_id)
    store.close()


def test_retry_after_receipt_raises_but_never_shortens_backoff(tmp_path):
    """A valid retry_at receipt hint raises the persisted next-attempt floor;
    bools, NaN/inf and past or smaller values are ignored."""
    shared = settings.write(tmp_path / 'sharing.json', enabled=True,
                            repository='owner/repo', project_roots=[tmp_path])
    store = Store(tmp_path / 'data', 'test')
    store.create_draft(kind='experience', title='Rate limited', summary='s',
                       content='Public observation.', generation=shared.generation)
    built = build_batch(store, settings=shared)
    batch_id = built[0]
    store.mark_batch(batch_id, 'unavailable', attempted=True, detail='HTTP 429')
    assert store.retry_due(batch_id)  # grants and sets the backoff floor
    floor = store.batch(batch_id)['next_attempt']
    now = time.time()
    assert floor > now
    # Invalid hints are rejected outright.
    for bad in (True, False, float('nan'), float('inf'), 'later', None):
        assert store.defer_operation_until(batch_id, bad) is False
    # A past hint never shortens the existing backoff.
    assert store.defer_operation_until(batch_id, now - 100)
    assert store.batch(batch_id)['next_attempt'] == floor
    # A smaller future hint does not shorten either; a later one raises it.
    assert store.defer_operation_until(batch_id, floor - 5)
    assert store.batch(batch_id)['next_attempt'] == floor
    assert store.defer_operation_until(batch_id, floor + 900)
    assert store.batch(batch_id)['next_attempt'] == floor + 900
    store.close()


def test_submit_and_reconcile_receipts_apply_retry_after_floor(tmp_path):
    """Both receipt paths (submit and reconcile) persist the Retry-After
    floor into the existing outbox due field; a transition and a smaller hint
    never shorten an already reserved floor."""
    shared = settings.write(tmp_path / 'sharing.json', enabled=True,
                            repository='owner/repo', project_roots=[tmp_path])
    store = Store(tmp_path / 'data', 'test')
    store.create_draft(kind='experience', title='Retry case', summary='s',
                       content='Public observation.', generation=shared.generation)
    built = build_batch(store, settings=shared)
    batch_id = built[0]
    retry_at = time.time() + 1800

    from mindie_knowledge.loop.engine import Engine
    engine = Engine(store, agent_command=None, settings_path=tmp_path / 'sharing.json')
    engine.community = {
        'submit_batch': lambda *a, **k: dict(status='unavailable', retry_at=retry_at),
        'reconcile_batch': lambda *a, **k: dict(status='unknown', retry_at=retry_at - 900),
    }
    engine._submit(store.batch(batch_id))
    assert store.batch(batch_id)['status'] == 'unavailable'
    assert store.batch(batch_id)['next_attempt'] == pytest.approx(retry_at)
    # The reconcile path applies the floor too; the smaller hint and the
    # unavailable->unknown transition keep the reserved floor.
    engine._reconcile(store.batch(batch_id))
    row = store.batch(batch_id)
    assert row['status'] == 'unknown'
    assert row['next_attempt'] == pytest.approx(retry_at)  # not shortened
    assert row['reconciliations'] == 0  # only the counter was reset
    store.close()


def test_single_platform_legal_record_passes_a_smaller_grouping_budget(tmp_path, monkeypatch):
    """The per-flush envelope is a soft multi-record grouping budget: a quote/
    backslash-heavy body whose JSON expansion exceeds the patched envelope
    still batches alone — never skipped forever, never an artificial cap."""
    import mindie_knowledge.community.common as common_mod
    from mindie_knowledge.loop import export as export_mod

    monkeypatch.setattr(common_mod, 'MAX_BATCH_BYTES', 5242)
    monkeypatch.setattr(common_mod, 'MAX_FILE_BYTES', 4096)
    monkeypatch.setattr(export_mod, 'MAX_BATCH_BYTES', 5242)
    monkeypatch.setattr(export_mod, 'MAX_FILE_BYTES', 4096)
    shared = settings.write(tmp_path / 'sharing.json', enabled=True,
                            repository='owner/repo', project_roots=[tmp_path])
    store = Store(tmp_path / 'data', 'test')
    heavy = store.create_draft(
        kind='experience', title='Quote heavy', summary='s',
        content='"\\' * 1400,
        generation=shared.generation)
    ordinary = store.create_draft(kind='experience', title='Ordinary', summary='s',
                                  content='plain body', generation=shared.generation)
    first = build_batch(store, settings=shared)
    assert first is not None  # one record is never stranded by the budget
    row = store.batch(first[0])
    assert row is not None and row['status'] == 'pending'
    assert first[3]  # something was actually staged
    assert not store.status()['export_attempts'] or \
        store.status()['export_attempts'][0]['status'] == 'staged'
    # The other valid record waits for the next automatic batch after this
    # one resolves: grouping budget, never a drop.
    remaining = [d['entry_id'] for d in store.drafts_changed(generation=shared.generation)]
    assert sorted(first[3] + remaining) == sorted(
        [heavy['entry_id'], ordinary['entry_id']])
    store.mark_batch(first[0], 'submitted', head_sha='a' * 40)
    second = build_batch(store, settings=shared)
    assert second is not None and not store.drafts_changed(generation=shared.generation)
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
