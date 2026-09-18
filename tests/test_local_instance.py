"""Ownership and lock behaviour for the local OpenViking instance."""

from __future__ import annotations

import os
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from mindie_knowledge.local.instance import (
    InstanceLock,
    LocalInstance,
    owned_process,
    pid_alive,
    stop_owned_pid,
)
from mindie_knowledge.local.shared import current_shared, shared_search_uri


class ProcessOwnership(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "native Windows inherited-pipe startup")
    def test_daemon_python_starts_while_mcp_parent_is_reading_stdin(self) -> None:
        # Exercise Python's real startup, not only a mocked Popen flag. Without
        # DEVNULL the child blocks until the parent's private stdin reaches EOF.
        parent_code = r'''
import json, subprocess, sys, threading, time
from mindie_knowledge.local.instance import _popen_kwargs
reader = threading.Thread(target=lambda: sys.stdin.buffer.read(1))
reader.start()
time.sleep(.1)
kwargs = _popen_kwargs()
child = subprocess.Popen([sys.executable, '-B', '-c', 'print("started")'],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
try:
    out, err = child.communicate(timeout=3)
    early = True
except subprocess.TimeoutExpired:
    early = False
print(json.dumps({'early': early, 'hidden': bool(kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW)}), flush=True)
if not early:
    out, err = child.communicate(timeout=5)
reader.join()
print(json.dumps({'out': out.decode().strip(), 'err': err.decode(), 'exit': child.returncode}), flush=True)
'''
        proc = subprocess.Popen([sys.executable, "-B", "-c", parent_code],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            first = json.loads(proc.stdout.readline())
        finally:
            proc.stdin.close()
        rest = proc.stdout.read()
        errors = proc.stderr.read()
        self.assertEqual(proc.wait(timeout=8), 0, errors)
        second = json.loads(rest)
        self.assertTrue(first["early"], "child startup waited for MCP stdin EOF")
        self.assertTrue(first["hidden"])
        self.assertEqual(second, {"out": "started", "err": "", "exit": 0})
        self.assertEqual(errors, "")

    @unittest.skipIf(os.name == "nt", "POSIX process command width")
    def test_marker_after_long_arguments_survives_narrow_terminal(self) -> None:
        marker = "mindie-knowledge-long-command-marker"
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", "x" * 512, marker],
        )
        try:
            with mock.patch.dict(os.environ, {"COLUMNS": "40"}):
                self.assertTrue(owned_process(proc.pid, marker))
                self.assertFalse(owned_process(proc.pid, "unrelated-marker"))
                self.assertTrue(stop_owned_pid(proc.pid, marker))
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)

    def test_owned_process_requires_command_marker(self) -> None:
        marker = "mindie-knowledge-ownership-marker"
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", marker],
        )
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not pid_alive(proc.pid):
            time.sleep(0.05)
        self.assertTrue(pid_alive(proc.pid))
        # A live PID does not mean the Windows CIM provider is warm. Wait for
        # actual command-line evidence, keeping production's fail-closed check
        # unchanged. Three bounded reads allow at most about 15 seconds.
        observed = False
        for _ in range(3):
            observed = owned_process(proc.pid, marker)
            if observed:
                break
            time.sleep(0.05)
        self.assertTrue(observed, "process command line did not become observable")
        self.assertFalse(owned_process(proc.pid, "not-this-instance"))

    def test_stop_owned_leaves_unrelated_pid_alone(self) -> None:
        marker = "mindie-knowledge-do-not-kill"
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", marker],
        )
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not pid_alive(proc.pid):
            time.sleep(0.05)
        self.assertFalse(stop_owned_pid(proc.pid, "other-workspace-ov.conf"))
        self.assertTrue(pid_alive(proc.pid))
        self.assertTrue(stop_owned_pid(proc.pid, marker))
        self.assertFalse(pid_alive(proc.pid))

    def test_stale_pid_record_does_not_kill_reused_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inst = LocalInstance(Path(tmp))
            inst.pid_path.parent.mkdir(parents=True, exist_ok=True)
            inst.pid_path.write_text(
                '{"openviking_pid": 1, "embedding_pid": 1, "openviking_port": 1, "embedding_port": 1}\n',
                encoding="utf-8",
            )
            inst.stop()
            self.assertFalse(inst.pid_path.exists())
            if os.name != "nt":
                self.assertTrue(pid_alive(1))


class InstanceLockTests(unittest.TestCase):
    def test_lock_is_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "instance.lock"
            with InstanceLock(path):
                self.assertTrue(path.exists())


class SharedJoinPoint(unittest.TestCase):
    def test_missing_current_shared_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(current_shared(Path(tmp)))
            self.assertEqual(
                shared_search_uri(Path(tmp)),
                "viking://resources/shared/bootstrap",
            )

    def test_current_json_selects_active_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "shared").mkdir()
            (root / "shared" / "current.json").write_text(
                '{"source_git_sha": "abc", "root_uri": "viking://resources/shared/abc"}\n',
                encoding="utf-8",
            )
            current = current_shared(root)
            self.assertEqual(current["root_uri"], "viking://resources/shared/abc")
            self.assertEqual(shared_search_uri(root), "viking://resources/shared/abc")
