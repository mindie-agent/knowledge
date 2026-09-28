"""Stdlib Windows subprocess ownership for shared runtime callers.

The child starts suspended, joins an owned Job, then resumes. Descendants
remain owned after their leader exits. Used by both organizer and Git/gh
execution so normal exit, cancellation and timeout share the same cleanup.
"""
import os
import subprocess

def _own_windows_tree(process):
    """Keep descendant ownership after the process group leader exits.

    A taskkill PID traversal cannot find a tree whose root has already exited.
    A Windows Job retains that ownership, including inherited pipe holders.
    This launcher is stdlib-only because it also runs before runtime setup.
    """
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
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                               ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    limits = Extended()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
    if (not job or not kernel.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            or not kernel.AssignProcessToJobObject(job, int(process._handle))):
        error = ctypes.get_last_error()
        if job:
            kernel.CloseHandle(job)
        raise ctypes.WinError(error)
    process._mindie_windows_job = (kernel, job)

def _resume_windows_process(process):
    """Resume the suspended primary thread through documented Win32 APIs.

    Popen closes the thread handle returned by CreateProcess, so reopen this
    process's sole suspended thread from a Toolhelp snapshot. Its process
    handle stays owned throughout; no shell or system-wide process kill.
    """
    import ctypes
    from ctypes import wintypes

    class ThreadEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ThreadID", wintypes.DWORD),
                    ("th32OwnerProcessID", wintypes.DWORD),
                    ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
    if snapshot == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel.Thread32First(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32OwnerProcessID == process.pid:
                thread = kernel.OpenThread(0x2, False, entry.th32ThreadID)  # SUSPEND_RESUME
                if not thread:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    previous = kernel.ResumeThread(thread)
                    if previous == 0xFFFFFFFF:
                        raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    kernel.CloseHandle(thread)
                if previous == 1:
                    return
                if previous > 1:
                    raise OSError("owned primary thread has an unexpected suspend count")
                # An injected, already-running thread is not the primary
                # thread we created suspended. Continue to the owned one.
            more = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        raise OSError("owned suspended process has no primary thread")
    finally:
        kernel.CloseHandle(snapshot)

def terminate_owned(process):
    owned = getattr(process, "_mindie_windows_job", None)
    if owned is not None:
        process._mindie_windows_job = None
        kernel, job = owned
        try:
            kernel.TerminateJobObject(job, 1)
        finally:
            kernel.CloseHandle(job)
        return
    if process.poll() is None:
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        try:
            subprocess.run(
                [os.path.join(system_root, "System32", "taskkill.exe"),
                 "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()


def spawn_owned(command, **kwargs):
    """Create a process with descendant ownership established before it runs."""
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP | 0x4
    process = subprocess.Popen(command, **kwargs)
    try:
        _own_windows_tree(process)
        _resume_windows_process(process)
    except BaseException:
        terminate_owned(process)
        process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        raise
    return process
