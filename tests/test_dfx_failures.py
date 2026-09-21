"""Original failures and cancellation survive optional safe diagnostics."""
import sys
import threading
import types

import pytest

from mindie_knowledge.loop.dfx import failure
from mindie_knowledge.loop.process import MaintenanceCancelled, bounded_run


@pytest.fixture
def reports(monkeypatch):
    records = []
    module = types.ModuleType('mindie_diagnostics.integration')
    def record_failure(**kwargs):
        records.append(kwargs)
        return {'recorded': True, 'incident_id': 'a' * 32, 'logging_failed': False}
    module.record_failure = record_failure
    monkeypatch.setitem(sys.modules, 'mindie_diagnostics.integration', module)
    return records


@pytest.mark.parametrize('code,category,reportable', [(70, 'native', False), (78, 'configuration', False), (2, 'unknown', False)])
def test_actual_child_exit_recorded_without_stderr(reports, code, category, reportable):
    with pytest.raises(RuntimeError, match=f'maintenance agent exited {code}') as caught:
        bounded_run([sys.executable, '-c', f'import sys;sys.stderr.write("private_provider_sentinel");sys.exit({code})'], '', timeout=3, max_output=4096)
    assert len(reports) == 1
    row = reports[0]
    assert row['category'] == category and row['reportable'] is reportable and row['exit_code'] == code
    assert row['exception'] is caught.value and 'private_provider_sentinel' not in str(caught.value)
    assert caught.value.mindie_diagnostic == {'incident_id': 'a' * 32, 'logging_failed': False}
    failure('knowledge.capture', stage='process', category='internal_exception', exception=caught.value)
    assert len(reports) == 1


@pytest.mark.parametrize('code,limit,timeout,error,category', [
    ('import time;time.sleep(3)', 4096, .1, TimeoutError, 'deadline'),
    ('import os;os.write(1,b"x"*20000)', 4096, 3, ValueError, 'output_limit'),
])
def test_actual_child_control_failure(reports, code, limit, timeout, error, category):
    with pytest.raises(error):
        bounded_run([sys.executable, '-c', code], '', timeout=timeout, max_output=limit)
    assert len(reports) == 1 and reports[0]['category'] == category


def test_actual_cancel_does_not_report(reports):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(MaintenanceCancelled):
        bounded_run([sys.executable, '-c', 'import time;time.sleep(3)'], '', timeout=3, max_output=4096, cancel=cancel)
    assert reports == []


def test_logger_failure_cannot_replace_original_error(monkeypatch):
    module = types.ModuleType('mindie_diagnostics.integration')
    module.record_failure = lambda **_: (_ for _ in ()).throw(RuntimeError('private_logger_sentinel'))
    monkeypatch.setitem(sys.modules, 'mindie_diagnostics.integration', module)
    with pytest.raises(RuntimeError, match='maintenance agent exited 70') as caught:
        bounded_run([sys.executable, '-c', 'raise SystemExit(70)'], '', timeout=3, max_output=4096)
    assert caught.value.mindie_diagnostic == {'logging_failed': True}


def test_external_reference_is_not_dedup_authority(reports):
    exc = RuntimeError('private_exception_sentinel')
    exc.mindie_diagnostic = {'incident_id': 'b' * 32, 'logging_failed': False}
    exc._mindie_diagnostic = {'incident_id': 'b' * 32}
    failure('knowledge.capture', stage='process', category='internal_exception', exception=exc)
    assert len(reports) == 1 and exc.mindie_diagnostic['incident_id'] == 'a' * 32


@pytest.mark.parametrize('child_code', [0, 70])
def test_cleanup_fault_keeps_original_and_both_facts(reports, monkeypatch, child_code):
    from mindie_knowledge.loop import process
    original_cleanup = process.terminate_tree
    def cleanup_fault(child):
        original_cleanup(child)
        child.stdout.close()
        child.stderr.close()
        raise OSError('private_cleanup_sentinel')
    monkeypatch.setattr(process, 'terminate_tree', cleanup_fault)
    expected = OSError if child_code == 0 else RuntimeError
    with pytest.raises(expected) as caught:
        bounded_run([sys.executable, '-c', f'raise SystemExit({child_code})'], '', timeout=3, max_output=4096)
    if child_code:
        assert 'exited 70' in str(caught.value)
    assert [r['category'] for r in reports] == (['native', 'cleanup'] if child_code else ['cleanup'])


def test_actual_agent_invalid_json_and_expected_publish_error(reports, store, tmp_path):
    from conftest import write_settings
    from mindie_knowledge.loop.engine import Engine
    from mindie_knowledge.community.common import CommunityError
    settings = tmp_path / 'sharing.json'
    write_settings(settings, roots=[tmp_path])
    engine = Engine(store, settings_path=settings, agent_command=[sys.executable, '-c', 'print("[1]")'])
    with pytest.raises(ValueError, match='one JSON object'):
        engine.agent({'role': 'organize'}, attempt_id='fixture', root_hash='root')
    assert [r['category'] for r in reports] == ['invalid_result']
    engine._unexpected('knowledge.publish', 'submit', CommunityError('private_network_sentinel'))
    engine._unexpected('knowledge.publish', 'submit', PermissionError('private_permission_sentinel'))
    assert len(reports) == 1
    engine._unexpected('knowledge.publish', 'submit', KeyError('private_internal_sentinel'))
    assert [r['category'] for r in reports] == ['invalid_result', 'internal_exception']


def test_live_http_internal_reference_not_duplicated(reports, store, monkeypatch):
    from mindie_knowledge.loop.engine import Engine
    from mindie_knowledge.loop.transport import Service, rpc, RequestRejected
    engine = Engine(store)
    service = Service(engine)
    original_call = service.call
    def call(method, args):
        if method == 'broken_owned_operation':
            raise KeyError('private_internal_sentinel')
        return original_call(method, args)
    monkeypatch.setattr(service, 'call', call)
    thread = threading.Thread(target=service.serve, daemon=True)
    thread.start()
    try:
        with pytest.raises(RequestRejected):
            rpc(service.connection, 'unsupported_fixture')
        assert reports == []
        with pytest.raises(RuntimeError) as caught:
            rpc(service.connection, 'broken_owned_operation')
        assert len(reports) == 1 and reports[0]['stage'] == 'dispatch'
        assert caught.value.mindie_diagnostic == {'incident_id': 'a' * 32, 'logging_failed': False}
        failure('knowledge.capture', stage='rpc', category='internal_exception', exception=caught.value)
        assert len(reports) == 1
    finally:
        service.close()
        thread.join(timeout=3)


@pytest.mark.parametrize('enabled', [False, True])
def test_capture_hook_records_only_authorized_rpc_internal(reports, tmp_path, monkeypatch, enabled):
    import json
    from conftest import write_settings, make_admission, admission_token
    from mindie_knowledge.loop import cli
    sharing = tmp_path / 'sharing.json'
    write_settings(sharing, enabled=enabled, roots=[tmp_path])
    admission = make_admission(tmp_path, project_root=tmp_path)
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'root': str(tmp_path / 'knowledge'), 'domain': 'fixture', 'community_config': str(sharing), 'admission_path': str(admission)}))
    calls = []
    monkeypatch.setattr(cli, 'connect', lambda config: {'url': 'fixture'})
    def broken(*a, **k):
        calls.append(True)
        raise KeyError('private_rpc_sentinel')
    monkeypatch.setattr(cli, 'rpc', broken)
    event = {'hook_event_name': 'Stop', 'session_id': 'manual-A', 'turn_id': 'turn', 'mindie_activation': admission_token(admission)}
    assert cli.capture_hook(config, event) is None
    assert len(calls) == len(reports) == int(enabled)


def test_actual_invalid_utf8_is_recorded_at_decode_owner(reports):
    with pytest.raises(UnicodeDecodeError) as caught:
        bounded_run([sys.executable, '-c', 'import os;os.write(1,bytes([255]))'], '', timeout=3, max_output=4096)
    assert len(reports) == 1
    assert reports[0]['stage'] == 'decode' and reports[0]['category'] == 'invalid_result'
    assert reports[0]['reportable'] is True
    assert caught.value.mindie_diagnostic['incident_id'] == 'a' * 32


@pytest.mark.parametrize('code', [70, 2, 78, 124])
def test_model_exit_alone_is_local_diagnostic_not_product_bug(reports, code):
    with pytest.raises(RuntimeError):
        bounded_run([sys.executable, '-c', f'raise SystemExit({code})'], '', timeout=3, max_output=4096)
    assert len(reports) == 1 and reports[0]['reportable'] is False
