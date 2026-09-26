import concurrent.futures
import os
import re
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


def test_failures_never_pause_and_attempts_stay_consumed(store):
    budget = MaintenanceBudget(store)
    for i in range(3):
        budget.reserve(str(i), "s", "organize")
        budget.finish(str(i), False)
    budget = MaintenanceBudget(store)
    # No pause latch exists: new work is admitted immediately, and a consumed
    # attempt identity is still never replayed.
    budget.reserve("new", "s", "organize")
    budget.finish("new", True)
    with pytest.raises(BudgetExceeded, match="already been attempted"):
        budget.reserve("0", "s", "organize")
    assert "paused" not in budget.status()


def test_legacy_pause_latch_and_backlog_migrate_on_store_open(store):
    parked = store.add_capture(
        root_session="r", session="s", turn="t", transcript=None,
        summary="legacy parked work",
    )
    with store._write_txn():
        store.db.execute(
            "INSERT OR REPLACE INTO state VALUES('maintenance_paused', 'legacy')"
        )
    store.dormant_capture(parked["id"], reason="maintenance-paused")
    with store._write_txn():
        store.db.execute(
            "INSERT INTO continuations VALUES('cap-2', 0, 'incomplete-tail', 0)"
        )
    root = store.root
    store.close()
    # Reopening the store migrates exactly the paused backlog; other dormant
    # reasons are untouched, and the migration is idempotent.
    migrated = Store(root.parent, root.name)
    try:
        with migrated.lock:
            assert migrated.db.execute(
                "SELECT 1 FROM state WHERE key='maintenance_paused'"
            ).fetchone() is None
            rows = dict(
                migrated.db.execute(
                    "SELECT capture_id, eligible FROM continuations"
                ).fetchall()
            )
        assert rows == {parked["id"]: 1, "cap-2": 0}
        due = migrated.due_capture()
        assert due == parked["id"]
    finally:
        migrated.close()
    again = Store(root.parent, root.name)
    try:
        with again.lock:
            assert again.db.execute(
                "SELECT 1 FROM state WHERE key='maintenance_paused'"
            ).fetchone() is None
    finally:
        again.close()
    store = Store(root.parent, root.name)
    try:
        budget = MaintenanceBudget(store)
        budget.reserve("after-migration", "s", "organize")
        budget.finish("after-migration", True)
    finally:
        store.close()


def test_legacy_paused_capture_completes_through_the_worker(store, tmp_path):
    """F4: a capture parked ONLY by the retired latch is consumed after the
    store-open migration and finishes through the normal worker path."""
    import sys

    from conftest import write_settings

    from mindie_knowledge.loop.activation import Admission
    from mindie_knowledge.loop.engine import Engine

    project = tmp_path / "proj"
    project.mkdir()
    settings_path = tmp_path / "community.json"
    write_settings(settings_path, enabled=True, roots=[project])
    admission = Admission(tmp_path / "admission.sqlite3")
    admission.activate("manual-A", project_root=str(project))
    runner = tmp_path / "runner.py"
    runner.write_text("print('{\"entries\": []}')")
    engine = Engine(
        store,
        agent_command=[sys.executable, str(runner)],
        settings_path=settings_path,
        admission=admission,
    )
    captured = engine.capture(
        session_id="manual-A", turn_id="t1", summary="pending legacy work"
    )
    assert captured["status"] == "queued"
    # Simulate the retired latch exactly: parked ineligible, state row present.
    with store._write_txn():
        store.db.execute(
            "INSERT OR REPLACE INTO state VALUES('maintenance_paused', 'legacy')"
        )
        store.db.execute(
            "UPDATE continuations SET due=0, eligible=1 WHERE capture_id=?",
            (captured["id"],),
        )
    store.dormant_capture(captured["id"], reason="maintenance-paused")
    assert store.due_capture() is None
    root = store.root
    store.close()
    # Upgrade/restart: reopening migrates the backlog; the worker then
    # consumes it through the normal path and the content converges.
    reopened = Store(root.parent, root.name)
    try:
        assert reopened.due_capture() == captured["id"]
        engine = Engine(
            reopened,
            agent_command=[sys.executable, str(runner)],
            settings_path=settings_path,
            admission=admission,
        )
        engine._process(captured["id"])
        row = reopened.capture_row(captured["id"])
        assert row["status"] == "organized", row["detail"]
        assert reopened.due_capture() is None
        with reopened.lock:
            assert reopened.db.execute(
                "SELECT count(*) FROM continuations"
            ).fetchone()[0] == 0
    finally:
        reopened.close()


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


def test_diagnostic_does_not_grant_failed_attempt_a_retry(store, tmp_path):
    from conftest import write_settings

    settings_path = tmp_path / "community.json"
    write_settings(settings_path, enabled=True, roots=[tmp_path])
    marker = tmp_path / "spawns"
    command = [sys.executable, "-c",
               f"import sys; from pathlib import Path; p=Path({str(marker)!r}); "
               "p.write_text(p.read_text()+'x' if p.exists() else 'x'); sys.exit(124)"]
    engine = Engine(store, agent_command=command, settings_path=settings_path)
    with pytest.raises(RuntimeError, match="category=deadline"):
        engine.agent(dict(role="organize"), attempt_id="failed", root_hash="s")
    restarted = Engine(store, agent_command=command, settings_path=settings_path)
    with pytest.raises(BudgetExceeded, match="already been attempted"):
        restarted.agent(dict(role="organize"), attempt_id="failed", root_hash="s")
    assert marker.read_text() == "x"


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


def test_interrupted_attempts_are_consumed_without_pausing(store):
    for i in range(3):
        budget = MaintenanceBudget(store)
        budget.reserve('crash-' + str(i), 'session', 'organize')
        restarted = MaintenanceBudget(store)
        restarted.recover_interrupted()
    # Interrupted attempts stay consumed (no replay) but never pause the domain.
    restarted.reserve('new', 'session', 'organize')
    restarted.finish('new', True)
    with pytest.raises(BudgetExceeded, match='already been attempted'):
        restarted.reserve('crash-0', 'session', 'organize')
    with store.lock:
        rows = store.db.execute(
            "SELECT status FROM maintenance_attempts WHERE id LIKE 'crash-%'"
        ).fetchall()
    assert {row[0] for row in rows} == {"failed"}


def test_failure_categories_are_recorded_without_a_domain_latch(store):
    """Per-item content failures are recorded as ``invalid``, shared/systemic
    ones as ``failed``; both stay consumed and visible, and neither pauses
    unrelated work — recovery is per-capture and bounded."""
    budget = MaintenanceBudget(store)
    for i in range(3):
        budget.reserve(f"bad-{i}", f"s{i}", "organize")
        budget.finish(f"bad-{i}", False, category="invalid_result")
    row = store.db.execute(
        "SELECT status FROM maintenance_attempts WHERE id='bad-0'"
    ).fetchone()
    assert row[0] == "invalid"  # consumed and visible
    for i in range(3):
        budget.reserve(f"cfg-{i}", f"sc{i}", "organize")
        budget.finish(f"cfg-{i}", False, category="configuration")
    row = store.db.execute(
        "SELECT status FROM maintenance_attempts WHERE id='cfg-0'"
    ).fetchone()
    assert row[0] == "failed"
    # Other work is admitted normally after any failure mix.
    budget.reserve("good", "sg", "organize")
    budget.finish("good", True)


def test_runner_exit_category_never_blocks_later_work(store, tmp_path):
    from conftest import write_settings

    settings_path = tmp_path / "community.json"
    write_settings(settings_path, enabled=True, roots=[tmp_path])
    command = [sys.executable, "-c", "import sys; sys.exit(65)"]
    engine = Engine(store, agent_command=command, settings_path=settings_path)
    for i in range(3):
        with pytest.raises(RuntimeError, match="category=invalid_result"):
            engine.agent(dict(role="organize"), attempt_id=f"bad-{i}", root_hash="s")
    engine.agent_command = [sys.executable, "-c", "import sys; sys.exit(78)"]
    for i in range(3):
        with pytest.raises(RuntimeError, match="category=configuration"):
            engine.agent(dict(role="organize"), attempt_id=f"cfg-{i}", root_hash="sc")
    # No pause state: the next attempt is admitted immediately.
    engine.agent_command = [sys.executable, "-c", "import json; print(json.dumps({'entries': []}))"]
    result = engine.agent(dict(role="organize", entries=[]), attempt_id="after", root_hash="sc")
    assert result == {"entries": []}
