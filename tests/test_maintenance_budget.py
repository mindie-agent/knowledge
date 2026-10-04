"""Current bounded process ownership; legacy organizer recovery is removed."""
import os
import re
import sys
import time

import pytest

from mindie_knowledge.loop.budget import BudgetExceeded
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


def _wait_until(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_bounded_run_cancellation_kills_the_whole_process_tree(tmp_path):
    """A real sleeping parent+child are both gone after cancellation."""
    import threading

    from mindie_knowledge.loop import process as process_module
    from mindie_knowledge.loop.process import MaintenanceCancelled

    marker = tmp_path / "pids"
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

    def recording_spawn(command, stdin):
        proc = real_spawn(command, stdin)
        spawned.append(proc)
        return proc

    cancel = threading.Event()
    process_module._spawn = recording_spawn
    try:
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
        assert _wait_until(marker.exists, 5), "parent never recorded descendant pids"
        parent_pid, child_pid = map(int, marker.read_text().split())
        assert _pid_running(parent_pid) and _pid_running(child_pid)
        cancel.set()
        thread.join(timeout=10)
        assert not thread.is_alive(), "cancellation did not interrupt bounded_run"
        assert outcome == ["cancelled"]
        assert _wait_until(lambda: not _pid_running(parent_pid), 2)
        assert _wait_until(lambda: not _pid_running(child_pid), 2)
        if os.name != "nt" and spawned:
            with pytest.raises(ProcessLookupError):
                os.killpg(spawned[0].pid, 0)
    finally:
        cancel.set()
        for proc in spawned:
            process_module.terminate_tree(proc)
        process_module._spawn = real_spawn
