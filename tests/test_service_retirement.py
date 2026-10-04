"""Retirement excludes late real starters and preserves uncertain stop effects."""
import json
import subprocess
import sys
import threading
import time
from contextlib import closing

import pytest
import transcript_double

from mindie_knowledge.loop import cli, lifecycle, transport
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.locks import StartLock, lock_held
from mindie_knowledge.loop.store import Store


def configuration(tmp_path):
    config = dict(root=str(tmp_path / 'data'), domain='test',
                  transcript_adapter=transcript_double.__file__,
                  redactor_executable=str(tmp_path / 'gitleaks'))
    path = tmp_path / 'engine.json'
    path.write_text(json.dumps(config))
    return path, config


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
        assert not (tmp_path / 'data/test/state-v4.sqlite3').exists()
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


def test_lost_stop_reply_is_not_repeated_or_restored(tmp_path, monkeypatch):
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
    with pytest.raises(RuntimeError, match='uncertain outcome'):
        lifecycle.retire_service(path)
    with pytest.raises(ValueError, match='exact completed'):
        lifecycle.restore_service(path)
    assert calls == ['stop_if_idle']


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
