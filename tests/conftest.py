"""Shared pytest fixtures for the knowledge loop test-suite."""

import hashlib
import json
import sqlite3
import time

import pytest

from mindie_knowledge.loop import settings as settings_mod
from mindie_knowledge.loop.store import Store


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path, "vllm-ascend")
    yield instance
    instance.close()


def write_settings(path, *, enabled=True, repository="mindie-agent/knowledge-vllm-ascend",
                   roots=None, idle_seconds=300, **extensions):
    return settings_mod.write(
        path,
        enabled=enabled,
        repository=repository,
        project_roots=roots or [],
        idle_seconds=idle_seconds,
        **extensions,
    )


def make_admission(tmp_path, *, project_root, session="manual-A", token="cap-A",
                   root_session=None, capture_schema=True):
    """Adapter config + lease store with the current capture schema."""
    config, engine_config = tmp_path / "adapter.json", tmp_path / "engine.json"
    engine_config.write_text(json.dumps(dict(root=str(tmp_path), domain="test")))
    config.write_text(json.dumps(dict(engine_config=str(engine_config))))
    fingerprint = hashlib.sha256(
        config.read_bytes() + b"\0" + engine_config.read_bytes()
    ).hexdigest()
    columns = (
        "session TEXT,token TEXT,fingerprint TEXT,expires REAL,enabled INTEGER,"
        "failures INTEGER"
    )
    values = (session, token, fingerprint, time.time() + 60, 1, 0)
    if capture_schema:
        columns += ",project_root TEXT,root_session TEXT,activated_at REAL"
        values = values + (str(project_root), root_session or session, time.time())
    with sqlite3.connect(config.with_suffix(".sessions.sqlite3")) as db:
        db.execute(f"CREATE TABLE leases({columns})")
        db.execute(
            f"INSERT INTO leases VALUES({','.join('?' for _ in values)})", values
        )
    return config
