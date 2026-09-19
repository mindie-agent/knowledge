import concurrent.futures
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
        budget.reserve("extra", "s", "judge")
    for i in range(6, 20):
        budget.reserve(str(i), str(i), "judge")
        budget.finish(str(i), True)
    with pytest.raises(BudgetExceeded, match="hourly"):
        budget.reserve("extra", "another", "judge")


def test_failures_pause_across_restart_and_resume_does_not_replay(store):
    budget = MaintenanceBudget(store)
    for i in range(3):
        budget.reserve(str(i), "s", "judge")
        budget.finish(str(i), False)
    budget = MaintenanceBudget(store)
    assert budget.status()["paused"]
    with pytest.raises(BudgetExceeded, match="paused"):
        budget.reserve("new", "s", "judge")
    assert not budget.resume()["paused"]
    with pytest.raises(BudgetExceeded, match="already been attempted"):
        budget.reserve("0", "s", "judge")


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


def test_110_hook_repeats_only_queue_one_model_job(store):
    engine = Engine(store, agent_command=[sys.executable, "-c", "pass"])
    store.query("example", session_id="s")
    for _ in range(110):
        engine.capture("s", "t", "same final summary")
    assert engine.queue.qsize() == 1


def test_replayed_hooks_execute_runner_once_even_after_restart(store, tmp_path):
    marker = tmp_path / "calls"
    code = f"from pathlib import Path; p=Path({str(marker)!r}); p.open('a').write('call\\n'); print('{{\"entries\":[]}}')"
    engine = Engine(store, agent_command=[sys.executable, "-c", code])
    store.query("example", session_id="s")
    engine.start()
    try:
        for _ in range(110):
            engine.capture("s", "t", "same summary")
        deadline = time.monotonic() + 3
        while engine.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.02)
        assert engine.queue.unfinished_tasks == 0
        assert marker.read_text().splitlines() == ["call"]
    finally:
        engine.stop.set()
        engine.thread.join(2)
    restarted = Engine(store, agent_command=engine.agent_command)
    assert restarted.capture("s", "t", "changed summary")["duplicate"]
    assert restarted.queue.qsize() == 0


def test_denied_budget_does_not_spawn_runner(store, tmp_path):
    marker = tmp_path / "spawned"
    engine = Engine(
        store,
        agent_command=[sys.executable, "-c", f"open({str(marker)!r},'w').close()"],
    )
    engine.budget.reserve("job", "s", "organize")
    with pytest.raises(BudgetExceeded):
        engine.agent("organize", {}, attempt_id="job", session="s")
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


def test_bounded_run_cancellation_kills_the_whole_process_tree(tmp_path):
    """A real sleeping parent+child are both gone after cancellation."""
    import os
    import threading
    import time

    from mindie_knowledge.loop import process as process_module
    from mindie_knowledge.loop.process import MaintenanceCancelled

    spawned = []
    real_spawn = process_module._spawn

    def recording_spawn(command, stdin):
        proc = real_spawn(command, stdin)
        spawned.append(proc)
        return proc

    cancel = threading.Event()
    original = process_module._spawn
    process_module._spawn = recording_spawn
    try:
        outcome = []

        def work():
            try:
                process_module.bounded_run(
                    ["sh", "-c", "sleep 30 & exec sleep 30"],
                    "{}",
                    timeout=60,
                    max_output=1024,
                    cancel=cancel,
                )
            except MaintenanceCancelled:
                outcome.append("cancelled")

        thread = threading.Thread(target=work)
        thread.start()
        time.sleep(0.7)
        cancel.set()
        thread.join(timeout=10)
        assert not thread.is_alive(), "cancellation did not interrupt bounded_run"
        assert outcome == ["cancelled"]
        proc = spawned[0]
        with pytest.raises(ProcessLookupError):
            os.killpg(proc.pid, 0)  # the whole group, including the child, is gone
    finally:
        process_module._spawn = original
