"""Real Windows ownership, including a leader that exits before cleanup."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows console inheritance')
@pytest.mark.parametrize('kind', ['owned', 'service'])
def test_python_venv_helper_does_not_create_a_console(tmp_path, kind):
    from mindie_knowledge.loop.process import spawn_service, terminate_tree
    from mindie_knowledge.windows_process import spawn_owned, terminate_owned
    marker = tmp_path / 'console.json'
    source = ('import ctypes,json,pathlib,sys; '
              'pathlib.Path(sys.argv[1]).write_text(json.dumps({"console":int(ctypes.windll.kernel32.GetConsoleWindow())}))')
    if kind == 'service':
        child = spawn_service([sys.executable, '-c', source, str(marker)])
    else:
        child = spawn_owned([sys.executable, '-c', source, str(marker)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        child.wait(timeout=5)
        assert child.returncode == 0
        assert json.loads(marker.read_text()) == {'console': 0}
    finally:
        if kind == 'owned':
            terminate_owned(child)
        elif child.poll() is None:
            terminate_tree(child)


@pytest.mark.skipif(os.name != "nt", reason="Win32 Job ownership; POSIX groups have separate tests")
@pytest.mark.parametrize("caller", ["organizer", "community"])
@pytest.mark.parametrize("inherited_pipes", [False, True], ids=["normal-exit", "pipe-timeout"])
def test_exited_leader_keeps_descendants_owned(tmp_path, caller, inherited_pipes):
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    child = tmp_path / "owned_child.py"
    child.write_text("import time; time.sleep(30)\n", encoding="utf-8")
    parent = tmp_path / "parent.py"
    pidfile = tmp_path / "child.pid"
    redirect = "" if inherited_pipes else ", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL"
    parent.write_text(
        "import pathlib, subprocess, sys\n"
        f"p=subprocess.Popen([sys.executable, sys.argv[1]]{redirect})\n"
        "pathlib.Path(sys.argv[2]).write_text(str(p.pid))\n", encoding="utf-8")
    runner = tmp_path / "runner.py"
    repository = Path(__file__).resolve().parents[1]
    runner.write_text(
        "import json, sys\n"
        f"sys.path.insert(0, {str(repository)!r})\n"
        "from mindie_knowledge.loop.process import bounded_run\n"
        "from mindie_knowledge.community.common import run_argv\n"
        "timed_out=False\n"
        "try:\n"
        " if sys.argv[1]=='organizer':\n"
        "  bounded_run([sys.executable,*sys.argv[2:]],'',timeout=0.3,max_output=4096)\n"
        " else:\n"
        "  timed_out=run_argv([sys.executable,*sys.argv[2:]],timeout=0.3).timed_out\n"
        "except TimeoutError:\n"
        " timed_out=True\n"
        "print(json.dumps({'timed_out':timed_out}))\n", encoding="utf-8")
    process = subprocess.Popen([sys.executable, str(runner), caller, str(parent), str(child), str(pidfile)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    handle = None
    started = time.monotonic()
    try:
        deadline = started + 3
        while not pidfile.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pidfile.exists(), "real descendant did not start"
        pid = int(pidfile.read_text())
        # Hold the exact process object, so failure cleanup cannot hit a reused PID.
        handle = kernel.OpenProcess(0x100001, False, pid)  # SYNCHRONIZE | TERMINATE
        stdout, stderr = process.communicate(timeout=4)
        assert process.returncode == 0, stderr.decode("utf-8", "replace")
        assert json.loads(stdout)["timed_out"] is inherited_pipes
        if handle:
            assert kernel.WaitForSingleObject(handle, 1000) == 0, "owned descendant survived"
        assert time.monotonic() - started < 3
    finally:
        if handle:
            if kernel.WaitForSingleObject(handle, 0) == 258:
                kernel.TerminateProcess(handle, 1)
            kernel.CloseHandle(handle)
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=3)
