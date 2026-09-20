"""Admission gate against adapter leases and community settings."""

import hashlib
import sqlite3
import time

import pytest

from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.store import session_key

from conftest import make_admission


def test_missing_store_never_created_and_fails_closed(tmp_path):
    gate = Admission(tmp_path / "adapter.json")
    assert not gate.allows_hash(session_key("inactive"))
    with pytest.raises(ValueError):
        gate.check("inactive", "guessed")
    assert not list(tmp_path.iterdir())


def test_old_lease_schema_reads_for_status_but_capture_fails_closed(tmp_path):
    adapter = make_admission(tmp_path, project_root=tmp_path, capture_schema=False)
    gate = Admission(adapter)
    lease = gate.check("manual-A", "cap-A")  # identity still readable
    assert lease["session"] == "manual-A"
    with pytest.raises(ValueError, match="predates this schema"):
        gate.capture_lease("manual-A", "cap-A")


def test_capture_lease_and_scope_root(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    adapter = make_admission(tmp_path, project_root=project, root_session="root-1")
    gate = Admission(adapter)
    lease = gate.capture_lease("manual-A", "cap-A")
    assert lease["root_session"] == "root-1"
    assert gate.scope_root("manual-A") == str(project)
    with pytest.raises(ValueError):
        gate.capture_lease("manual-A", "wrong-token")
    with pytest.raises(ValueError):
        gate.capture_lease("other", "cap-A")


def test_expiry_and_config_change_revoke(tmp_path):
    adapter = make_admission(tmp_path, project_root=tmp_path)
    gate = Admission(adapter)
    assert gate.active_lease("manual-A")
    with sqlite3.connect(adapter.with_suffix(".sessions.sqlite3")) as db:
        db.execute("UPDATE leases SET expires=?", (time.time() - 1,))
    assert gate.active_lease("manual-A") is None
