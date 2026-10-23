"""[Role] Explicitly retire or restore one exact service configuration.

[Invariants] The receipt lives beside the configuration. It bars already-spawned wake
helpers as well as new starters; absence of a listener alone is never an
admission decision. The startup and admission OS locks make that decision
atomic with service ownership. No elapsed-time deadline governs useful work.
[Timeouts & Locks] Caller control budgets bound startup/admission locks and
owner observation; restoration can use the caller's remaining cleanup budget.
[Side-Effect Policy] Stops only idle owners and writes exact configuration
retirement markers. Restoration never invokes model or publication work.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
import uuid
from pathlib import Path

from mindie_knowledge.markdown import _atomic_write_text
from ..filesystem import filesystem_path

from .locks import StartInProgress, StartLock, lock_held

SCHEMA = 'mindie-service-retirement/1'


class ServiceRetired(RuntimeError):
    pass


class RetirementTimeout(TimeoutError):
    """A control observation expired; no business process was terminated."""

    def __init__(self, stage):
        self.stage = stage
        super().__init__("service retirement observation timed out: " + stage)


def _remaining(deadline, stage):
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RetirementTimeout(stage)
    return remaining


def _acquire(lock, deadline, stage):
    try:
        lock.acquire(wait=_remaining(deadline, stage))
    except StartInProgress:
        if deadline is None:
            raise
        raise RetirementTimeout(stage) from None


def _pause(deadline, stage):
    remaining = _remaining(deadline, stage)
    time.sleep(.05 if remaining is None else min(.05, remaining))


def config_fingerprint(config_path, config=None):
    from .cli import config_at
    value = config_at(config_path) if config is None else config
    data = dict(path=str(Path(config_path).resolve()), config=value)
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _marker(config_path):
    return filesystem_path(config_path).with_name(Path(config_path).name + '.retired')


def inspect_retirement(config_path, config=None):
    from .diagnostics import _bytes
    fingerprint = config_fingerprint(config_path, config)
    try:
        value = json.loads(_bytes(_marker(config_path)))
    except FileNotFoundError:
        return None
    if (not isinstance(value, dict) or value.get('schema') != SCHEMA
            or set(value) != {'schema', 'status', 'operation_id', 'config_fingerprint'}
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
    path = filesystem_path(config['root']) / config['domain'] / 'consumer.lock'
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
        admission.acquire(wait=5.0)
        try:
            require_active(config_path, config)
            try:
                consumer.acquire(wait=0.0)
            except StartInProgress:
                pass
            else:
                return consumer
        finally:
            admission.release()
        time.sleep(.01)


def _completed_retirement(config_path, config, outcome, receipt):
    """Discard obsolete discovery only after owner exit, under both locks."""
    from .cli import connection_path
    from .service_host import platform_backend
    backend = platform_backend()
    if backend is not None:
        try:
            backend.remove_owned(config_path, config)
        except TimeoutError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            return dict(outcome, retirement=receipt, cleanup_failed=True,
                        cleanup_error=dict(stage='service_host', error_type=type(exc).__name__,
                                           operation_completed=True))
    try:
        connection_path(config).unlink(missing_ok=True)
    except TimeoutError:
        raise
    except OSError as exc:
        return dict(outcome, retirement=receipt, cleanup_failed=True,
                    cleanup_error=dict(stage='connection_discovery', error_type=type(exc).__name__,
                                       errno=exc.errno, operation_completed=True))
    return dict(outcome, retirement=receipt)


def retire_service(config_path, *, control_timeout=None):
    """Retire an idle owner, optionally bounding control waits.

    A missing stop acknowledgement stays uncertain until exact owner observation.
    A ready owner can safely receive the idempotent stop-if-idle control again;
    stopping owners are only observed. No business operation is replayed or killed.
    """
    from .cli import config_at, connect
    from .handoff import _probe
    from .transport import rpc
    if control_timeout is not None and (
        isinstance(control_timeout, bool)
        or not isinstance(control_timeout, (int, float))
        or not math.isfinite(control_timeout)
        or control_timeout < 0
    ):
        raise ValueError("control_timeout must be a finite nonnegative number")
    deadline = None if control_timeout is None else time.monotonic() + control_timeout
    config = config_at(config_path)
    start, admission = _locks(config)
    _acquire(start, deadline, "startup_lock")
    try:
        _acquire(admission, deadline, "admission_lock")
        receipt = None
        try:
            previous = inspect_retirement(config_path, config)
            if previous is not None and previous['status'] == 'retired':
                if _consumer_state(config):
                    raise RuntimeError('retired service still owns its consumer lock')
                return _completed_retirement(config_path, config, dict(idle=True, service='absent'), previous)
            # Startup locks already bar another owner. Do not mark a healthy
            # service retired while merely observing initialization.
            while True:
                state = _probe(config, _remaining(deadline, "readiness"), config_path=config_path)
                _remaining(deadline, "readiness")
                if state not in {'starting', 'stopping'}:
                    break
                _pause(deadline, "readiness")
            if state not in {'absent', 'ready'}:
                raise RuntimeError('service retirement cannot establish service state: ' + state)
            connection = connect(config, config_path=config_path) if state == 'ready' else None
            _remaining(deadline, "stop_acknowledgement")
            receipt = dict(previous) if previous is not None else dict(
                schema=SCHEMA, config_fingerprint=config_fingerprint(config_path, config),
                operation_id=uuid.uuid4().hex, status='retiring')
            if previous is None:
                _atomic_write_text(_marker(config_path), json.dumps(receipt, sort_keys=True) + '\n')
            if state == 'absent':
                outcome = dict(idle=True, service='absent')
            else:
                try:
                    remaining = _remaining(deadline, "stop_acknowledgement")
                except RetirementTimeout:
                    # Undo a new marker only: an earlier attempt is still unknown.
                    if previous is None and inspect_retirement(config_path, config) == receipt:
                        _marker(config_path).unlink()
                    raise
                result = rpc(connection, 'stop_if_idle', timeout=remaining)
                if not isinstance(result, dict) or not isinstance(result.get('idle'), bool):
                    raise ValueError('service retirement response is invalid')
                if not result['idle']:
                    if inspect_retirement(config_path, config) != receipt:
                        raise RuntimeError('service retirement ownership changed')
                    _marker(config_path).unlink()
                    return dict(result, service='busy', retirement=dict(receipt, status='busy'))
                outcome = dict(result, service='stopped')
            # A stop acknowledgement is a known effect. Preserve it before
            # cleanup, even if filesystem/connection cleanup later fails.
            receipt['status'] = 'retired'
            try:
                _atomic_write_text(_marker(config_path), json.dumps(receipt, sort_keys=True) + '\n')
            except TimeoutError:
                raise
            except OSError as exc:
                return dict(outcome, retirement=receipt, cleanup_failed=True,
                            cleanup_error=dict(stage='retirement_receipt', error_type=type(exc).__name__,
                                               errno=exc.errno, operation_completed=True))
            try:
                while _consumer_state(config):
                    _pause(deadline, "consumer_ownership")
            except (RetirementTimeout, OSError) as exc:
                return dict(outcome, retirement=receipt, cleanup_failed=True,
                            cleanup_error=dict(stage='consumer_ownership', error_type=type(exc).__name__,
                                               errno=getattr(exc, 'errno', None), operation_completed=True))
            return _completed_retirement(config_path, config, outcome, receipt)
        except BaseException as exc:
            if receipt is not None:
                exc.mindie_retirement = dict(receipt)
            raise
        finally:
            admission.release()
    finally:
        start.release()


def restore_service(config_path, retirement=None, *, control_timeout=None):
    """Explicitly restore one exact, known completed retirement."""
    from .cli import config_at
    if control_timeout is not None and (
        type(control_timeout) not in (int, float) or not math.isfinite(control_timeout) or control_timeout < 0
    ):
        raise ValueError("control_timeout must be a finite nonnegative number")
    deadline = None if control_timeout is None else time.monotonic() + control_timeout
    config = config_at(config_path)
    start, admission = _locks(config)
    _acquire(start, deadline, "startup_lock")
    try:
        _acquire(admission, deadline, "admission_lock")
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
