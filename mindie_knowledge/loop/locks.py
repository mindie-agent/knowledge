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
import errno
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


def lock_held(path):
    """Observe an existing OS lock: True held, False released, None unknown.

    Never create/unlink the file or interpret its diagnostic PID. Opening a
    second descriptor and trying the real lock also works after owner exit.
    """
    try:
        fd = os.open(path, os.O_RDWR)
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
        self.path = Path(path)
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
