"""Execution completion and cleanup are separate; every native is synthetic."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from mindie_knowledge.loop.process import bounded_run, MaintenanceCancelled
from mindie_knowledge.loop import process as runner


def test_quiet_and_progressing_owned_processes_have_no_default_execution_deadline():
    for body in ('time.sleep(.3)', '[(print(" ", flush=True), time.sleep(.05)) for _ in range(6)]'):
        result = bounded_run([sys.executable, '-c', 'import time; '+body+'; print("done")'], '', max_output=1024)
        assert result.strip() == 'done'


def test_result_is_saved_before_cleanup_and_cleanup_cannot_erase_it(monkeypatch):
    saved, state = [], {}
    original = runner.terminate_tree
    def failed_cleanup(process):
        assert saved == [dict(status='returned', value='durable')]
        original(process)
        raise OSError('synthetic cleanup failure')
    monkeypatch.setattr(runner, 'terminate_tree', failed_cleanup)
    command = [sys.executable, '-c', 'import json; print(json.dumps({"status":"returned","value":"durable"}),flush=True)']
    raw = bounded_run(command, '', max_output=1024, on_result=lambda text:saved.append(json.loads(text)), cleanup_receipt=state)
    assert json.loads(raw) == saved[0]
    assert state == dict(cleanup_failed=True)


def test_terminal_then_hanging_worker_keeps_result_and_is_reaped():
    saved, cleanup = [], {}
    started = time.monotonic()
    raw = bounded_run([sys.executable, '-c', 'import time; print("{\\"status\\":\\"returned\\"}",flush=True); time.sleep(30)'],
                      '', max_output=1024, on_result=lambda text:saved.append(json.loads(text)), cleanup_receipt=cleanup)
    assert saved == [dict(status='returned')]
    assert json.loads(raw) == saved[0]
    assert cleanup['cleanup_failed'] is True
    assert time.monotonic()-started < 5  # Test watchdog on completed cleanup only.


def test_owner_cancellation_stops_the_same_quiet_operation():
    cancel = threading.Event()
    timer = threading.Timer(.2, cancel.set)
    timer.start()
    try:
        with pytest.raises(MaintenanceCancelled):
            bounded_run([sys.executable, '-c', 'import time; time.sleep(30)'], '', max_output=1024, cancel=cancel)
    finally:
        timer.cancel()


def test_closed_pipes_wait_for_real_success_beyond_old_cleanup_duration(tmp_path):
    done = tmp_path / 'completed'
    command = [sys.executable, '-c', 'import os,time,sys; from pathlib import Path; '
               'os.close(1); os.close(2); time.sleep(2.2); Path(sys.argv[1]).write_text("done")', str(done)]
    assert bounded_run(command, '', max_output=1024) == ''
    assert done.read_text() == 'done'


@pytest.mark.parametrize('stop', ['cancel', 'deadline'])
def test_closed_pipes_keep_owner_cancel_and_explicit_deadline_live(stop, tmp_path):
    ready = tmp_path / 'closed'
    cancel = threading.Event()
    def cancelled_after_eof():
        until = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < until:
            time.sleep(.01)
        cancel.set()
    trigger = threading.Thread(target=cancelled_after_eof) if stop == 'cancel' else None
    if trigger:
        trigger.start()
    try:
        with pytest.raises(MaintenanceCancelled if stop == 'cancel' else TimeoutError):
            bounded_run([sys.executable, '-c', 'import os,time,sys; from pathlib import Path; '
                         'os.close(1); os.close(2); Path(sys.argv[1]).write_text("closed"); time.sleep(30)', str(ready)],
                        '', max_output=1024, cancel=cancel, timeout=None if stop == 'cancel' else .3)
        assert ready.exists()
    finally:
        if trigger:
            trigger.join(timeout=5)
