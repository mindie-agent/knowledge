"""Neutral admission store: canonical SessionGate semantics."""

from contextlib import closing
import os
import sqlite3
import time

import pytest

from mindie_knowledge.loop.activation import Admission, AdmissionUnavailable
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
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE leases SET activated_at=?", (time.time() - 10 * 86400,))
    assert Admission(path).active_lease("manual-A")


def test_claim_consumes_exactly_once_and_finish_is_diagnostic_only(tmp_path):
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
    gate.finish("manual-A", token, True)  # a valid success resets the counter
    assert gate.active_lease("manual-A")["failures"] == 0
    for _ in range(5):
        gate.finish("manual-A", token, False)
    # Failure counts never pause or revoke the binding: claims and capture
    # authorization keep working and a repeat activate preserves the token.
    assert gate.active_lease("manual-A")["failures"] == 5
    assert gate.claim("manual-A", "hook", "update:0.9.0", token=token) is True
    again = gate.activate("manual-A", project_root=str(tmp_path))
    assert again["token"] == token and again["enabled"] is True
    assert gate.capture_authorization("manual-A", token, timeout=0.1)["state"] == "admitted"
    # Explicit revoke still rotates the token on the next fresh activation.
    gate.deactivate("manual-A")
    fresh = gate.activate("manual-A", project_root=str(tmp_path))
    assert gate.active_lease("manual-A")["failures"] == 0
    assert fresh["token"] != token
    with pytest.raises(ValueError):
        gate.finish("manual-A", token, True)  # rotated-out token is dead


def test_incomplete_lease_schema_cannot_authorize_status_or_capture(tmp_path):
    path = tmp_path / "admission.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE leases(session TEXT, token TEXT, enabled INTEGER, "
            "failures INTEGER)"
        )
        db.execute("INSERT INTO leases VALUES('manual-A', 'cap-A', 1, 0)")
    gate = Admission(path)
    before = path.read_bytes()
    with pytest.raises(AdmissionUnavailable, match="state"):
        gate.check("manual-A", "cap-A")
    with pytest.raises(AdmissionUnavailable, match="state"):
        gate.capture_lease("manual-A", "cap-A")
    assert path.read_bytes() == before
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT * FROM leases").fetchall() == [("manual-A", "cap-A", 1, 0)]
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='attempts'").fetchone()


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor barrier")
@pytest.mark.parametrize("observer", ["writer", "reader"])
def test_first_open_does_not_treat_a_created_lock_as_lost_authority(tmp_path, observer):
    """A created-but-not-yet-locked .init.lock is not authority loss.

    A opens/creates the real init lock, then pauses on an OS pipe at the exact
    open-to-flock interval. B therefore observes the lock file as pre-existing
    and acquires the kernel lock first while no database exists. B's real
    post-flock barrier proves it owned the kernel lock; a second initializer
    must create the store and share its token, while a read-only probe must
    report an unused store, leave the DB absent, and write no history.
    """
    import json
    import select
    import subprocess
    import sys
    from pathlib import Path

    project = tmp_path / "proj"
    project.mkdir()
    path = tmp_path / "admission.sqlite3"
    lock_path = path.with_name(path.name + ".init.lock")
    core_root = Path(__file__).resolve().parents[1]
    starter = tmp_path / "starter.py"
    starter.write_text(
        "import json,os,sys\n"
        f"sys.path.insert(0, {str(core_root)!r})\n"
        "import mindie_knowledge.loop.locks as locks\n"
        "from mindie_knowledge.loop.activation import Admission\n"
        "real = locks._lock_file_nb\n"
        "def between_open_and_flock(fd):\n"
        "    os.write(int(sys.argv[2]), b'r')\n"
        "    os.close(int(sys.argv[2]))\n"
        "    if os.read(int(sys.argv[3]), 1) != b'g':\n"
        "        raise SystemExit('barrier gate closed')\n"
        "    os.close(int(sys.argv[3]))\n"
        "    real(fd)\n"
        "locks._lock_file_nb = between_open_and_flock\n"
        "print(json.dumps(Admission(sys.argv[1]).activate("
        "'shared', project_root=sys.argv[4])))\n"
    )
    second = tmp_path / "second.py"
    second.write_text(
        "import json,os,sys\n"
        f"sys.path.insert(0, {str(core_root)!r})\n"
        "import mindie_knowledge.loop.locks as locks\n"
        "from mindie_knowledge.loop.activation import Admission\n"
        "real = locks.StartLock.acquire\n"
        "def after_acquire(self, **kw):\n"
        "    real(self, **kw)\n"
        "    if self.path.name.endswith('.init.lock'):\n"
        "        os.write(int(sys.argv[3]), b'f')\n"
        "        os.read(int(sys.argv[4]), 1)\n"
        "locks.StartLock.acquire = after_acquire\n"
        "gate = Admission(sys.argv[1])\n"
        + ("result = gate.activate('shared', project_root=sys.argv[2])\n"
           if observer == "writer" else
           "result = gate.active_lease('shared')\n")
        + "print(json.dumps(result))\n"
    )
    ready_read, ready_write = os.pipe()
    gate_read, gate_write = os.pipe()
    b_ready_r, b_ready_w = os.pipe()
    b_gate_r, b_gate_w = os.pipe()
    first = second_proc = None
    try:
        first = subprocess.Popen(
            [sys.executable, str(starter), str(path), str(ready_write),
             str(gate_read), str(project)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            pass_fds=(ready_write, gate_read),
        )
        os.close(ready_write)
        ready_write = None
        os.close(gate_read)
        gate_read = None
        readable, _, _ = select.select([ready_read], [], [], 20)
        assert readable, "starter never reached the open-to-flock interval"
        assert os.read(ready_read, 1) == b"r"
        second_proc = subprocess.Popen(
            [sys.executable, str(second), str(path), str(project),
             str(b_ready_w), str(b_gate_r)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            pass_fds=(b_ready_w, b_gate_r),
        )
        os.close(b_ready_w)
        b_ready_w = None
        os.close(b_gate_r)
        b_gate_r = None
        readable, _, _ = select.select([b_ready_r], [], [], 20)
        assert readable, "observer never reached its post-flock barrier"
        assert os.read(b_ready_r, 1) == b"f"
        from mindie_knowledge.loop.locks import lock_held
        assert lock_held(lock_path) is True
        assert not path.exists()
        assert lock_path.stat().st_size == 0  # no pre-DB record for either
        os.write(b_gate_w, b"g")
        os.close(b_gate_w)
        b_gate_w = None
        out_b, err_b = second_proc.communicate(timeout=20)
        assert second_proc.returncode == 0, err_b
        if observer == "reader":
            assert json.loads(out_b) is None
            assert not path.exists()
            assert lock_path.stat().st_size == 0
        else:
            assert path.exists()
            assert lock_path.stat().st_size > 0
        os.write(gate_write, b"g")
        os.close(gate_write)
        gate_write = None
        out_a, err_a = first.communicate(timeout=20)
        assert first.returncode == 0, err_a
        token_a = json.loads(out_a)["token"]
        assert len(token_a) == 64
        if observer == "writer":
            assert token_a == json.loads(out_b)["token"]
        else:
            assert Admission(path).active_lease("shared")["token"] == token_a
    finally:
        for process in (first, second_proc):
            if process is not None and process.poll() is None:
                process.kill()
            if process is not None:
                process.communicate(timeout=10)
        for descriptor in (ready_read, ready_write, gate_read, gate_write,
                           b_ready_r, b_ready_w, b_gate_r, b_gate_w):
            if descriptor is not None:
                os.close(descriptor)


@pytest.mark.skipif(os.name == "nt", reason="POSIX pipe/SIGKILL barrier")
def test_pre_db_process_failure_leaves_never_used_store_retryable(tmp_path):
    """A crash after real flock but before DB creation must not persist evidence.

    The child is paused after the real kernel lock is acquired and before
    sqlite3.connect. The parent verifies real lock ownership with lock_held,
    SIGKILLs the child while no DB exists, and requires the next genuine
    activation to succeed. It then deletes the initialized DB and requires the
    preserved missing-authority failure.
    """
    import json
    import select
    import signal
    import subprocess
    import sys
    from pathlib import Path

    project = tmp_path / "proj"
    project.mkdir()
    path = tmp_path / "admission.sqlite3"
    lock_path = path.with_name(path.name + ".init.lock")
    core_root = Path(__file__).resolve().parents[1]
    crash = tmp_path / "pre_db_crash.py"
    crash.write_text(
        "import os,sys\n"
        f"sys.path.insert(0, {str(core_root)!r})\n"
        "import mindie_knowledge.loop.locks as locks\n"
        "from mindie_knowledge.loop.activation import Admission\n"
        "real = locks.StartLock.acquire\n"
        "def after_acquire(self, **kw):\n"
        "    real(self, **kw)\n"
        "    os.write(int(sys.argv[2]), b'a')\n"
        "    os.close(int(sys.argv[2]))\n"
        "    os.read(int(sys.argv[3]), 1)\n"
        "    raise SystemExit('parent did not kill')\n"
        "locks.StartLock.acquire = after_acquire\n"
        "Admission(sys.argv[1]).activate('shared', project_root=sys.argv[4])\n"
    )
    ready_read, ready_write = os.pipe()
    gate_read, gate_write = os.pipe()
    child = None
    try:
        child = subprocess.Popen(
            [sys.executable, str(crash), str(path), str(ready_write),
             str(gate_read), str(project)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            pass_fds=(ready_write, gate_read),
        )
        os.close(ready_write)
        ready_write = None
        os.close(gate_read)
        gate_read = None
        readable, _, _ = select.select([ready_read], [], [], 20)
        assert readable, "child never reached the post-flock barrier"
        assert os.read(ready_read, 1) == b"a"
        from mindie_knowledge.loop.locks import lock_held
        assert lock_held(lock_path) is True
        assert not path.exists()
        assert lock_path.stat().st_size == 0
        child.kill()
        _, err = child.communicate(timeout=20)
        assert child.returncode == -signal.SIGKILL, err
        assert not path.exists()
        assert lock_path.stat().st_size == 0

        lease = Admission(path).activate("shared", project_root=str(project))
        assert len(lease["token"]) == 64
        assert path.exists()
        assert lock_path.stat().st_size > 0

        path.unlink()
        with pytest.raises(AdmissionUnavailable, match="missing"):
            Admission(path).activate("shared", project_root=str(project))
        with pytest.raises(AdmissionUnavailable, match="missing"):
            Admission(path).leases()
        assert not path.exists()
    finally:
        if child is not None and child.poll() is None:
            child.kill()
        if child is not None:
            child.communicate(timeout=10)
        for descriptor in (ready_read, ready_write, gate_read, gate_write):
            if descriptor is not None:
                os.close(descriptor)


def test_authority_record_write_failure_is_visible_and_repairable(tmp_path, monkeypatch):
    """The record is authority evidence: its write failure is not swallowed."""
    from mindie_knowledge.loop.locks import StartLock

    path = tmp_path / "admission.sqlite3"
    lock_path = path.with_name(path.name + ".init.lock")
    project = tmp_path / "project"
    project.mkdir()

    def fail(_lock):
        raise OSError("synthetic authority record failure")

    with monkeypatch.context() as patch:
        patch.setattr(StartLock, "persist_record", fail)
        with pytest.raises(AdmissionUnavailable, match="authority record failure"):
            Admission(path).activate("shared", project_root=str(project))

    assert path.is_file()  # the validated database was created, not deleted
    assert lock_path.stat().st_size == 0  # no record was silently claimed
    repaired = Admission(path).activate("shared", project_root=str(project))
    assert len(repaired["token"]) == 64
    assert lock_path.stat().st_size > 0
    assert Admission(path).active_lease("shared")["token"] == repaired["token"]
