"""Real owner death, including a worker that exited before its core."""
import errno
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from mindie_knowledge.loop import posix_owner, process


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX ownership")


def _wait(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError("synthetic process condition did not become true")


@pytest.mark.parametrize("worker_exits", [False, True])
def test_core_sigkill_stops_writer_after_worker_lifetime(tmp_path, worker_exits):
    writer = (
        "import os,time;from pathlib import Path;"
        f"p=Path({str(tmp_path)!r});"
        "(p/'writer').write_text(str(os.getpid()));i=0\n"
        "while True:\n"
        " (p/'heartbeat').write_text(str(i));i+=1;time.sleep(.02)\n"
    )
    worker = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{writer!r}]);"
        + ("" if worker_exits else "time.sleep(30)")
    )
    core = (
        "import json,os,subprocess,sys,time;from pathlib import Path;"
        "from mindie_knowledge.loop import process;"
        f"p=Path({str(tmp_path)!r});"
        f"c=process._spawn([sys.executable,'-c',{worker!r}],subprocess.DEVNULL);"
        "(p/'attempt').write_text(str(c.pid));"
        + ("c.wait(timeout=5);" if worker_exits else "")
        + "(p/'ready').write_text('ready');time.sleep(30)"
    )
    owner = subprocess.Popen([sys.executable, "-c", core], stderr=subprocess.PIPE)
    group = None
    try:
        _wait(lambda: (tmp_path / "ready").exists() and (tmp_path / "heartbeat").exists())
        group = int((tmp_path / "attempt").read_text())
        if worker_exits:
            assert subprocess.run(
                ["/bin/ps", "-p", str(group), "-o", "pid="],
                capture_output=True, timeout=3,
            ).returncode == 1
        owner.kill()
        assert owner.wait(timeout=3) == -signal.SIGKILL
        _wait(lambda: not process._posix_group_has_writers(group, timeout=1))
        before = (tmp_path / "heartbeat").read_bytes()
        time.sleep(.08)
        assert (tmp_path / "heartbeat").read_bytes() == before
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=3)
        if group is None and (tmp_path / "attempt").exists():
            group = int((tmp_path / "attempt").read_text())
        if group is not None:
            try:
                os.killpg(group, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        owner.stderr.close()


def test_exec_preserves_parent_group_and_watcher_does_not_hold_stdio():
    child = posix_owner.spawn(
        [sys.executable, "-c",
         "import json,os;print(json.dumps([os.getpid(),os.getppid(),os.getpgrp()]))"],
        stdin=subprocess.DEVNULL, env=os.environ.copy(),
    )
    try:
        stdout, stderr = child.communicate(timeout=3)
        assert json.loads(stdout) == [child.pid, os.getpid(), child.pid]
        assert not stderr
        assert child.returncode == 0
    finally:
        process.terminate_tree(child, wait=True)
    assert child._mindie_owner_fd is None


def test_failed_exec_is_proven_not_started_and_group_is_closed():
    with pytest.raises(posix_owner.LaunchError) as raised:
        posix_owner.spawn(
            ["missing-mindie-owner-fixture-command"],
            stdin=subprocess.DEVNULL, env=os.environ.copy(),
        )
    assert raised.value.stage == "exec"
    assert raised.value.mindie_execution == "not_started"
    assert not raised.value.cleanup_failed


@pytest.mark.parametrize("failure_stage", ["descriptors", "fork", "watcher", "exec"])
def test_pre_exec_failure_never_continues_without_owner(monkeypatch, failure_stage):
    writes, execs = [], []

    def fail(*_):
        raise OSError(errno.EACCES, "synthetic private details")

    def failed_exec(*_):
        execs.append(True)
        fail()

    # Restore OS calls before pytest resumes its own descriptor capture.
    with monkeypatch.context() as patch:
        patch.setattr(os, "getppid", lambda: 10)
        patch.setattr(os, "getpid", lambda: 20)
        patch.setattr(os, "getpgrp", lambda: 20)
        patch.setattr(os, "fstat", lambda _: SimpleNamespace(st_mode=stat.S_IFIFO))
        patch.setattr(os, "set_inheritable", lambda *_: None)
        patch.setattr(os, "pipe", lambda: (50, 51))
        patch.setattr(os, "close", lambda _: None)
        patch.setattr(os, "fork", lambda: 30)
        patch.setattr(os, "read", lambda *_: b"R")
        patch.setattr(os, "write", lambda fd, data: writes.append((fd, data)))
        patch.setattr(os, "execvpe", failed_exec)
        if failure_stage == "descriptors":
            patch.setattr(os, "set_inheritable", fail)
        elif failure_stage == "fork":
            patch.setattr(os, "fork", fail)
        elif failure_stage == "watcher":
            patch.setattr(os, "read", lambda *_: b"")
        assert posix_owner._exec(10, 40, 41, ["fixture"]) == 78
    assert bool(execs) == (failure_stage == "exec")
    value = json.loads(writes[-1][1])
    assert value["stage"] == failure_stage
    assert set(value) == {"stage", "errno"}
    assert "private" not in writes[-1][1].decode()


def test_lost_or_malformed_start_receipt_is_unknown(monkeypatch):
    for data in (b'{"stage":"exec","errno":"bad"}', b"not-json", b"x" * 257):
        read, write = os.pipe()
        try:
            os.write(write, data)
            os.close(write)
            with pytest.raises(posix_owner.LaunchError) as raised:
                posix_owner._exec_ack(read)
            assert raised.value.mindie_execution == "unknown"
        finally:
            os.close(read)
    read, write = os.pipe()
    try:
        monkeypatch.setattr(posix_owner, "_START_WAIT", 0)
        with pytest.raises(posix_owner.LaunchError) as raised:
            posix_owner._exec_ack(read)
        assert raised.value.mindie_execution == "unknown"
    finally:
        os.close(read)
        os.close(write)
