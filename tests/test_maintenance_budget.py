"""Current bounded process ownership; legacy organizer recovery is removed."""
import os
import re
import shutil
import signal
import subprocess
import sys
import time

import pytest

from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.process import bounded_run
from mindie_knowledge.loop.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path, "vllm-ascend")
    yield value
    value.close()






def test_status_exposes_missing_publisher_without_spawning_or_blocking_store(store, monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None if name == "gh" else "/git")
    status = Engine(store).status()
    assert status["publication_runtime"] == dict(
        state="unavailable", missing=["gh"], authentication="not-checked",
    )














def test_replayed_stops_dedupe_one_capture(store):
    first = store.add_capture(
        root_session="r", session="s", turn="t", transcript=None, summary="same"
    )
    for _ in range(110):
        again = store.add_capture(
            root_session="r", session="s", turn="t", transcript=None, summary="same"
        )
        assert again["duplicate"]
    assert first["id"] == again["id"]




def test_bounded_runner_timeout_and_output_limit():
    with pytest.raises(TimeoutError, match=r"category=deadline; elapsed=\d+\.\d+s"):
        bounded_run(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            "input",
            timeout=0.2,
            max_output=4096,
        )
    with pytest.raises(ValueError, match=r"category=output_limit; elapsed=\d+\.\d+s"):
        bounded_run(
            [sys.executable, "-c", "print('x'*100000)"],
            "input",
            timeout=2,
            max_output=4096,
        )


@pytest.mark.parametrize("code, category", [
    (78, "configuration"), (124, "deadline"), (70, "native"),
    (65, "invalid_result"), (75, "output_limit"), (2, "unknown"),
])
def test_failed_process_keeps_only_safe_diagnostic(code, category):
    command = [sys.executable, "-c",
               f"import sys; print('PRIVATE_TASK_SECRET', file=sys.stderr); sys.exit({code})"]
    with pytest.raises(RuntimeError) as failure:
        bounded_run(command, "", timeout=2, max_output=4096)
    assert re.fullmatch(
        rf"maintenance agent exited {code}; category={category}; elapsed=\d+\.\d+s",
        str(failure.value),
    )




def test_timeout_stops_descendants(tmp_path):
    marker = tmp_path / "escaped"
    child = f"import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).touch()"
    parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(10)"
    with pytest.raises(TimeoutError):
        bounded_run([sys.executable, "-c", parent], "", timeout=0.2, max_output=4096)
    time.sleep(1.1)
    assert not marker.exists()


def _pid_running(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_state(pid):
    """Return the OS state token, or None when this pid cannot be observed."""
    stat_path = f"/proc/{pid}/stat"
    if os.path.isdir("/proc"):
        try:
            with open(stat_path, encoding="ascii", errors="surrogateescape") as handle:
                text = handle.read()
        except FileNotFoundError:
            return None
        marker = text.rfind(")")
        if marker < 0:
            return None
        fields = text[marker + 1:].split()
        return fields[0] if fields else None
    executable = shutil.which("ps")
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "-o", "stat=", "-p", str(pid)],
            capture_output=True, text=True, timeout=1,
            env={**os.environ, "LC_ALL": "C", "COLUMNS": "80", "LINES": "24"},
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.split()[0]


def _proven_zombie(pid):
    """An unreaped zombie still answers kill(pid, 0) but cannot run."""
    state = _process_state(pid)
    return bool(state) and state.startswith("Z") and all(
        c in "<>NLSsl+EWX" for c in state[1:]
    )


def _pid_can_run(pid):
    return _pid_running(pid) and not _proven_zombie(pid)


def _group_can_run(pgid):
    """True only when the owned group still contains a process that can run.

    Linux killpg(pgid, 0) succeeds while an unreaped zombie holds the group.
    macOS reports that same zombie-only group as EPERM. Neither is a survivor.
    """
    from mindie_knowledge.loop.process import _posix_group_has_writers

    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return _posix_group_has_writers(pgid, timeout=1)


def _group_members(pgid):
    executable = shutil.which("ps")
    if executable is None:
        return "ps is unavailable"
    try:
        result = subprocess.run(
            [executable, "-o", "pid=,ppid=,pgid=,stat=", "-g", str(pgid)],
            capture_output=True, text=True, timeout=1,
            env={**os.environ, "LC_ALL": "C", "COLUMNS": "80", "LINES": "24"},
        )
    except subprocess.TimeoutExpired:
        return "ps timed out"
    text = (result.stdout or result.stderr or "").strip()
    return text or f"ps exit {result.returncode}"


def _wait_until(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _set_child_subreaper(enabled):
    """Hold orphaned descendants until this process waits for them."""
    import ctypes

    # Linux prctl(PR_SET_CHILD_SUBREAPER). Absent on other POSIX hosts.
    pr_set_child_subreaper = 36
    try:
        prctl = ctypes.CDLL(None, use_errno=True).prctl
    except (AttributeError, OSError):
        return False
    if prctl(pr_set_child_subreaper, 1 if enabled else 0, 0, 0, 0) != 0:
        return False
    return True


def _cancelled_sleeping_tree(marker):
    """Cancel a sleeping parent and the child it records. Return their pids."""
    import threading

    from mindie_knowledge.loop import process as process_module
    from mindie_knowledge.loop.process import MaintenanceCancelled

    child = "import time; time.sleep(30)"
    parent = (
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        f"Path({str(marker)!r}).write_text(f'{{os.getpid()}} {{child.pid}}')\n"
        "time.sleep(30)\n"
    )
    spawned = []
    real_spawn = process_module._spawn

    def recording_spawn(command, stdin, scratch=None, child_env=None):
        proc = real_spawn(command, stdin, scratch, child_env)
        spawned.append(proc)
        return proc

    cancel = threading.Event()
    process_module._spawn = recording_spawn
    outcome = []

    def work():
        try:
            process_module.bounded_run(
                [sys.executable, "-c", parent],
                "{}",
                timeout=60,
                max_output=1024,
                cancel=cancel,
            )
        except MaintenanceCancelled:
            outcome.append("cancelled")

    thread = threading.Thread(target=work)
    thread.start()
    try:
        assert _wait_until(marker.exists, 5), "parent never recorded descendant pids"
        parent_pid, child_pid = map(int, marker.read_text().split())
        assert _pid_can_run(parent_pid) and _pid_can_run(child_pid)
        cancel.set()
        thread.join(timeout=10)
        assert not thread.is_alive(), "cancellation did not interrupt bounded_run"
        assert outcome == ["cancelled"]
        return parent_pid, child_pid, list(spawned)
    finally:
        cancel.set()
        thread.join(timeout=2)
        for proc in spawned:
            process_module.terminate_tree(proc)
        process_module._spawn = real_spawn


def _assert_cancelled_tree_cannot_run(parent_pid, child_pid, spawned):
    assert _wait_until(lambda: not _pid_can_run(parent_pid), 2), "parent can still run"
    assert _wait_until(lambda: not _pid_can_run(child_pid), 2), "child can still run"
    if os.name != "nt" and spawned:
        pgid = spawned[0].pid
        assert pgid == parent_pid
        assert _wait_until(lambda: not _group_can_run(pgid), 2), (
            "cancellation left a live process in the owned group\n"
            + _group_members(pgid)
        )


def test_bounded_run_cancellation_kills_the_whole_process_tree(tmp_path):
    """Cancellation stops the sleeping parent, its child, and any group peer."""
    parent_pid, child_pid, spawned = _cancelled_sleeping_tree(tmp_path / "pids")
    _assert_cancelled_tree_cannot_run(parent_pid, child_pid, spawned)


def test_unreaped_zombie_group_is_not_a_running_tree(tmp_path):
    """killpg still sees a zombie the reaper has not collected.

    That is the Linux failure behind Package checks run 42010527973: the
    owner-pipe watcher and the child are already dead, but the group id
    remains until they are reaped.
    """
    if os.name == "nt" or not hasattr(os, "killpg"):
        pytest.skip("POSIX process groups")
    if not _set_child_subreaper(True):
        pytest.skip("child subreaper is unavailable")
    parent_pid = None
    try:
        parent_pid, child_pid, spawned = _cancelled_sleeping_tree(tmp_path / "pids")
        _assert_cancelled_tree_cannot_run(parent_pid, child_pid, spawned)
        try:
            os.killpg(parent_pid, 0)
        except ProcessLookupError as exc:
            raise AssertionError(
                "the unreaped group disappeared; this no longer reproduces the race"
            ) from exc
        rows = [line.split() for line in _group_members(parent_pid).splitlines()]
        assert rows, "zombie group was empty"
        assert all(len(row) >= 4 and row[3].startswith("Z") for row in rows), rows
        assert not any(int(row[0]) == parent_pid for row in rows)
        assert any(int(row[0]) == child_pid for row in rows)
    finally:
        if parent_pid is not None:
            for line in _group_members(parent_pid).splitlines():
                parts = line.split()
                if not parts or not parts[0].isdigit():
                    continue
                pid = int(parts[0])
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    try:
                        reaped, _status = os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        break
                    if reaped:
                        break
                    time.sleep(0.01)
        if not _set_child_subreaper(False) and sys.exc_info()[1] is None:
            raise RuntimeError("child subreaper could not be cleared")
