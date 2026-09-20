"""Neutral admission store: canonical SessionGate semantics."""

import os
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
        gate.check("inactive")
    assert gate.resolve("0" * 32) is None
    assert not list(tmp_path.iterdir())  # checks never create state


def test_activate_issues_random_token_and_preserves_it_while_healthy(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    gate = Admission(tmp_path / "admission.sqlite3")
    lease = gate.activate("manual-A", project_root=str(project), root_session="root-1")
    assert lease["capture_schema"] is True
    assert lease["project_root"] == str(project)
    assert lease["root_session"] == "root-1"
    again = gate.activate("manual-A", project_root=str(project))
    assert again["token"] == lease["token"]  # healthy re-activate is stable
    assert again["activated_at"] == lease["activated_at"]  # original boundary
    other = gate.activate("manual-B", project_root=str(project))
    assert other["token"] != lease["token"]  # random per activation
    assert gate.scope_root("manual-A") == str(project)
    assert gate.allows_hash(session_key("manual-A"))


def test_check_allows_omitted_token_and_resolve_returns_lease(tmp_path):
    path = make_admission(tmp_path, project_root=tmp_path)
    gate = Admission(path)
    lease = gate.check("manual-A")  # active task check, token optional
    assert lease["session"] == "manual-A"
    assert gate.check("manual-A", admission_token(path))["session"] == "manual-A"
    with pytest.raises(ValueError):
        gate.check("manual-A", "wrong-token")
    with pytest.raises(ValueError):
        gate.check("stranger")
    resolved = gate.resolve(admission_token(path))
    assert resolved["session"] == "manual-A"
    assert gate.capture_lease("manual-A", admission_token(path))["capture_schema"]
    with pytest.raises(ValueError):
        gate.capture_lease("manual-A", "wrong-token")


def test_deactivate_preserves_attempts_and_reactivation_rotates_token(tmp_path):
    path = make_admission(tmp_path, project_root=tmp_path)
    gate = Admission(path)
    old_token = admission_token(path)
    assert gate.claim("manual-A", "capture", "turn-1", token=old_token) is True
    assert gate.deactivate("manual-A") is True
    assert gate.active_lease("manual-A") is None
    assert gate.resolve(old_token) is None
    assert gate.deactivate("manual-A") is False
    revived = gate.activate("manual-A", project_root=str(tmp_path))
    assert revived["token"] != old_token  # rotated; no reusable capability
    assert gate.resolve(old_token) is None
    # Attempt identities survive revocation: old work is never replayed.
    assert gate.claim("manual-A", "capture", "turn-1", token=revived["token"]) is False
    assert gate.claim("manual-A", "capture", "turn-2", token=revived["token"]) is True


def test_scope_change_rotates_token_and_boundary(tmp_path):
    path = make_admission(tmp_path, project_root=tmp_path)
    gate = Admission(path)
    before = gate.active_lease("manual-A")
    other = tmp_path / "other"
    other.mkdir()
    moved = gate.activate("manual-A", project_root=str(other))
    assert moved["project_root"] == str(other)
    assert moved["token"] != before["token"]
    assert moved["activated_at"] >= before["activated_at"]


def test_authorization_has_no_wallclock_expiry(tmp_path):
    path = make_admission(tmp_path, project_root=tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE leases SET activated_at=?", (time.time() - 10 * 86400,))
    assert Admission(path).active_lease("manual-A")


def test_claim_consumes_exactly_once_and_finish_drives_circuit(tmp_path):
    path = make_admission(tmp_path, project_root=tmp_path)
    gate = Admission(path)
    token = admission_token(path)
    assert gate.claim("manual-A", "hook", "update:0.8.0", token=token) is True
    assert gate.claim("manual-A", "hook", "update:0.8.0", token=token) is False
    with pytest.raises(ValueError):
        gate.claim("stranger", "hook", "update:0.8.0")
    with pytest.raises(ValueError):
        gate.claim("manual-A", "hook", "x", token="forged")
    gate.finish("manual-A", token, False)
    gate.finish("manual-A", token, False)
    gate.finish("manual-A", token, True)  # valid success resets while healthy
    assert gate.active_lease("manual-A")["failures"] == 0
    for _ in range(3):
        gate.finish("manual-A", token, False)
    assert gate.active_lease("manual-A") is None  # circuit paused
    # Success never unpauses at >=3, and activate is not a bypass either.
    gate.finish("manual-A", token, True)
    paused = gate.activate("manual-A", project_root=str(tmp_path))
    assert paused["enabled"] is False and paused["paused"] is True
    assert paused["failures"] >= 3
    assert gate.active_lease("manual-A") is None
    with pytest.raises(ValueError):
        gate.claim("manual-A", "hook", "y", token=token)  # resolve fails closed
    # Explicit recovery: revoke, then a fresh activation.
    gate.deactivate("manual-A")
    fresh = gate.activate("manual-A", project_root=str(tmp_path))
    assert gate.active_lease("manual-A")["failures"] == 0
    assert fresh["token"] != token
    with pytest.raises(ValueError):
        gate.finish("manual-A", token, True)  # rotated-out token is dead


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_admission_storage_is_private(tmp_path):
    path = make_admission(tmp_path, project_root=tmp_path)
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (path.parent.stat().st_mode & 0o777) == 0o700


def test_finish_conditions_on_token_so_rotation_is_immune(tmp_path):
    path = make_admission(tmp_path, project_root=tmp_path)
    gate = Admission(path)
    old = admission_token(path)
    gate.deactivate("manual-A")
    fresh = gate.activate("manual-A", project_root=str(tmp_path))
    with pytest.raises(ValueError):
        gate.finish("manual-A", old, False)
    assert gate.active_lease("manual-A")["failures"] == 0
    with pytest.raises(ValueError):
        gate.claim("manual-A", "hook", "after-revoke", token=old)


def test_concurrent_processes_share_one_activation_token(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    project = tmp_path / "proj"
    project.mkdir()
    path = tmp_path / "admission.sqlite3"
    script = tmp_path / "act.py"
    core_root = Path(__file__).resolve().parents[1]
    script.write_text(
        "import json,sys\n"
        f"sys.path.insert(0, {str(core_root)!r})\n"
        "from mindie_knowledge.loop.activation import Admission\n"
        "print(json.dumps(Admission(sys.argv[1]).activate("
        "'shared', project_root=sys.argv[2])))\n"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), str(path), str(project)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(2)
    ]
    tokens = []
    for proc in procs:
        out, err = proc.communicate(timeout=20)
        assert proc.returncode == 0, err
        tokens.append(json.loads(out)["token"])
    assert tokens[0] == tokens[1]
    assert len(tokens[0]) == 64
