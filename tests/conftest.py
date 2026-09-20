"""Shared pytest fixtures for the knowledge loop test-suite."""

import pytest

from mindie_knowledge.loop import settings as settings_mod
from mindie_knowledge.loop.activation import Admission
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


def make_admission(tmp_path, *, project_root, session="manual-A",
                   root_session=None):
    """One neutral admission store with the session explicitly activated.
    Returns the admission SQLite path."""
    path = tmp_path / "admission.sqlite3"
    Admission(path).activate(session, project_root=str(project_root),
                             root_session=root_session)
    return path


def admission_token(path, session="manual-A"):
    return Admission(path).active_lease(session)["token"]
