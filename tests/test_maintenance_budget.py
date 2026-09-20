import concurrent.futures
import os
import sys
import time

import pytest

from mindie_knowledge.loop.budget import BudgetExceeded, MaintenanceBudget
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.process import bounded_run
from mindie_knowledge.loop.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path, "vllm-ascend")
    yield value
    value.close()


def test_durable_attempt_id_is_consumed_before_model_execution(store):
    budget = MaintenanceBudget(store)
    budget.reserve("job", "session", "organize")
    # Includes a crash between invocation and recording its result.
    budget = MaintenanceBudget(store)
    with pytest.raises(BudgetExceeded, match="already been attempted"):
        budget.reserve("job", "session", "organize")


def test_session_and_hourly_limits_apply_to_successes_too(store):
    budget = MaintenanceBudget(store)
    for i in range(6):
        budget.reserve(str(i), "s", "organize")
        budget.finish(str(i), True)
    with pytest.raises(BudgetExceeded, match="session"):
        budget.reserve("extra", "s", "organize")
    for i in range(6, 20):
        budget.reserve(str(i), str(i), "organize")
        budget.finish(str(i), True)
    with pytest.raises(BudgetExceeded, match="hourly"):
        budget.reserve("extra", "another", "organize")


def test_session_quota_rolls_forward_without_replaying_old_attempts(store, monkeypatch):
    clock=[10000.0];monkeypatch.setattr(time,'time',lambda:clock[0])
    budget=MaintenanceBudget(store)
    for i in range(6):
        budget.reserve(str(i),'s','organize');budget.finish(str(i),True)
    with pytest.raises(BudgetExceeded) as exc:budget.reserve('next','s','organize')
    assert exc.value.retry_at>clock[0]
    clock[0]+=3602
    budget.reserve('next','s','organize');budget.finish('next',True)
    with pytest.raises(BudgetExceeded,match='already been attempted'):budget.reserve('0','s','organize')


def test_failures_pause_across_restart_and_resume_does_not_replay(store):
    budget = MaintenanceBudget(store)
    for i in range(3):
        budget.reserve(str(i), "s", "organize")
        budget.finish(str(i), False)
    budget = MaintenanceBudget(store)
    assert budget.status()["paused"]
    with pytest.raises(BudgetExceeded, match="paused"):
        budget.reserve("new", "s", "organize")
    assert not budget.resume()["paused"]
    with pytest.raises(BudgetExceeded, match="already been attempted"):
        budget.reserve("0", "s", "organize")


def test_concurrent_duplicate_reserves_only_one_call(store):
    budget = MaintenanceBudget(store)

    def claim(_):
        try:
            budget.reserve("same", "s", "organize")
            return True
        except BudgetExceeded:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(claim, range(100))) == 1


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


def test_denied_budget_does_not_spawn_runner(store, tmp_path):
    marker = tmp_path / "spawned"
    engine = Engine(
        store,
        agent_command=[sys.executable, "-c", f"open({str(marker)!r},'w').close()"],
    )
    engine.budget.reserve("job", "s", "organize")
    with pytest.raises(BudgetExceeded):
        engine.agent(dict(role="organize"), attempt_id="job", root_hash="s")
    assert not marker.exists()


def test_bounded_runner_timeout_and_output_limit():
    with pytest.raises(TimeoutError):
        bounded_run(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            "input",
            timeout=0.2,
            max_output=4096,
        )
    with pytest.raises(ValueError, match="output"):
        bounded_run(
            [sys.executable, "-c", "print('x'*100000)"],
            "input",
            timeout=2,
            max_output=4096,
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


def test_interrupted_attempts_count_toward_pause_across_restarts(store):
    for i in range(3):
        budget = MaintenanceBudget(store)
        budget.reserve('crash-' + str(i), 'session', 'organize')
        restarted = MaintenanceBudget(store)
        restarted.recover_interrupted()
    assert restarted.status()['paused']
    with pytest.raises(BudgetExceeded, match='paused'):
        restarted.reserve('new', 'session', 'organize')
    restarted.resume()
    with pytest.raises(BudgetExceeded, match='already been attempted'):
        restarted.reserve('crash-0', 'session', 'organize')
