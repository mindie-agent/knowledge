"""Bound owned process trees and pipe memory without retaining retry work.

Pipe draining uses daemon reader threads and a queue, which works with
subprocess pipes on both POSIX and Windows (``selectors`` cannot select
Windows pipes). Process-tree cleanup: POSIX uses a new session and
``killpg``; Windows assigns the spawned process to a Job Object so
descendants stay owned, then ``TerminateJobObject`` (``taskkill /T`` only
if job assignment fails).
"""

import os
import queue
import signal
import subprocess
import tempfile
import threading
import time


class MaintenanceCancelled(RuntimeError):
    """The owning service is stopping; interrupted work is not replayed."""


_JOB_KILL_ON_CLOSE = 0x2000
_JOBOBJECT_EXTENDED_LIMIT_INFORMATION = 9


def _windows_job_api():
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", Basic),
            ("IoInfo", IO),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel, ctypes, Extended


def _attach_windows_job(process):
    """Own ``process`` and future descendants. None if assignment is refused."""
    kernel, ctypes, Extended = _windows_job_api()
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        return None
    limits = Extended()
    limits.BasicLimitInformation.LimitFlags = _JOB_KILL_ON_CLOSE
    handle = int(process._handle)
    if not kernel.SetInformationJobObject(
        job,
        _JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ) or not kernel.AssignProcessToJobObject(job, handle):
        kernel.CloseHandle(job)
        return None
    return job


def _spawn(command, stdin):
    if os.name == "nt":
        process = subprocess.Popen(
            command,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            env={**os.environ, "MINDIE_MAINTENANCE_GROUP": "1"},
        )
        process._mindie_job = _attach_windows_job(process)
        return process
    return subprocess.Popen(
        command,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env={**os.environ, "MINDIE_MAINTENANCE_GROUP": "1"},
    )


def terminate_tree(process):
    """Terminate the whole owned tree rooted at ``process``; never raises."""
    if os.name == "nt":
        job = getattr(process, "_mindie_job", None)
        if job:
            process._mindie_job = None
            kernel, _, _ = _windows_job_api()
            try:
                kernel.TerminateJobObject(job, 1)
            except OSError:
                pass
            try:
                kernel.CloseHandle(job)
            except OSError:
                pass
            return
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _reader(stream, tag, chunks):
    try:
        while True:
            chunk = os.read(stream.fileno(), 4096)
            if not chunk:
                break
            chunks.put((tag, chunk))
    except OSError:
        pass
    finally:
        chunks.put((tag, None))


def bounded_run(command, payload, *, timeout, max_output, cancel=None):
    with tempfile.TemporaryFile() as input_file:
        input_file.write(payload.encode())
        input_file.seek(0)
        process = _spawn(command, input_file)
        chunks = queue.Queue()
        readers = [
            threading.Thread(
                target=_reader, args=(stream, tag, chunks), daemon=True
            )
            for stream, tag in ((process.stdout, "out"), (process.stderr, "err"))
        ]
        for reader in readers:
            reader.start()
        output = bytearray()
        total = 0
        open_streams = len(readers)
        deadline = time.monotonic() + timeout
        try:
            while open_streams:
                if cancel is not None and cancel.is_set():
                    raise MaintenanceCancelled("maintenance cancelled by shutdown")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("maintenance deadline exceeded")
                try:
                    tag, chunk = chunks.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    continue
                if chunk is None:
                    open_streams -= 1
                    continue
                total += len(chunk)
                if total > max_output:
                    raise ValueError("maintenance output exceeds limit")
                if tag == "out":
                    output.extend(chunk)
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if code:
                raise RuntimeError(f"maintenance agent exited {code}")
            return output.decode()
        finally:
            terminate_tree(process)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
            for reader in readers:
                reader.join(timeout=1)
            process.stdout.close()
            process.stderr.close()
