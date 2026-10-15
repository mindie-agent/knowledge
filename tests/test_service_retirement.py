"""Retirement excludes late real starters and preserves uncertain stop effects."""
import json
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest
import transcript_double

from mindie_knowledge.loop import cli, lifecycle, transport
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.locks import StartLock, lock_held
from mindie_knowledge.loop.store import Store
from mindie_knowledge.loop.handoff import _probe


def configuration(tmp_path):
    config = dict(root=str(tmp_path / 'data'), domain='test',
                  transcript_adapter=transcript_double.__file__,
                  redactor_executable=str(tmp_path / 'gitleaks'))
    path = tmp_path / 'engine.json'
    path.write_text(json.dumps(config))
    return path, config


def test_retirement_discards_previous_generation_discovery_before_new_owner(tmp_path):
    path, config = configuration(tmp_path)
    connection = cli.connection_path(config)
    connection.parent.mkdir(parents=True)
    connection.write_text(json.dumps(dict(domain=config['domain'], token='test',
        url='http://127.0.0.1:1', config_fingerprint=lifecycle.config_fingerprint(path, config))))
    next_path = tmp_path / 'next-engine.json'
    next_path.write_text(json.dumps(config))
    lifecycle.retire_service(path)
    # A real new owner can hold its lifetime lock while initializing. Its old
    # endpoint must not become an unknown configuration or RPC to the old owner.
    with StartLock(connection.with_name('consumer.lock')):
        assert _probe(config, None, config_path=next_path) == 'starting'
    assert not connection.exists()
    try:
        actual = cli.ensure_service(next_path)
        assert actual['config_fingerprint'] == lifecycle.config_fingerprint(next_path, config)
        assert transport.rpc(actual, 'status')['worker_alive']
    finally:
        lifecycle.retire_service(next_path)


@pytest.mark.parametrize('previous_receipt', [False, True])
def test_discovery_cleanup_failure_preserves_completed_retirement(tmp_path, monkeypatch, previous_receipt):
    path, config = configuration(tmp_path)
    if previous_receipt:
        lifecycle.retire_service(path)
    connection = cli.connection_path(config)
    connection.parent.mkdir(parents=True, exist_ok=True)
    with StartLock(connection.with_name('consumer.lock')):
        pass
    connection.write_text('previous stopped listener')
    unlink = Path.unlink
    def denied(target, *args, **kwargs):
        if target == connection:
            raise PermissionError(13, 'private path must not appear in result')
        return unlink(target, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', denied)
    result = lifecycle.retire_service(path)
    assert result['idle'] and result['service'] == 'absent'
    assert result['retirement']['status'] == 'retired' and result['cleanup_failed']
    assert result['cleanup_error'] == dict(stage='connection_discovery', error_type='PermissionError',
                                          errno=13, operation_completed=True)
    assert lifecycle.inspect_retirement(path)['status'] == 'retired'
    assert connection.read_text() == 'previous stopped listener'
    assert 'private path' not in json.dumps(result)
    monkeypatch.setattr(Path, 'unlink', unlink)
    recovered = lifecycle.retire_service(path)
    assert recovered['retirement'] == result['retirement']
    assert not recovered.get('cleanup_failed') and not connection.exists()


def test_retirement_bars_already_spawned_late_helper_and_direct_serve(tmp_path):
    path, config = configuration(tmp_path)
    ready, release = tmp_path / 'ready', tmp_path / 'release'
    code = (
        'from pathlib import Path; import time\n'
        'from mindie_knowledge.loop.cli import ensure_service\n'
        'from mindie_knowledge.loop.lifecycle import ServiceRetired\n'
        f'Path({str(ready)!r}).touch()\n'
        f'while not Path({str(release)!r}).exists(): time.sleep(.01)\n'
        f'try: ensure_service({str(path)!r}, _from_detached_starter=True)\n'
        'except ServiceRetired: raise SystemExit(0)\n'
        'raise SystemExit(9)\n'
    )
    child = subprocess.Popen([sys.executable, '-c', code])
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            assert child.poll() is None
            time.sleep(.01)
        assert ready.exists()
        result = lifecycle.retire_service(path)
        assert result['idle'] and result['service'] == 'absent'
        release.touch()
        assert child.wait(timeout=5) == 0
        direct = subprocess.run([sys.executable, '-m', 'mindie_knowledge.loop.cli',
                                 'serve', '--config', str(path)], capture_output=True, timeout=5)
        assert direct.returncode != 0 and b'configuration is retired' in direct.stderr
        assert not cli.connection_path(config).exists()
        assert not (tmp_path / 'data/test/state-v1/state-v4.sqlite3').exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)


def test_real_idle_service_stops_and_exact_rollback_can_restart(tmp_path):
    path, config = configuration(tmp_path)
    first = cli.ensure_service(path)
    try:
        result = lifecycle.retire_service(path)
        assert result['idle'] and result['service'] == 'stopped'
        assert not cli.connection_path(config).exists()
        assert lock_held(cli.connection_path(config).with_name('consumer.lock')) is False
        with pytest.raises(lifecycle.ServiceRetired):
            cli.ensure_service(path)
        wrong = dict(result['retirement'], operation_id='0' * 32)
        with pytest.raises(ValueError, match='exact completed'):
            lifecycle.restore_service(path, wrong)
        assert lifecycle.restore_service(path, result['retirement'])['status'] == 'restored'
        second = cli.ensure_service(path)
        assert second['token'] != first['token']
    finally:
        if lifecycle.inspect_retirement(path) is None:
            lifecycle.retire_service(path)


def test_busy_service_rolls_back_only_this_retirement(tmp_path):
    path, config = configuration(tmp_path)
    connection = cli.connection_path(config)
    with closing(Store(config['root'], config['domain'])) as store, StartLock(connection.with_name('consumer.lock')):
        engine = Engine(store)
        service = transport.Service(engine, connection_path=connection,
                                    config_fingerprint=lifecycle.config_fingerprint(path, config))
        thread = threading.Thread(target=service.serve, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not connection.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert engine.begin_work()
            result = lifecycle.retire_service(path)
            assert result['idle'] is False and result['service'] == 'busy'
            assert lifecycle.inspect_retirement(path) is None
            assert transport.rpc(service.connection, 'status')['worker_alive']
            engine.end_work()
        finally:
            service.close()
            thread.join(timeout=5)


def test_lost_stop_reply_reconciles_owner_exit_without_repeating_rpc(tmp_path, monkeypatch):
    path, config = configuration(tmp_path)
    cli.ensure_service(path)
    real, calls = transport.rpc, []
    def lose_reply(connection, method, *args, **kwargs):
        result = real(connection, method, *args, **kwargs)
        if method == 'stop_if_idle':
            calls.append(method)
            raise ConnectionResetError('terminal reply lost')
        return result
    monkeypatch.setattr(transport, 'rpc', lose_reply)
    with pytest.raises(ConnectionResetError):
        lifecycle.retire_service(path)
    assert lifecycle.inspect_retirement(path)['status'] == 'retiring'
    with pytest.raises(ValueError, match='exact completed'):
        lifecycle.restore_service(path)
    consumer = cli.connection_path(config).with_name('consumer.lock')
    deadline = time.monotonic() + 5
    while lock_held(consumer) is not False and time.monotonic() < deadline:
        time.sleep(.01)
    assert lock_held(consumer) is False
    recovered = lifecycle.retire_service(path, control_timeout=2)
    assert recovered['retirement']['status'] == 'retired'
    assert recovered['service'] == 'absent'
    assert not cli.connection_path(config).exists()
    assert calls == ['stop_if_idle']


def test_uncertain_stop_rechecks_exact_owner_before_idempotent_control(tmp_path, monkeypatch):
    path, config = configuration(tmp_path)
    connection = cli.ensure_service(path)
    real, calls = transport.rpc, []

    def lose_request(connection, method, *args, **kwargs):
        if method == 'stop_if_idle':
            calls.append(method)
            if len(calls) == 1:
                raise ConnectionResetError('request delivery is unknown')
        return real(connection, method, *args, **kwargs)

    monkeypatch.setattr(transport, 'rpc', lose_request)
    try:
        with pytest.raises(ConnectionResetError):
            lifecycle.retire_service(path, control_timeout=2)
        with pytest.raises(ValueError, match='exact completed'):
            lifecycle.restore_service(path)
        assert real(connection, 'status')['worker_alive']
        original = lifecycle.inspect_retirement(path)
        result = lifecycle.retire_service(path, control_timeout=2)
        assert result['idle'] and result['retirement']['status'] == 'retired'
        assert result['retirement']['operation_id'] == original['operation_id']
        assert calls == ['stop_if_idle', 'stop_if_idle']
    finally:
        if lock_held(cli.connection_path(config).with_name('consumer.lock')):
            real(connection, 'stop')
        deadline = time.monotonic() + 5
        while lock_held(cli.connection_path(config).with_name('consumer.lock')) is not False and time.monotonic() < deadline:
            time.sleep(.01)
        lifecycle.retire_service(path, control_timeout=2)


def test_uncertain_retirement_never_sends_control_to_foreign_owner(tmp_path, monkeypatch):
    path, config = configuration(tmp_path)
    lifecycle.retire_service(path)
    marker = lifecycle._marker(path)
    previous = json.loads(marker.read_text())
    previous['status'] = 'retiring'
    marker.write_text(json.dumps(previous))
    other = tmp_path / 'other-engine.json'
    other.write_text(json.dumps(config))
    connection = cli.ensure_service(other)
    real, calls = transport.rpc, []

    def count_control(connection, method, *args, **kwargs):
        calls.append(method)
        return real(connection, method, *args, **kwargs)

    monkeypatch.setattr(transport, 'rpc', count_control)
    try:
        with pytest.raises(RuntimeError, match='state: unknown'):
            lifecycle.retire_service(path, control_timeout=2)
        assert calls == []
        assert json.loads(marker.read_text()) == previous
        assert real(connection, 'status')['worker_alive']
    finally:
        lifecycle.retire_service(other)


def test_control_timeout_does_not_mark_or_interrupt_initializing_owner(tmp_path):
    path, config = configuration(tmp_path)
    with StartLock(cli.connection_path(config).with_name('consumer.lock')):
        started = time.monotonic()
        with pytest.raises(lifecycle.RetirementTimeout) as failure:
            lifecycle.retire_service(path, control_timeout=.1)
        assert failure.value.stage == 'readiness'
        assert time.monotonic() - started < 2
        assert lifecycle.inspect_retirement(path) is None
        assert lock_held(cli.connection_path(config).with_name('consumer.lock'))


def test_control_timeout_leaves_contended_start_lock_and_config_untouched(tmp_path):
    path, config = configuration(tmp_path)
    before = path.read_bytes()
    with StartLock(cli.connection_path(config).with_name('start.lock')):
        with pytest.raises(lifecycle.RetirementTimeout) as failure:
            lifecycle.retire_service(path, control_timeout=.1)
        assert failure.value.stage == 'startup_lock'
        assert path.read_bytes() == before
        assert lifecycle.inspect_retirement(path) is None


def test_known_stop_timeout_preserves_receipt_without_terminating_owner(tmp_path, monkeypatch):
    path, config = configuration(tmp_path)
    cli.ensure_service(path)
    acknowledgements = []
    actual_rpc, actual_pause = transport.rpc, lifecycle._pause

    def observe_rpc(connection, method, *args, **kwargs):
        result = actual_rpc(connection, method, *args, **kwargs)
        if method == 'stop_if_idle':
            acknowledgements.append(result)
        return result

    def timeout_after_ack(deadline, stage):
        if stage == 'consumer_ownership':
            assert len(acknowledgements) == 1 and acknowledgements[0]['idle'] is True
            assert lifecycle.inspect_retirement(path)['status'] == 'retired'
            raise lifecycle.RetirementTimeout(stage)
        return actual_pause(deadline, stage)

    with monkeypatch.context() as patch:
        patch.setattr(transport, 'rpc', observe_rpc)
        patch.setattr(lifecycle, '_consumer_state', lambda _config: True)
        patch.setattr(lifecycle, '_pause', timeout_after_ack)
        result = lifecycle.retire_service(path)
    assert result['service'] == 'stopped'
    assert result['idle'] and result['retirement']['status'] == 'retired'
    assert result['cleanup_failed']
    assert result['cleanup_error']['stage'] == 'consumer_ownership'
    assert result['cleanup_error']['error_type'] == 'RetirementTimeout'
    assert result['cleanup_error']['operation_completed'] is True
    deadline = time.monotonic() + 5
    while lock_held(cli.connection_path(config).with_name('consumer.lock')) is not False and time.monotonic() < deadline:
        time.sleep(.01)
    assert lifecycle.retire_service(path, control_timeout=2)['retirement'] == result['retirement']


@pytest.mark.parametrize('timeout', [True, -1, float('inf'), float('nan'), '30'])
def test_invalid_control_timeout_has_no_retirement_effect(tmp_path, timeout):
    path, _ = configuration(tmp_path)
    with pytest.raises(ValueError, match='control_timeout'):
        lifecycle.retire_service(path, control_timeout=timeout)
    assert lifecycle.inspect_retirement(path) is None


def test_known_stop_survives_retirement_receipt_cleanup_failure(tmp_path, monkeypatch):
    path, config = configuration(tmp_path)
    cli.ensure_service(path)
    real = lifecycle._atomic_write_text
    def fail_completed_receipt(target, content):
        if json.loads(content)['status'] == 'retired':
            raise OSError('receipt storage unavailable')
        return real(target, content)
    monkeypatch.setattr(lifecycle, '_atomic_write_text', fail_completed_receipt)
    result = lifecycle.retire_service(path)
    assert result['idle'] and result['service'] == 'stopped' and result['cleanup_failed']
    assert result['retirement']['status'] == 'retired'
    assert lifecycle.inspect_retirement(path)['status'] == 'retiring'

    assert result['cleanup_error'] == dict(stage='retirement_receipt', error_type='OSError',
                                           errno=None, operation_completed=True)


def test_known_stop_retains_failed_consumer_observation_stage(tmp_path, monkeypatch):
    path, config = configuration(tmp_path)
    cli.ensure_service(path)
    def unavailable(_config):
        raise PermissionError(13, 'private path must not appear in result')
    monkeypatch.setattr(lifecycle, '_consumer_state', unavailable)
    result = lifecycle.retire_service(path)
    assert result['idle'] and result['service'] == 'stopped'
    assert result['cleanup_failed']
    assert result['cleanup_error'] == dict(stage='consumer_ownership', error_type='PermissionError',
                                           errno=13, operation_completed=True)
    assert lifecycle.inspect_retirement(path)['status'] == 'retired'
    assert 'private path' not in json.dumps(result)


def test_live_owner_reuses_exact_config_but_rejects_another_binding(tmp_path, monkeypatch):
    path, config = configuration(tmp_path)
    first = cli.ensure_service(path)
    other = tmp_path / 'other-engine.json'
    other.write_text(json.dumps(config))
    from mindie_knowledge.loop import service_host
    backend = service_host.platform_backend()
    if backend is not None:
        monkeypatch.setattr(backend, 'request_start',
                            lambda *args: pytest.fail('must not launch another owner'))
    try:
        assert cli.ensure_service(path) == first
        assert _probe(config, .25, config_path=other) == 'unknown'
        with pytest.raises(ValueError, match='another service configuration'):
            cli.connect(config, config_path=other)
        with pytest.raises(RuntimeError, match='unknown'):
            cli.ensure_service(other)
        assert transport.rpc(first, 'status')['worker_alive']
        assert cli.connection_path(config).exists()
    finally:
        lifecycle.retire_service(path)


def test_removed_client_declaration_is_not_silently_ignored(tmp_path):
    path, config = configuration(tmp_path)
    config['client_contract'] = {'schema': 'mindie-service-client/1'}
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match='cross-runtime client compatibility'):
        cli.config_at(path)
    assert not cli.connection_path(config).exists()


def test_successor_receipt_is_rejected_without_rewriting_or_starting(tmp_path):
    path, config = configuration(tmp_path)
    result = lifecycle.retire_service(path)
    marker = lifecycle._marker(path)
    receipt = dict(result['retirement'], successor={
        'config_path': str(tmp_path / 'another-engine.json'),
        'config_fingerprint': 'a' * 64,
    })
    marker.write_text(json.dumps(receipt))
    before = marker.read_bytes()
    with pytest.raises(ValueError, match='retirement receipt is invalid'):
        lifecycle.inspect_retirement(path)
    with pytest.raises(ValueError, match='retirement receipt is invalid'):
        cli.ensure_service(path)
    assert marker.read_bytes() == before
    assert not cli.connection_path(config).exists()
