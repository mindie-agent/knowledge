"""Real Windows ownership, including a leader that exits before cleanup."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Windows native process status')
def test_native_resume_reports_invalid_owned_handle():
    from types import SimpleNamespace
    from mindie_knowledge.windows_process import _resume_windows_process
    with pytest.raises(OSError):
        _resume_windows_process(SimpleNamespace(_handle=0))


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
@pytest.mark.parametrize("inherited_pipes", [False, True], ids=["normal-exit", "inherited-pipes"])
def test_exited_leader_keeps_descendants_owned(tmp_path, caller, inherited_pipes):
    from ctypes import wintypes
    from mindie_knowledge.windows_process import spawn_owned, terminate_owned
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    child = tmp_path / "owned_child.py"
    pidfile = tmp_path / "child.pid"
    release = tmp_path / "release"
    finished = tmp_path / "finish"
    result_path = tmp_path / "result.json"
    child.write_text(
        "import os, pathlib, sys, time\n"
        "pid_path=pathlib.Path(sys.argv[1])\n"
        "pending=pid_path.with_suffix('.tmp')\n"
        "pending.write_text(str(os.getpid()))\n"
        "pending.replace(pid_path)\n"
        "time.sleep(120)\n", encoding="utf-8")
    parent = tmp_path / "parent.py"
    redirect = "" if inherited_pipes else ", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL"
    parent.write_text(
        "import pathlib, subprocess, sys, time\n"
        "time.sleep(0.35)\n"  # Longer than the operation deadline by design.
        f"p=subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]]{redirect})\n"
        "while not pathlib.Path(sys.argv[3]).exists(): time.sleep(0.01)\n",
        encoding="utf-8")
    runner = tmp_path / "runner.py"
    repository = Path(__file__).resolve().parents[1]
    runner.write_text(
        "import json, pathlib, sys, time\n"
        f"sys.path.insert(0, {str(repository)!r})\n"
        "from mindie_knowledge.loop.process import bounded_run\n"
        "from mindie_knowledge.community.common import run_argv\n"
        "from mindie_knowledge import windows_process\n"
        "real_spawn=windows_process.spawn_owned\n"
        "prepared={}\n"
        "def ready_spawn(*args, **kwargs):\n"
        " started=time.monotonic()\n"
        " process=real_spawn(*args, **kwargs)\n"
        " deadline=started+20\n"
        " try:\n"
        "  while not pathlib.Path(sys.argv[-1]).exists():\n"
        "   if process.poll() is not None or time.monotonic()>=deadline:\n"
        "    raise RuntimeError('fixture leader exited or readiness watchdog expired before handle release')\n"
        "   time.sleep(0.01)\n"
        "  assert pathlib.Path(sys.argv[-2]).is_file(), 'fixture descendant readiness missing'\n"
        "  assert process.wait(timeout=max(0.01,deadline-time.monotonic()))==0\n"
        "  prepared.update(leader_exit=process.returncode,seconds=time.monotonic()-started)\n"
        "  return process\n"
        " except BaseException:\n"
        "  windows_process.terminate_owned(process)\n"
        "  process.wait(timeout=5)\n"
        "  for stream in (process.stdout,process.stderr):\n"
        "   if stream is not None: stream.close()\n"
        "  raise\n"
        "windows_process.spawn_owned=ready_spawn\n"
        "timed_out=False\n"
        "if sys.argv[1]=='organizer':\n"
        " bounded_run([sys.executable,*sys.argv[2:]],'',timeout=None,max_output=4096)\n"
        "else:\n"
        " timed_out=run_argv([sys.executable,*sys.argv[2:]],timeout=0.3).timed_out\n"
        "result={'timed_out':timed_out,'prepared':prepared}\n"
        "print(json.dumps(result),flush=True)\n"
        f"result_path=pathlib.Path({str(result_path)!r})\n"
        "pending=result_path.with_suffix('.tmp')\n"
        "pending.write_text(json.dumps(result))\n"
        "pending.replace(result_path)\n"
        f"while not pathlib.Path({str(finished)!r}).exists(): time.sleep(0.01)\n",
        encoding="utf-8")
    stdout_path, stderr_path = tmp_path / "runner.stdout", tmp_path / "runner.stderr"
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = spawn_owned(
            [sys.executable, str(runner), caller, str(parent), str(child), str(pidfile), str(release)],
            stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
    handle = None
    primary = None
    deadline = time.monotonic() + 30  # Harness watchdog, independent of the operation under test.
    try:
        while not pidfile.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pidfile.exists(), "real descendant did not start"
        pid = int(pidfile.read_text())
        # Hold the exact process object, so failure cleanup cannot hit a reused PID.
        handle = kernel.OpenProcess(0x100001, False, pid)  # SYNCHRONIZE | TERMINATE
        assert handle, f"cannot retain descendant handle: WinError {ctypes.get_last_error()}"
        assert kernel.WaitForSingleObject(handle, 0) == 258, "descendant exited before the ownership test"
        release.touch()  # Only now may the leader exit and the real operation begin.
        while not result_path.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert result_path.exists(), "ownership runner did not return its result"
        # The organizer completes on the native leader's real exit and then
        # reaps its owned descendants. Community subprocesses drain complete
        # output, so inherited pipes instead reach this explicit test deadline.
        expected_timeout = inherited_pipes and caller == "community"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["prepared"]["leader_exit"] == 0
        assert result["prepared"]["seconds"] >= 0.35
        assert result["timed_out"] is expected_timeout
        assert kernel.WaitForSingleObject(handle, 1000) == 0, "owned descendant survived"
        # Keep the runner alive until after this assertion: runner exit would
        # close its Job handles and could hide missing product cleanup.
        assert process.poll() is None, "runner exited before the ownership assertion"
        finished.touch()
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
        assert process.returncode == 0, "ownership runner failed"
    except BaseException as exc:
        primary = exc
        raise
    finally:
        cleanup_errors = []
        try:
            terminate_owned(process)
            process.wait(timeout=5)
        except Exception as exc:
            cleanup_errors.append(repr(exc))
        if handle:
            if kernel.WaitForSingleObject(handle, 0) == 258:
                if not kernel.TerminateProcess(handle, 1):
                    cleanup_errors.append(f"descendant cleanup WinError {ctypes.get_last_error()}")
                elif kernel.WaitForSingleObject(handle, 5000) != 0:
                    cleanup_errors.append("descendant did not exit during fixture cleanup")
            kernel.CloseHandle(handle)
        if primary is not None or cleanup_errors:
            detail = ("\nrunner stdout:\n" + stdout_path.read_text(encoding="utf-8", errors="replace")[-8000:]
                      + "\nrunner stderr:\n" + stderr_path.read_text(encoding="utf-8", errors="replace")[-8000:]
                      + "\nfixture cleanup: " + repr(cleanup_errors))
            if primary is not None:
                primary.add_note(detail)
            else:
                pytest.fail(detail)
