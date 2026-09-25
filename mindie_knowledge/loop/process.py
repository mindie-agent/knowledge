"""Bound owned process trees and pipe memory without retaining retry work.

Pipe draining uses daemon reader threads and a bounded queue, which works
with subprocess pipes on both POSIX and Windows (``selectors`` cannot select
Windows pipes). The queue is deliberately bounded so a runaway child cannot
grow memory past the output cap before the consumer notices; readers block
briefly and drop nothing while the consumer is alive.

Process-tree cleanup: POSIX uses a new session and ``killpg``. Windows
assigns the spawned process to a Job Object so descendants stay owned, then
``TerminateJobObject`` (``taskkill /T`` only if job assignment fails).
Honest limitation: the child starts runnable BEFORE job assignment, so a
descendant spawned in that race window can escape ownership, and the
taskkill fallback can lose orphans whose parent already exited. This module
does NOT prove reliable Windows tree ownership; that needs an atomic
create-suspended/assign/resume sequence or an equivalent bounded supervisor
and real Windows acceptance, which remains open (root note 9).
"""

import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time

from .dfx import failure


class MaintenanceCancelled(RuntimeError):
    """The owning service is stopping; interrupted work is not replayed."""


# Trusted adapters report these categories through their exit status. Never
# parse provider stderr: it can contain secrets, task material or reasoning.
# An unclassified/older adapter exit remains unknown, not a guessed timeout.
AGENT_ERROR_EXIT_CODES = {
    "configuration": 78,
    "deadline": 124,
    "native": 70,
    "invalid_result": 65,
    "output_limit": 75,
}


def _failure_detail(category, started):
    return f"category={category}; elapsed={time.monotonic() - started:.3f}s"


def annotated_error(exc, category, started, exit_code=None, stage="run"):
    """Attach diagnostics and return the same exception object.

    The trusted category also travels on the exception object so the budget
    layer can tell a per-item content failure (invalid result, output limit)
    apart from a shared configuration/runtime failure without parsing text.
    """
    exc.mindie_category = category
    failure(
        "organizer.process",
        stage=stage,
        category=category,
        exception=exc,
        elapsed_ms=(time.monotonic() - started) * 1000,
        exit_code=exit_code,
        reportable=category in {"invalid_result", "output_limit", "cleanup"},
    )
    return exc


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


def _reader(stream, tag, chunks, cancel):
    try:
        while True:
            chunk = os.read(stream.fileno(), 4096)
            if not chunk:
                break
            # Bounded hand-off: if the consumer stopped (cancellation), the
            # child is being killed; blocked readers exit instead of growing
            # memory without limit.
            while True:
                try:
                    chunks.put((tag, chunk), timeout=0.1)
                    break
                except queue.Full:
                    if cancel is not None and cancel.is_set():
                        return
    except OSError:
        pass
    finally:
        try:
            chunks.put((tag, None), timeout=0.5)
        except queue.Full:
            pass


def bounded_run(command, payload, *, timeout, max_output, cancel=None):
    started = time.monotonic()
    with tempfile.TemporaryFile() as input_file:
        input_file.write(payload.encode())
        input_file.seek(0)
        process = _spawn(command, input_file)
        chunks = queue.Queue(maxsize=max(2, max_output // 4096 + 1))
        readers_stop = threading.Event()
        readers = [
            threading.Thread(
                target=_reader, args=(stream, tag, chunks, readers_stop), daemon=True
            )
            for stream, tag in ((process.stdout, "out"), (process.stderr, "err"))
        ]
        for reader in readers:
            reader.start()
        output = bytearray()
        total = 0
        open_streams = len(readers)
        deadline = started + timeout
        try:
            while open_streams:
                if cancel is not None and cancel.is_set():
                    raise MaintenanceCancelled("maintenance cancelled by shutdown")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise annotated_error(
                        TimeoutError(
                            "maintenance deadline exceeded; "
                            + _failure_detail("deadline", started)
                        ),
                        "deadline",
                        started,
                    )
                try:
                    tag, chunk = chunks.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    continue
                if chunk is None:
                    open_streams -= 1
                    continue
                total += len(chunk)
                if total > max_output:
                    raise annotated_error(
                        ValueError(
                            "maintenance output exceeds limit; "
                            + _failure_detail("output_limit", started)
                        ),
                        "output_limit",
                        started,
                    )
                if tag == "out":
                    output.extend(chunk)
            try:
                code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise annotated_error(
                    TimeoutError(
                        "maintenance deadline exceeded; "
                        + _failure_detail("deadline", started)
                    ),
                    "deadline",
                    started,
                ) from None
            if code:
                category = next(
                    (name for name, value in AGENT_ERROR_EXIT_CODES.items()
                     if value == code), "unknown"
                )
                raise annotated_error(
                    RuntimeError(
                        f"maintenance agent exited {code}; "
                        + _failure_detail(category, started)
                    ),
                    category,
                    started,
                    exit_code=code,
                )
            try:
                return output.decode()
            except UnicodeDecodeError as exc:
                raise annotated_error(exc, "invalid_result", started, stage="decode")
        finally:
            original_error = sys.exc_info()[1]
            try:
                readers_stop.set()
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
            except Exception as cleanup_error:
                failure(
                    "organizer.process",
                    stage="cleanup",
                    category="cleanup",
                    exception=cleanup_error,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
                if original_error is None:
                    raise
