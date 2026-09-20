"""Neutral admission store: activation, lease checks, operation receipts."""

import sqlite3
import time

import pytest

from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.store import session_key

from conftest import admission_token, make_admission


def test_config_paths_are_rejected_and_construction_creates_nothing(tmp_path):
    with pytest.raises(ValueError, match="admission_path"):
        Admission(tmp_path / "adapter.json")
    gate = Admission(tmp_path / "admission.sqlite3")
    assert not gate.allows_hash(session_key("inactive"))
    with pytest.raises(ValueError):
        gate.check("inactive", "guessed")
    assert gate.resolve("0" * 32) is None
    assert not list(tmp_path.iterdir())  # checks never create state


def test_activate_issues_lease_and_preserves_token_and_boundary(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    path = tmp_path / "admission.sqlite3"
    gate = Admission(path)
    lease = gate.activate("manual-A", project_root=str(project), root_session="root-1")
    assert lease["capture_schema"] is True
    assert lease["project_root"] == str(project)
    assert lease["root_session"] == "root-1"
    again = gate.activate("manual-A", project_root=str(project))
    assert again["token"] == lease["token"]  # healthy re-activate is stable
    assert again["activated_at"] == lease["activated_at"]  # original boundary
    assert gate.scope_root("manual-A") == str(project)
    assert gate.allows_hash(session_key("manual-A"))
    with pytest.raises(ValueError):
        gate.capture_lease("manual-A", "wrong-token")
    with pytest.raises(ValueError):
        gate.capture_lease("other", lease["token"])


def test_authorization_has_no_wallclock_expiry(tmp_path):
    path = make_admission(tmp_path, project_root=tmp_path)
    with sqlite3.connect(path) as db:
        # An old activation stays valid for the same native task; revocation,
        # pause and scope change are the only exits.
        db.execute("UPDATE leases SET activated_at=?", (time.time() - 10 * 86400,))
    gate = Admission(path)
    assert gate.active_lease("manual-A")


def test_scope_change_starts_a_fresh_boundary(tmp_path):
    path = make_admission(tmp_path, project_root=tmp_path)
    gate = Admission(path)
    before = gate.active_lease("manual-A")
    other = tmp_path / "other"
    other.mkdir()
    moved = gate.activate("manual-A", project_root=str(other))
    assert moved["project_root"] == str(other)
    assert moved["activated_at"] >= before["activated_at"]


def test_deactivate_revokes_and_reactivate_issues_fresh_lease(tmp_path):
    path = make_admission(tmp_path, project_root=tmp_path)
    gate = Admission(path)
    assert gate.deactivate("manual-A") is True
    assert gate.active_lease("manual-A") is None
    assert gate.deactivate("manual-A") is False
    revived = gate.activate("manual-A", project_root=str(tmp_path))
    assert revived["token"] == admission_token(path)


def test_old_lease_schema_reads_for_status_but_capture_fails_closed(tmp_path):
    path = tmp_path / "admission.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE leases(session TEXT, token TEXT, enabled INTEGER, "
            "failures INTEGER)"
        )
        db.execute("INSERT INTO leases VALUES('manual-A', 'cap-A', 1, 0)")
    gate = Admission(path)
    lease = gate.check("manual-A", "cap-A")  # identity still readable
    assert lease["session"] == "manual-A"
    assert lease["capture_schema"] is False
    with pytest.raises(ValueError, match="predates this schema"):
        gate.capture_lease("manual-A", "cap-A")


def test_operation_receipts_and_failure_circuit(tmp_path):
    path = make_admission(tmp_path, project_root=tmp_path)
    gate = Admission(path)
    token = gate.claim("manual-A", "hook", "update:0.8.0")
    assert gate.claim("manual-A", "hook", "update:0.8.0") == token  # idempotent
    assert gate.resolve(token) == {
        "session": "manual-A", "kind": "hook", "identity": "update:0.8.0"
    }
    with pytest.raises(ValueError):
        gate.claim("stranger", "hook", "update:0.8.0")
    gate.finish("manual-A", token, False)
    assert gate.resolve(token) is None  # closed receipts never authorize
    assert gate.active_lease("manual-A")["failures"] == 1
    for attempt in range(2):
        receipt = gate.claim("manual-A", "hook", f"update:retry-{attempt}")
        gate.finish("manual-A", receipt, False)
    assert gate.active_lease("manual-A") is None  # circuit paused, no bypass
    # A successful finish never resets the circuit; only a fresh activate does.
    gate.activate("manual-A", project_root=str(tmp_path))
    assert gate.active_lease("manual-A") is None
    gate.deactivate("manual-A")
    gate.activate("manual-A", project_root=str(tmp_path))
    assert gate.active_lease("manual-A")["failures"] == 0
    with pytest.raises(ValueError):
        gate.finish("manual-A", token, True)  # receipts die with revocation
