"""Single-active-starter lock anchored on one persistent file.

The lock itself is OS-managed (``fcntl.flock`` on POSIX, ``LockFileEx``
on Windows) and the OS releases it when the holder exits or crashes, so a
live holder stays exclusive for *any* duration and there is no stale
metadata to heuristically reclaim. The file is deliberately never unlinked:
an old owner's ``release`` only closes its own descriptor and cannot delete
a later acquisition. The JSON payload is diagnostic only — liveness is never
inferred from it, so a half-written payload during another process's
create→write window just yields a conservative ``busy``.
"""

from __future__ import annotations

import json
import errno
import os
import time
from pathlib import Path
from ..filesystem import filesystem_path


class StartInProgress(RuntimeError):
    pass


def lock_file(fd: int, *, exclusive: bool, blocking: bool = False) -> None:
    """Lock an existing descriptor; its owner controls lifetime and lock path.

    Windows locks byte [0, 1), matching the existing bootstrap msvcrt lock.
    POSIX locks the whole file. Only contention becomes BlockingIOError; other
    OS failures remain errors, never a false busy result. No descriptor or
    process registry is retained here.
    """
    if os.name == "nt":
        import ctypes
        import msvcrt
        import pywintypes
        import win32file

        flags = (2 if exclusive else 0) | (0 if blocking else 1)
        try:
            win32file.LockFileEx(
                msvcrt.get_osfhandle(fd), flags, 1, 0, pywintypes.OVERLAPPED()
            )
        except pywintypes.error as exc:
            if exc.winerror == 33:  # ERROR_LOCK_VIOLATION
                raise BlockingIOError(errno.EAGAIN, "file lock is held") from None
            raise ctypes.WinError(exc.winerror) from None
    else:
        import fcntl

        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                        | (0 if blocking else fcntl.LOCK_NB))
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise BlockingIOError(errno.EAGAIN, "file lock is held") from None
            raise


def try_lock_file(fd: int, *, exclusive: bool) -> bool:
    """Try one acquisition without waiting; False means OS lock contention."""
    try:
        lock_file(fd, exclusive=exclusive)
    except BlockingIOError:
        return False
    return True


def unlock_file(fd: int) -> None:
    """Release the descriptor's matching lock without closing its file."""
    if os.name == "nt":
        import ctypes
        import msvcrt
        import pywintypes
        import win32file

        try:
            win32file.UnlockFileEx(
                msvcrt.get_osfhandle(fd), 1, 0, pywintypes.OVERLAPPED()
            )
        except pywintypes.error as exc:
            raise ctypes.WinError(exc.winerror) from None
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _lock_file_nb(fd: int) -> None:
    lock_file(fd, exclusive=True)


def _unlock_file(fd: int) -> None:
    unlock_file(fd)


def lock_held(path):
    """Observe an existing OS lock: True held, False released, None unknown.

    Never create/unlink the file or interpret its diagnostic PID. Opening a
    second descriptor and trying the real lock also works after owner exit.
    """
    try:
        fd = os.open(filesystem_path(path), os.O_RDWR)
    except OSError:
        return None
    try:
        try:
            _lock_file_nb(fd)
        except OSError as exc:
            return True if exc.errno in (errno.EACCES, errno.EAGAIN) else None
        _unlock_file(fd)
        return False
    finally:
        os.close(fd)


class StartLock:
    def __init__(self, path):
        self.path = filesystem_path(path)
        self.acquired = False
        self._fd = None

    def acquire(self, *, wait=0):
        if self.acquired:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = None if wait is None else time.monotonic() + wait
        try:
            # Windows permits locking beyond EOF; acquire before any write.
            while True:
                try:
                    _lock_file_nb(fd)
                    break
                except OSError as exc:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if exc.errno not in (errno.EACCES, errno.EAGAIN) or remaining is not None and remaining <= 0:
                        raise
                    time.sleep(.01 if remaining is None else min(.01, remaining))
        except OSError as exc:
            os.close(fd)
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            try:
                holder = json.loads(self.path.read_text() or "{}")
            except (OSError, ValueError):
                holder = {}
            raise StartInProgress(
                "another service start is in progress "
                f"(pid {holder.get('pid', '?')} since {holder.get('at', '?')})"
            ) from None
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "host": os.uname().nodename if hasattr(os, "uname") else "",
                "at": time.time(),
            }
        )
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, payload.encode("utf-8"))
        except OSError:
            pass  # diagnostic only; the OS lock is what excludes
        self._fd = fd
        self.acquired = True

    def release(self):
        if not self.acquired:
            return
        fd, self._fd = self._fd, None
        self.acquired = False
        try:
            _unlock_file(fd)
        except OSError:
            pass
        os.close(fd)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc):
        self.release()
