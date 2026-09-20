"""Single-active-starter lock anchored on one persistent file.

The lock itself is OS-managed (``fcntl.flock`` on POSIX, ``msvcrt.locking``
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
import os
import time
from pathlib import Path


class StartInProgress(RuntimeError):
    pass


def _lock_file_nb(fd: int) -> None:
    """Non-blocking exclusive lock on the open file; raises OSError when held."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class StartLock:
    def __init__(self, path):
        self.path = Path(path)
        self.acquired = False
        self._fd = None

    def acquire(self):
        if self.acquired:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt" and os.fstat(fd).st_size == 0:
                os.write(fd, b" ")  # msvcrt.locking needs byte 0 to exist
            _lock_file_nb(fd)
        except OSError:
            os.close(fd)
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
