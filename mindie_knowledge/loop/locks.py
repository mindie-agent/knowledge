"""[Role] Single-active-starter OS file lock with a persistent diagnostic record.

[Invariants]
- The lock is OS-managed (``fcntl.flock`` on POSIX, ``LockFileEx`` on Windows)
  and released by the OS when the holder exits or crashes; the file is never
  unlinked, so an old owner cannot delete a later acquisition.
- The JSON record is diagnostic metadata, never liveness evidence. On the
  database init lock its presence is authority evidence only after the DB was
  created and validated; a created-but-unlocked or pre-DB file is not evidence.
- ``acquire(write_record=False)`` observes without adding history, and
  ``persist_record()`` is the strict write for callers that use the record as
  authority evidence.

[Timeouts & Locks]
- Bounded waits use ``wait`` seconds or ``MINDIE_START_LOCK_WAIT_SECONDS``;
  zero is one non-blocking attempt. The record is read or written only while
  the OS lock is held.

[Side-Effect Policy]
- Local file descriptors and bytes only; no network, subprocess, or globals.
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


_UNSET = object()


class StartLock:
    def __init__(self, path, *, wait=0):
        self.path = filesystem_path(path)
        self.acquired = False
        self._fd = None
        self.wait = wait
        # True only when this acquisition observed a record written by an
        # earlier acquisition, before replacing it under the held OS lock.
        self.prior_record = False

    def acquire(self, *, wait=_UNSET, write_record=True):
        """Take the OS lock; ``write_record=False`` observes without adding evidence."""
        if self.acquired:
            return
        if wait is _UNSET:
            env_wait = os.environ.get("MINDIE_START_LOCK_WAIT_SECONDS")
            wait = float(env_wait) if env_wait is not None else self.wait
        elif wait is not None and wait > 0 and "MINDIE_START_LOCK_WAIT_SECONDS" in os.environ:
            wait = float(os.environ["MINDIE_START_LOCK_WAIT_SECONDS"])
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
        try:
            # A record is written only while this lock is held. Read the
            # previous one under the lock, before replacing it, so callers
            # can distinguish "an earlier acquisition ran" from "a sibling
            # created the file and has not reached flock yet".
            os.lseek(fd, 0, os.SEEK_SET)
            self.prior_record = bool(os.read(fd, 4096))
        except OSError:
            os.close(fd)
            raise
        if write_record:
            self._write_record(fd, strict=False)
        self._fd = fd
        self.acquired = True

    @staticmethod
    def _record_payload():
        return json.dumps(
            {
                "pid": os.getpid(),
                "host": os.uname().nodename if hasattr(os, "uname") else "",
                "at": time.time(),
            }
        ).encode("utf-8")

    @staticmethod
    def _write_record(fd, *, strict):
        """Replace the record without emptying it first; strict failures raise."""
        encoded = StartLock._record_payload()
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            written = 0
            while written < len(encoded):
                count = os.write(fd, encoded[written:])
                if count <= 0:
                    raise OSError(errno.EIO, "short write")
                written += count
            os.ftruncate(fd, len(encoded))
            if strict:
                os.fsync(fd)
        except OSError:
            if strict:
                raise
            # Diagnostic-only callers keep the OS lock as what excludes.

    def persist_record(self):
        """Record a held lock as authority evidence; failure is never swallowed."""
        if not self.acquired or self._fd is None:
            raise RuntimeError("start lock must be held before its record is persisted")
        self._write_record(self._fd, strict=True)

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
