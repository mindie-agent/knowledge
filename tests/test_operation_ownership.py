"""Ownership and transaction invariants, without latency performance thresholds."""
from contextlib import closing
import sqlite3

import pytest

from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.operations import CONTRIBUTION, OperationOwner
from mindie_knowledge.loop.store import Store
from test_publication_authority import prepare
from conftest import write_settings


def test_revocation_cancels_only_foreign_contribution_and_never_revives_old_tokens():
    import threading
    owner = OperationOwner(threading.Event())
    old = [owner.begin(kind, kind, 'old') for kind in CONTRIBUTION]
    current = [owner.begin(kind, kind, 'current') for kind in CONTRIBUTION]
    read = owner.begin('reconcile', 'uncertain-original-intent')
    capability = owner.begin('capability', 'independent-query')
    owner.revoke(keep_generation='current')
    assert all(operation.cancel.is_set() for operation in old)
    assert not any(operation.cancel.is_set() for operation in [*current, read, capability])
    owner.revoke()
    later = owner.begin('capture', 'subsequent-increment', 'next')
    assert all(operation.cancel.is_set() for operation in [*old, *current])
    assert not any(operation.cancel.is_set() for operation in [later, read, capability])
    assert owner.freeze() == len(old) + len(current) + 3
    assert owner.begin('index', 'cannot-enter-while-frozen', 'next') is None
    owner.shutdown()
    assert all(operation.cancel.is_set() for operation in [later, read, capability])
    for operation in [*old, *current, later, read, capability]:
        owner.end(operation)
    assert owner.snapshot()['activity'] == 0


def test_feedback_revocation_failure_rolls_back_batch_revocation(tmp_path):
    settings = write_settings(tmp_path / 'sharing.json', roots=[tmp_path])
    with closing(Store(tmp_path / 'store', 'test')) as store:
        doc, batch = prepare(store, settings, 'Synthetic unsent material.')
        store.record_vote(root_hash='b' * 64, ref=store.ref(doc['entry_id'], doc['revision']),
                          rating='up', reason='', publishable=True, generation=settings.generation)
        with store._write_txn():
            store.db.execute("CREATE TEMP TRIGGER reject_vote_revocation BEFORE UPDATE OF publishable ON votes "
                             "BEGIN SELECT RAISE(ABORT, 'synthetic feedback receipt failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match='synthetic feedback receipt failure'):
            store.revoke_unsent('sharing disabled')
        assert store.batch(batch)['status'] == 'pending'
        assert len(store.unbatched_votes(generation=settings.generation)) == 1


def test_idle_check_failure_reports_error_and_releases_admission(tmp_path, monkeypatch):
    with closing(Store(tmp_path / 'store', 'test')) as store:
        engine = Engine(store)
        error = OSError('synthetic required queue read failure')
        monkeypatch.setattr(store, 'due_capture', lambda: (_ for _ in ()).throw(error))
        with pytest.raises(OSError) as failure:
            engine.stop_if_idle()
        assert failure.value is error
        with engine.operations.scope('reconcile', 'existing-intent') as operation:
            assert operation is not None
