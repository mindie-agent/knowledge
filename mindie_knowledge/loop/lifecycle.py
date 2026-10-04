"""Retire one exact service configuration before replacing its owner.

The receipt lives beside the configuration. It bars already-spawned wake
helpers as well as new starters; absence of a listener alone is never an
admission decision. The startup and admission OS locks make that decision
atomic with service ownership. No elapsed-time deadline governs useful work.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

from mindie_knowledge.markdown import _atomic_write_text

from .locks import StartInProgress, StartLock, lock_held

SCHEMA = 'mindie-service-retirement/1'


class ServiceRetired(RuntimeError):
    pass


def config_fingerprint(config_path, config=None):
    from .cli import config_at
    value = config_at(config_path) if config is None else config
    data = dict(path=str(Path(config_path).resolve()), config=value)
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _marker(config_path):
    return Path(config_path).with_name(Path(config_path).name + '.retired')


def inspect_retirement(config_path, config=None):
    from .diagnostics import _bytes
    fingerprint = config_fingerprint(config_path, config)
    try:
        value = json.loads(_bytes(_marker(config_path)))
    except FileNotFoundError:
        return None
    if (not isinstance(value, dict) or value.get('schema') != SCHEMA
            or value.get('status') not in {'retiring', 'retired'}
            or not isinstance(value.get('operation_id'), str)
            or len(value['operation_id']) != 32
            or not isinstance(value.get('config_fingerprint'), str)
            or len(value['config_fingerprint']) != 64):
        raise ValueError('service retirement receipt is invalid')
    if value['config_fingerprint'] != fingerprint:
        raise ValueError('service retirement receipt belongs to a different configuration')
    return value


def require_active(config_path, config=None):
    if inspect_retirement(config_path, config) is not None:
        raise ServiceRetired('this service configuration is retired')


def _locks(config):
    root = Path(config['root']) / config['domain']
    return StartLock(root / 'start.lock'), StartLock(root / 'service-admission.lock')


def _consumer_state(config):
    path = Path(config['root']) / config['domain'] / 'consumer.lock'
    try:
        path.stat()
    except FileNotFoundError:
        return False
    held = lock_held(path)
    if held is None:
        raise OSError('service consumer ownership cannot be established')
    return held


def acquire_consumer(config_path, config):
    """Direct serve admission; never wait on the consumer while barring retirement."""
    _, admission = _locks(config)
    consumer = StartLock(Path(config['root']) / config['domain'] / 'consumer.lock')
    while True:
        admission.acquire(wait=None)
        try:
            require_active(config_path, config)
            try:
                consumer.acquire()
            except StartInProgress:
                pass
            else:
                return consumer
        finally:
            admission.release()
        time.sleep(.01)


def retire_service(config_path):
    from .cli import config_at, connect
    from .handoff import _probe
    from .transport import rpc
    config = config_at(config_path)
    start, admission = _locks(config)
    start.acquire(wait=None)
    try:
        admission.acquire(wait=None)
        try:
            previous = inspect_retirement(config_path, config)
            if previous is not None:
                if previous['status'] != 'retired':
                    raise RuntimeError('previous service retirement has an uncertain outcome; inspect it before recovery')
                if _consumer_state(config):
                    raise RuntimeError('retired service still owns its consumer lock')
                return dict(idle=True, service='absent', retirement=previous)
            receipt = dict(schema=SCHEMA, config_fingerprint=config_fingerprint(config_path, config),
                           operation_id=uuid.uuid4().hex, status='retiring')
            _atomic_write_text(_marker(config_path), json.dumps(receipt, sort_keys=True) + '\n')
            # A direct serve may already own the consumer but still be loading.
            # Wait for its actual readiness or lifetime-lock release.
            while True:
                state = _probe(config, None, config_path=config_path)
                if state != 'starting':
                    break
                time.sleep(.05)
            if state == 'absent':
                outcome = dict(idle=True, service='absent')
            elif state == 'ready':
                result = rpc(connect(config, config_path=config_path), 'stop_if_idle')
                if not isinstance(result, dict) or not isinstance(result.get('idle'), bool):
                    raise ValueError('service retirement response is invalid')
                if not result['idle']:
                    # Only undo this operation's newly-created, exact receipt.
                    if inspect_retirement(config_path, config) != receipt:
                        raise RuntimeError('service retirement ownership changed')
                    _marker(config_path).unlink()
                    return dict(result, service='busy', retirement=dict(receipt, status='busy'))
                outcome = dict(result, service='stopped')
            else:
                raise RuntimeError('service retirement cannot establish service state: ' + state)
            # A stop acknowledgement is a known effect. Preserve it before
            # cleanup, even if filesystem/connection cleanup later fails.
            receipt['status'] = 'retired'
            try:
                _atomic_write_text(_marker(config_path), json.dumps(receipt, sort_keys=True) + '\n')
            except OSError:
                return dict(outcome, retirement=receipt, cleanup_failed=True)
            try:
                while _consumer_state(config):
                    time.sleep(.05)
            except OSError:
                return dict(outcome, retirement=receipt, cleanup_failed=True)
            return dict(outcome, retirement=receipt)
        finally:
            admission.release()
    finally:
        start.release()


def restore_service(config_path, retirement=None):
    """Restore an exact, known completed retirement after adapter rollback."""
    from .cli import config_at
    config = config_at(config_path)
    start, admission = _locks(config)
    start.acquire(wait=None)
    try:
        admission.acquire(wait=None)
        try:
            current = inspect_retirement(config_path, config)
            if current is None:
                return dict(status='not-needed')
            if current['status'] != 'retired' or retirement is not None and current != retirement:
                raise ValueError('only the exact completed retirement can be restored')
            _marker(config_path).unlink()
            return dict(status='restored', config_fingerprint=current['config_fingerprint'])
        finally:
            admission.release()
    finally:
        start.release()
