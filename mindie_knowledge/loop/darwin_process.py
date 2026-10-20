"""[Role] Observe owned macOS process groups through unprivileged libproc calls.

[Invariants] Only an absent process or an explicit zombie cannot write. A
denied or incomplete observation is an error, never proof of completed cleanup.
[Timeouts & Locks] Native metadata reads have no wait; buffer growth and PID
iteration share the caller's remaining cleanup deadline. No locks.
[Side-Effect Policy] Read PID, process-group and state metadata only. Never
launch setuid ps, read process arguments/environment, or change sandbox policy.
"""
from __future__ import annotations

import ctypes
import errno
from functools import lru_cache
import os
import time


# SDK ABI: <sys/proc_info.h> proc_bsdshortinfo, PROC_PIDT_SHORTBSDINFO;
# <sys/proc.h> SZOMB. libproc's list wrapper returns PID count, not byte count.
class _BsdShortInfo(ctypes.Structure):
    _fields_ = [
        ("pid", ctypes.c_uint32), ("ppid", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32), ("status", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16), ("flags", ctypes.c_uint32),
        ("uid", ctypes.c_uint32), ("gid", ctypes.c_uint32),
        ("ruid", ctypes.c_uint32), ("rgid", ctypes.c_uint32),
        ("svuid", ctypes.c_uint32), ("svgid", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
    ]


@lru_cache(maxsize=1)
def _libproc():
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_listpgrppids.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
    library.proc_listpgrppids.restype = ctypes.c_int
    library.proc_pidinfo.argtypes = (
        ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int,
    )
    library.proc_pidinfo.restype = ctypes.c_int
    return library


def group_has_writers(pgid, *, timeout):
    """Use same-sandbox metadata access without invoking macOS's setuid ps."""
    deadline = time.monotonic() + timeout
    library, capacity = _libproc(), 16
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("owned process group inspection exceeded cleanup budget")
        pids = (ctypes.c_int * capacity)()
        ctypes.set_errno(0)
        count = library.proc_listpgrppids(pgid, pids, ctypes.sizeof(pids))
        error = ctypes.get_errno()
        if count <= 0 and error:
            raise OSError(error, os.strerror(error))
        if not 0 <= count <= capacity:
            raise RuntimeError("owned process group enumeration was incomplete")
        if count < capacity:
            break
        # A full buffer cannot prove that all surviving writers were listed.
        capacity *= 2
    for pid in pids[:count]:
        if time.monotonic() >= deadline:
            raise TimeoutError("owned process group inspection exceeded cleanup budget")
        info = _BsdShortInfo()
        ctypes.set_errno(0)
        # arg=1 requests zombie lookup too. ESRCH means it disappeared between
        # enumeration and inspection; permission failures remain visible.
        size = library.proc_pidinfo(pid, 13, 1, ctypes.byref(info), ctypes.sizeof(info))
        error = ctypes.get_errno()
        if size == 0 and error == errno.ESRCH:
            continue
        if size != ctypes.sizeof(info):
            if error:
                raise OSError(error, os.strerror(error))
            raise RuntimeError("owned process state observation was incomplete")
        if info.pgid == pgid and info.status != 5:
            return True
    return False
