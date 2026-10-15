"""Owned POSIX scratch can close once the group has no live writer."""
import errno
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mindie_knowledge.loop import process


pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" and not sys.platform.startswith("linux"),
    reason="supported POSIX process-group observation",
)


@pytest.mark.parametrize("stdout,expected", [
    ("10 10 Z\n11 10 Z+\n", False),
    ("10 10 Z\n11 10 S+\n", True),
    ("10 10 ?\n", True),
    ("10 10 Zunknown\n", True),
    ("10 10 Z\n11 11 S\n", False),
])
def test_metadata_requires_no_live_writer(monkeypatch, stdout, expected):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout, ""))
    monkeypatch.setattr(process.subprocess, "run", run)
    assert process._posix_group_has_writers(10, timeout=.5) is expected
    assert run.call_args.args[0] == [
        "/bin/ps", "-o", "pid=,pgid=,stat=", "-g", "10",
    ]


@pytest.mark.parametrize("code,stdout,stderr", [
    (2, "", ""), (1, "10 10 Z\n", ""), (0, "", "query denied"),
    (0, "10 invalid Z\n", ""), (0, "unknown output\n", ""),
])
def test_failed_or_malformed_inspection_is_not_absence(monkeypatch, code, stdout, stderr):
    monkeypatch.setattr(process.subprocess, "run", Mock(
        return_value=subprocess.CompletedProcess([], code, stdout, stderr),
    ))
    with pytest.raises(RuntimeError):
        process._posix_group_has_writers(10, timeout=.5)


def test_supported_ps_empty_selection_is_absence(monkeypatch):
    monkeypatch.setattr(process.subprocess, "run", Mock(
        return_value=subprocess.CompletedProcess([], 1, "", ""),
    ))
    assert not process._posix_group_has_writers(10, timeout=.5)


def test_metadata_query_replaces_bogus_inherited_terminal_dimensions(monkeypatch):
    monkeypatch.setenv("COLUMNS", "131072")
    monkeypatch.setenv("LINES", "1")
    run = Mock(return_value=subprocess.CompletedProcess([], 1, "", ""))
    monkeypatch.setattr(process.subprocess, "run", run)
    assert not process._posix_group_has_writers(10, timeout=.5)
    assert run.call_args.kwargs["env"]["COLUMNS"] == "80"
    assert run.call_args.kwargs["env"]["LINES"] == "24"
    assert os.environ["COLUMNS"] == "131072" and os.environ["LINES"] == "1"


def test_permission_error_needs_writer_proof_and_keeps_primary_error(monkeypatch):
    denied = PermissionError(errno.EPERM, "synthetic signal denied")
    def killpg(_pgid, sig):
        if sig == 0:
            raise denied
    monkeypatch.setattr(process.os, "killpg", killpg)
    child = SimpleNamespace(pid=10, wait=Mock())
    monkeypatch.setattr(process, "_posix_group_has_writers", Mock(return_value=False))
    process.terminate_tree(child, wait=True)
    broken = RuntimeError("synthetic query failure")
    monkeypatch.setattr(process, "_posix_group_has_writers", Mock(side_effect=broken))
    with pytest.raises(PermissionError) as raised:
        process.terminate_tree(child, wait=True)
    assert raised.value is denied
    assert raised.value.__cause__ is broken


def test_live_writer_is_not_authorized_by_permission_error(monkeypatch):
    denied = PermissionError(errno.EPERM, "synthetic signal denied")
    monkeypatch.setattr(process.os, "killpg", Mock(side_effect=denied))
    monkeypatch.setattr(process, "_posix_group_has_writers", Mock(return_value=True))
    monkeypatch.setattr(process.time, "monotonic", Mock(side_effect=[0, .1, 3]))
    monkeypatch.setattr(process.time, "sleep", Mock())
    with pytest.raises(PermissionError) as raised:
        process.terminate_tree(SimpleNamespace(pid=10, wait=Mock()), wait=True)
    assert raised.value is denied


def test_real_group_transitions_from_writer_to_zombie_to_absent(tmp_path):
    ready = tmp_path / "ready"
    finish = tmp_path / "finish"
    reaped = tmp_path / "reaped"
    # The keeper stays outside the observed group so its child can remain
    # deterministically unreaped without relying on init's scheduling.
    command = (
        "import json,os,time; from pathlib import Path\n"
        "pid=os.fork()\n"
        "if pid==0:\n"
        " os.setsid()\n"
        f" path=Path({str(ready)!r})\n"
        " path.with_suffix('.tmp').write_text(str(os.getpid()))\n"
        " path.with_suffix('.tmp').replace(path)\n"
        f" while not Path({str(finish)!r}).exists(): time.sleep(.01)\n"
        " os._exit(0)\n"
        f"while not Path({str(reaped)!r}).exists(): time.sleep(.01)\n"
        "os.waitpid(pid,0)\n"
    )
    keeper = subprocess.Popen([sys.executable, "-c", command])
    pgid = None
    try:
        until = time.monotonic() + 5
        while not ready.exists():
            assert keeper.poll() is None and time.monotonic() < until
            time.sleep(.01)
        pgid = int(ready.read_text())
        assert process._posix_group_has_writers(pgid, timeout=1)
        os.killpg(pgid, 0)
        finish.touch()
        until = time.monotonic() + 5
        while process._posix_group_has_writers(pgid, timeout=1):
            assert time.monotonic() < until
            time.sleep(.01)
        if sys.platform == "darwin":
            with pytest.raises(PermissionError):
                os.killpg(pgid, 0)
        else:
            os.killpg(pgid, 0)
        process.terminate_tree(SimpleNamespace(pid=pgid, wait=Mock()), wait=True)
        reaped.touch()
        assert keeper.wait(timeout=5) == 0
        assert not process._posix_group_has_writers(pgid, timeout=1)
        with pytest.raises(ProcessLookupError):
            os.killpg(pgid, 0)
    finally:
        original_error = sys.exc_info()[1]
        finish.touch()
        reaped.touch()
        try:
            keeper.wait(timeout=5)
        except subprocess.TimeoutExpired:
            keeper.kill()
            keeper.wait()
            if original_error is None:
                raise
            original_error.add_note("synthetic keeper cleanup required termination")
