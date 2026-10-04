"""A removed temporary profile must not leave a detached service forever."""
from contextlib import closing
from pathlib import Path

from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store
from mindie_knowledge.loop.transport import Service


def test_removed_config_retires_only_after_grace_and_idle(tmp_path, monkeypatch):
    config = tmp_path / 'engine.json'
    config.write_text('{}')
    now = [100.0]
    monkeypatch.setattr('mindie_knowledge.loop.transport.time.monotonic', lambda: now[0])
    with closing(Store(tmp_path / 'data', 'test')) as store:
        engine = Engine(store)
        service = Service(engine, config_path=config)
        closed = []
        # Exercise the actual admission/freeze decision without starting HTTP.
        monkeypatch.setattr(service, 'close', lambda: closed.append(True))
        try:
            service.http.service_actions()
            config.unlink()
            now[0] = 130.0
            service.http.service_actions()
            assert not engine._frozen
            assert engine.begin_work()
            now[0] = 160.0
            service.http.service_actions()
            assert not engine._frozen  # Never cancel a capture/publication.
            engine.end_work()
            now[0] = 190.0
            service.http.service_actions()
            assert engine._frozen and service._config_retired
        finally:
            service.http.server_close()


def test_atomic_replacement_and_stat_errors_do_not_retire(tmp_path, monkeypatch):
    config = tmp_path / 'engine.json'
    now = [100.0]
    monkeypatch.setattr('mindie_knowledge.loop.transport.time.monotonic', lambda: now[0])
    with closing(Store(tmp_path / 'data', 'test')) as store:
        service = Service(Engine(store), config_path=config)
        monkeypatch.setattr(service, '_stop_if_idle', lambda: (_ for _ in ()).throw(AssertionError('must stay alive')))
        try:
            service.http.service_actions()  # Missing during replacement.
            config.write_text('{}')
            now[0] = 130.0
            service.http.service_actions()
            assert service._config_missing_since is None
            config.unlink()
            now[0] = 160.0
            service.http.service_actions()
            real_stat = Path.stat
            def denied(path, *args, **kwargs):
                if path == config:
                    raise PermissionError('temporarily inaccessible')
                return real_stat(path, *args, **kwargs)
            monkeypatch.setattr(Path, 'stat', denied)
            now[0] = 190.0
            service.http.service_actions()
            assert service._config_missing_since is None
        finally:
            service.http.server_close()


def test_embedded_service_needs_no_config_file(tmp_path, monkeypatch):
    with closing(Store(tmp_path / 'data', 'test')) as store:
        service = Service(Engine(store))
        monkeypatch.setattr(service, '_stop_if_idle', lambda: (_ for _ in ()).throw(AssertionError('must stay alive')))
        try:
            service.http.service_actions()
        finally:
            service.http.server_close()
