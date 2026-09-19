import hashlib
import json
import sqlite3
import time
from unittest.mock import Mock

import pytest

from mindie_knowledge.loop.activation import SessionAdmission
from mindie_knowledge.loop import cli
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store, session_key
from mindie_knowledge.loop.transport import Service


def activated(tmp_path):
    config, engine_config = tmp_path / "adapter.json", tmp_path / "engine.json"
    engine_config.write_text(
        json.dumps(dict(root=str(tmp_path), domain="test", agent_command=["never"]))
    )
    config.write_text(json.dumps(dict(engine_config=str(engine_config))))
    fingerprint = hashlib.sha256(
        config.read_bytes() + b"\0" + engine_config.read_bytes()
    ).hexdigest()
    with sqlite3.connect(config.with_suffix(".sessions.sqlite3")) as db:
        db.execute(
            "CREATE TABLE leases(session TEXT,token TEXT,fingerprint TEXT,expires REAL,enabled INTEGER,failures INTEGER)"
        )
        db.execute(
            "INSERT INTO leases VALUES(?,?,?,?,1,0)",
            ("manual-A", "cap-A", fingerprint, time.time() + 60),
        )
    return config, engine_config


def test_read_only_admission_does_not_create_missing_database(tmp_path):
    config = tmp_path / "adapter.json"
    gate = SessionAdmission(config)
    assert not gate.allows(session_key("inactive"))
    with pytest.raises(ValueError):
        gate.require("inactive", "guessed")
    assert not list(tmp_path.iterdir())


def test_service_rejects_old_clients_and_cross_session_before_store_access(tmp_path):
    config, _ = activated(tmp_path)
    store = Store(tmp_path / "store", "test")
    engine = Engine(store, agent_command=["never"])
    service = Service(engine, session_activation=str(config))
    try:
        with pytest.raises(ValueError):
            service.call("query", dict(query="test", session_id="manual-A"))
        with pytest.raises(ValueError):
            service.call(
                "query",
                dict(
                    query="test",
                    session_id="other",
                    _session_id="manual-A",
                    _activation="cap-A",
                ),
            )
        assert not store.attached("manual-A")
        service.call(
            "query",
            dict(
                query="test",
                session_id="manual-A",
                _session_id="manual-A",
                _activation="cap-A",
            ),
        )
        assert store.attached("manual-A")
        with pytest.raises(ValueError):
            service.call(
                "capture", dict(session_id="manual-A", turn_id="t", summary="result")
            )
        with pytest.raises(ValueError):
            service.call("explain", dict(ref="any"))
        assert engine.queue.empty()
    finally:
        service.http.server_close()
        store.db.close()


def test_deactivation_and_expiry_prevent_queued_model_admission(tmp_path):
    config, _ = activated(tmp_path)
    store = Store(tmp_path / "store", "test")
    engine = Engine(store, agent_command=["never"])
    engine.session_allowed = SessionAdmission(config).allows
    assert engine.session_allowed(session_key("manual-A"))
    with sqlite3.connect(config.with_suffix(".sessions.sqlite3")) as db:
        db.execute("UPDATE leases SET enabled=0")
    with pytest.raises(ValueError, match="no longer active"):
        engine.agent("organize", {}, attempt_id="x", session=session_key("manual-A"))
    assert engine.budget.status()["calls_last_hour"] == 0
    store.db.close()


def test_configuration_change_revokes_admission(tmp_path):
    config, engine_config = activated(tmp_path)
    gate = SessionAdmission(config)
    gate.require("manual-A", "cap-A")
    engine_config.write_text("{}")
    with pytest.raises(ValueError):
        gate.require("manual-A", "cap-A")


def test_startup_uses_one_spawn_and_bounded_readiness_probes(tmp_path, monkeypatch):
    _, config = activated(tmp_path)
    connection = dict(url="http://127.0.0.1:1", token="test", domain="test")
    monkeypatch.setattr(cli, "connect", lambda _: connection)
    rpc = Mock(side_effect=OSError("unavailable"))
    monkeypatch.setattr(cli, "rpc", rpc)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    process = Mock(pid=99999999)
    process.poll.return_value = None
    spawn = Mock(return_value=process)
    monkeypatch.setattr(cli.subprocess, "Popen", spawn)
    kill = Mock()
    monkeypatch.setattr(cli.os, "killpg", kill)
    with pytest.raises(RuntimeError, match="bounded readiness"):
        cli.ensure_service(config)
    assert spawn.call_count == 1
    assert rpc.call_count == 2 + cli.MAX_STARTUP_PROBES
    assert kill.call_count == 1
    process.wait.assert_called_once()
