"""Native catalog preparation preserves policy and separates domain stores."""
import json
from pathlib import Path
import sys

from conftest import write_settings
from mindie_knowledge.loop.domain_catalog import prepare
from mindie_knowledge.loop.settings import from_engine_config
from mindie_knowledge.loop.domain_routes import select_engine_config


def initial(tmp_path):
    shared = tmp_path / 'community.json'
    settings = write_settings(shared, consent_choice='contribute', roots=[tmp_path])
    engine = dict(root=str(tmp_path / 'data'), domain='vllm-ascend',
                  admission_path=str(tmp_path / 'default-admission.sqlite3'),
                  community_config=str(shared), redactor_executable=sys.executable,
                  transcript_adapter=str(Path(__file__).parent / 'transcript_double.py'),
                  summary_command=[sys.executable, 'native-worker.py'])
    path = tmp_path / 'default.engine.json'
    path.write_text(json.dumps(engine))
    adapter = dict(engine_config=str(path), community_config=str(shared), extension={'keep': 1})
    return adapter, engine, dict(config_directory=str(tmp_path / 'runtime-1'),
        authority_directory=str(tmp_path / 'authorities'), consent_config=settings.raw['consent_config'])


def test_catalog_creates_separate_read_domains_without_expanding_saved_consent(tmp_path):
    adapter, engine, options = initial(tmp_path)
    before = {path: path.read_bytes() for path in
              (Path(engine['community_config']), Path(options['consent_config']))}
    result = prepare(adapter, engine, **options)
    assert len(result['domain_configs']) == 7
    assert result['domain_configs']['vllm-ascend'] == adapter['engine_config']
    generations, admissions, roots = set(), set(), set()
    for domain, path in result['domain_configs'].items():
        selected = select_engine_config(adapter['engine_config'], domain=domain,
                                        domain_configs=result['domain_configs'])
        assert selected == Path(path)
        value = json.loads(selected.read_text())
        if domain == 'vllm-ascend':
            continue
        settings = from_engine_config(value)
        assert settings.repository == 'mindie-agent/knowledge-' + domain
        assert not settings.allows_capture() and settings.project_roots == []
        assert settings.consent['choice'] == 'contribute'
        assert not Path(value['admission_path']).exists()
        assert value['feeds'][0]['domain'] == domain
        generations.add(settings.generation)
        admissions.add(value['admission_path'])
        roots.add(str(Path(value['root']) / domain))
    assert len(generations) == len(admissions) == len(roots) == 6
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert 'domain_configs' not in adapter


def test_update_refreshes_owned_native_wiring_preserving_policy_and_manual_routes(tmp_path):
    adapter, engine, options = initial(tmp_path)
    first = prepare(adapter, engine, **options)
    adapter['domain_configs'] = first['domain_configs']
    operators = json.loads(Path(adapter['domain_configs']['operators']).read_text())
    sharing = Path(operators['community_config'])
    customized = json.loads(sharing.read_text())
    customized.update(enabled=True, enabled_at=1, project_roots=[str(tmp_path)],
                      repository='fixture/explicit-destination', generation='explicit-domain-policy')
    sharing.write_text(json.dumps(customized))
    expected = sharing.read_bytes()
    custom_path = tmp_path / 'custom.engine.json'
    custom = {**engine, 'domain': 'custom', 'community_config': str(sharing)}
    custom_path.write_text(json.dumps(custom))
    adapter['domain_configs']['custom'] = str(custom_path)
    new = prepare(adapter, {**engine, 'summary_command': [sys.executable, 'new-native-worker.py']},
                  **{**options, 'config_directory': str(tmp_path / 'runtime-2')})
    updated = json.loads(Path(new['domain_configs']['operators']).read_text())
    assert updated['summary_command'][-1] == 'new-native-worker.py'
    for key in ('root', 'admission_path', 'community_config', 'feeds'):
        assert updated[key] == operators[key]
    assert sharing.read_bytes() == expected
    assert new['domain_configs']['custom'] == str(custom_path)
    assert not new['created_disabled_settings']


def test_no_public_feed_install_does_not_create_extra_engines_or_settings(tmp_path):
    adapter, engine, options = initial(tmp_path)
    result = prepare(adapter, engine, public_feeds=False, **options)
    assert result['domain_configs'] == {'vllm-ascend': adapter['engine_config']}
    assert not Path(options['authority_directory']).exists()


def test_same_install_directory_never_overwrites_an_existing_domain_binding(tmp_path):
    adapter, engine, options = initial(tmp_path)
    first = prepare(adapter, engine, **options)
    adapter['domain_configs'] = first['domain_configs']
    path = Path(first['domain_configs']['operators'])
    before = path.read_bytes()
    second = prepare(adapter, {**engine, 'summary_command': [sys.executable, 'updated-worker.py']}, **options)
    assert second['domain_configs']['operators'] != str(path)
    assert path.read_bytes() == before
    assert json.loads(Path(second['domain_configs']['operators']).read_text())['summary_command'][-1] == 'updated-worker.py'


def test_busy_later_domain_restores_earlier_exact_retirement(tmp_path):
    import threading
    import time
    from contextlib import closing
    from mindie_knowledge.loop import cli, lifecycle, transport
    from mindie_knowledge.loop.domain_catalog import retire_changed
    from mindie_knowledge.loop.engine import Engine
    from mindie_knowledge.loop.locks import StartLock
    from mindie_knowledge.loop.store import Store

    adapter, engine, options = initial(tmp_path)
    prepared = prepare(adapter, engine, **options)
    routes = {key: prepared['domain_configs'][key] for key in ('operators', 'infer-core')}
    previous = {**adapter, 'domain_configs': routes}
    candidate = {**adapter, 'domain_configs': {key: path + '.next' for key, path in routes.items()}}
    selected = json.loads(Path(routes['infer-core']).read_text())
    connection = cli.connection_path(selected)
    with closing(Store(selected['root'], selected['domain'])) as store, StartLock(connection.with_name('consumer.lock')):
        worker = Engine(store)
        service = transport.Service(worker, connection_path=connection,
            config_fingerprint=lifecycle.config_fingerprint(routes['infer-core'], selected))
        thread = threading.Thread(target=service.serve, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 3
            while not connection.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert worker.begin_work()
            result = retire_changed(previous, candidate, timeout=3)
            assert result['status'] == 'deferred' and result['restoration']['status'] == 'restored'
            assert lifecycle.inspect_retirement(routes['operators']) is None
            assert lifecycle.inspect_retirement(routes['infer-core']) is None
            assert transport.rpc(service.connection, 'status')['worker_alive']
            worker.end_work()
        finally:
            service.close()
            thread.join(timeout=3)


def test_retry_restores_exact_earlier_retirement_when_binding_was_not_published(tmp_path):
    from mindie_knowledge.loop import lifecycle
    from mindie_knowledge.loop.domain_catalog import retire_changed, restore_changed

    adapter, engine, options = initial(tmp_path)
    prepared = prepare(adapter, engine, **options)
    old_path = prepared['domain_configs']['operators']
    first = lifecycle.retire_service(old_path)
    assert first['idle'] is True
    previous = {**adapter, 'domain_configs': {'operators': old_path}}
    candidate = {**adapter, 'domain_configs': {'operators': old_path + '.next'}}
    retry = retire_changed(previous, candidate)
    assert retry['status'] == 'retired'
    assert retry['domains'][0]['retirement'] == first['retirement']
    assert restore_changed(retry)['status'] == 'restored'
    assert lifecycle.inspect_retirement(old_path) is None



def test_native_catalog_cli_uses_utf8_with_a_legacy_stream_locale(tmp_path):
    import os
    import subprocess
    root = tmp_path / "工作𠀀"
    root.mkdir()
    adapter, engine, options = initial(root)
    request = dict(adapter=adapter, engine=engine, domains=[], **options)
    result = subprocess.run(
        [sys.executable, "-I", "-m", "mindie_knowledge.loop.domain_catalog"],
        input=json.dumps(request, ensure_ascii=False).encode("utf-8"), capture_output=True,
        env={**os.environ, "PYTHONUTF8": "0", "PYTHONIOENCODING": "cp1252"}, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["domain_configs"] == {engine["domain"]: adapter["engine_config"]}
