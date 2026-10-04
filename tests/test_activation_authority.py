"""Native association loss cannot authorize a fresh identity or repeated work."""
from contextlib import closing
import sqlite3
import time

import pytest

from mindie_knowledge.loop.activation import Admission, AdmissionUnavailable
from mindie_knowledge.loop.locks import StartLock
from mindie_knowledge.loop.store import session_key


def test_never_used_admission_reads_and_noop_writes_create_nothing(tmp_path):
    path = tmp_path / 'missing-parent' / 'admission.sqlite3'
    gate = Admission(path)
    assert gate.leases() == []
    assert gate.active_lease('native') is None
    assert gate.inspect('native')['status'] == 'missing'
    assert gate.capture_authorization('native', 'token', timeout=.1)['state'] == 'inactive'
    assert gate.resolve_capture_lease('native', timeout=.1)['state'] == 'inactive'
    assert gate.deactivate('native') is False
    with pytest.raises(ValueError, match='task binding is not active'):
        gate.claim('native', 'capture', 'once')
    with pytest.raises(ValueError, match='invalid activation token'):
        gate.finish('native', 'token', True)
    assert not path.parent.exists()


@pytest.mark.parametrize('damage', ['missing', 'empty', 'leases', 'attempts', 'constraints', 'marker'])
def test_lost_native_association_never_becomes_inactive_or_replays(tmp_path, damage):
    path = tmp_path / 'admission.sqlite3'
    original = Admission(path)
    lease = original.associate('native', project_root=str(tmp_path), not_before=12.0)
    assert original.claim('native', 'capture', 'once', lease['token']) is True
    marker = path.with_name(path.name + '.owner')
    if damage == 'missing':
        path.unlink()
    elif damage == 'empty':
        path.write_bytes(b'')
    elif damage == 'marker':
        marker.write_text('corrupt\n')
    else:
        with closing(sqlite3.connect(path)) as db, db:
            if damage == 'constraints':
                db.execute('ALTER TABLE attempts RENAME TO old_attempts')
                db.execute('CREATE TABLE attempts AS SELECT * FROM old_attempts')
                db.execute('DROP TABLE old_attempts')
            else:
                db.execute(f'DROP TABLE {damage}')
    before = path.read_bytes() if path.exists() else None
    marker_before = marker.read_bytes()
    # Both the long-lived gate and a new native event after restart must fail.
    for gate in (original, Admission(path)):
        reads = [gate.leases, lambda: gate.active_lease('native'),
                 lambda: gate.check('native', lease['token']), lambda: gate.resolve(lease['token']),
                 lambda: gate.allows_hash(session_key('native')), lambda: gate.scope_root('native'),
                 lambda: gate.capture_authorization('native', lease['token'], timeout=.1),
                 lambda: gate.resolve_capture_lease('native', timeout=.1)]
        writes = [lambda: gate.associate('native', project_root=str(tmp_path), not_before=12.0),
                  lambda: gate.activate('native', project_root=str(tmp_path)),
                  lambda: gate.claim('native', 'capture', 'once', lease['token']),
                  lambda: gate.finish('native', lease['token'], True), lambda: gate.deactivate('native')]
        for operation in reads + writes:
            with pytest.raises(AdmissionUnavailable):
                operation()
        assert gate.inspect('native')['status'] == 'unavailable'
    assert (path.read_bytes() if path.exists() else None) == before
    assert marker.read_bytes() == marker_before


def test_healthy_association_reuses_boundary_and_receipts_after_restart(tmp_path):
    path = tmp_path / 'admission.sqlite3'
    first = Admission(path)
    lease = first.associate('native', project_root=str(tmp_path), not_before=12.0)
    assert first.claim('native', 'capture', 'once', lease['token'])
    restarted = Admission(path)
    repeated = restarted.associate('native', project_root=str(tmp_path), not_before=999.0)
    assert repeated['token'] == lease['token']
    assert repeated['activated_at'] == 12.0
    assert not restarted.claim('native', 'capture', 'once', lease['token'])
    assert restarted.resolve_capture_lease('native', timeout=.1)['state'] == 'admitted'
    assert restarted.deactivate('native')
    with pytest.raises(ValueError, match='explicitly revoked'):
        restarted.associate('native', project_root=str(tmp_path), not_before=1000.0)


def test_native_stop_honors_caller_lock_timeout(tmp_path):
    path = tmp_path / 'admission.sqlite3'
    gate = Admission(path)
    lease = gate.associate('native', project_root=str(tmp_path), not_before=12.0)
    lock = StartLock(path.with_name(path.name + '.init.lock'))
    lock.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(AdmissionUnavailable):
            gate.capture_authorization('native', lease['token'], timeout=.05)
        assert time.monotonic() - started < 1.0
    finally:
        lock.release()
