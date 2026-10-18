"""Install-level one-time choices: the single persistent consent authority.

One small JSON document (``mindie-consent/1``) per installation profile. Every
adapter sharing the profile reads the same saved choice; an isolated profile
has its own file and never inherits another profile's choice.

This module is the shared implementation used by adapters and the knowledge
runtime. Runtime installation does not create or replace the saved choice.

Semantics:

- ``read`` has zero side effects and never migrates; it distinguishes
  ``ok``/``missing``/``unreadable``/``corrupt`` strictly. A damaged document
  is a fault, never a fresh install and never a guessed opt-in or opt-out.
- ``record_choice``/``record_reporting`` apply explicit user-intent updates
  under a cross-process OS lock (never only a thread lock): re-read, merge
  the other field and every untouched key, then a unique temp file, fsync
  and an atomic replace. A corrupt or unreadable file is never silently
  cleared by a field update — the update fails and the bytes stay.

Platform note: on Windows a stdlib ``open`` shares read/write but denies
delete, and MoveFileExW (``os.replace``) refuses a destination with ANY
open handle — even one opened with FILE_SHARE_DELETE (CPython issue 90161).
This module therefore does BOTH halves: reads open with FILE_SHARE_DELETE
(``_open_for_read``), and writes supersede via the NTFS POSIX-semantics
rename ``SetFileInformationByHandle(FileRenameInfoEx, REPLACE_IF_EXISTS|
POSIX_SEMANTICS)`` (``_atomic_replace``), which tolerates existing handles
and keeps old handles valid for reads. On a filesystem/platform without
that support the write fails honestly with the preserved document and the
original WinError — no retry wrapper, no fallback that would silently
change semantics.
"""

from __future__ import annotations

import json
import errno
import os
import stat
import struct
import tempfile
import time
from pathlib import Path

SCHEMA = "mindie-consent/1"
CHOICES = ("contribute", "read-only", "later", "disabled")
REPORTING = ("enabled", "disabled", "later")
STATES = ("ok", "missing", "unreadable", "corrupt")
MAX_BYTES = 64 * 1024
LOCK_WAIT_SECONDS = 5.0
LOCK_POLL_SECONDS = 0.05

__all__ = [
    "SCHEMA",
    "CHOICES",
    "REPORTING",
    "STATES",
    "MAX_BYTES",
    "ConsentError",
    "read",
    "record_choice",
    "record_reporting",
]


class ConsentError(Exception):
    """A record operation could not honor the saved authority.

    ``state`` is ``corrupt``, ``unreadable`` or ``locked``. The consent file
    is left byte-identical: a damaged authority is never silently cleared by
    a field update.
    """

    def __init__(self, message, *, state):
        super().__init__(message)
        if state not in ("corrupt", "unreadable", "locked"):
            raise ValueError("ConsentError state must be corrupt, unreadable or locked")
        self.state = state


# --------------------------------------------------------------------- lock


def _lock_file_nb(fd):
    """Non-blocking exclusive OS lock on the open file; OSError when held."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(fd):
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class _UpdateLock:
    """Bounded cross-process exclusive lock on a sibling ``.lock`` file.

    The OS releases the lock when the holder exits or crashes, so there is no
    stale metadata to reclaim. The lock file is deliberately never unlinked:
    an old owner's close cannot delete a later acquisition.
    """

    def __init__(self, path, wait=LOCK_WAIT_SECONDS):
        self.path = Path(path)
        self.wait = wait
        self._fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            # Windows permits locking beyond EOF. Writing byte zero before
            # acquiring the lock races another first-time writer's lock.
            deadline = time.monotonic() + self.wait
            while True:
                try:
                    _lock_file_nb(fd)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ConsentError(
                            "consent update lock is held by another process",
                            state="locked",
                        ) from None
                    time.sleep(LOCK_POLL_SECONDS)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, *_exc):
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock_file(fd)
        except OSError:
            pass
        os.close(fd)


# --------------------------------------------------------------------- read


def _read_bytes(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError(errno.EINVAL, "authority is not a regular file")
        return stream.read(MAX_BYTES + 1)


def _read_raw(path):
    """(state, data): data is the raw document dict only when state is ok."""
    try:
        raw = _read_bytes(path)
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "unreadable", None
    if len(raw) > MAX_BYTES:
        return "corrupt", None
    try:
        data = json.loads(raw)
    except ValueError:
        return "corrupt", None
    if not isinstance(data, dict):
        return "corrupt", None
    return "ok", data


def _validate(data):
    """None when the document is a valid consent authority, else the error."""
    if data.get("schema") != SCHEMA:
        return "unsupported consent schema"
    choice = data.get("choice")
    if choice is not None and choice not in CHOICES:
        return "unknown consent choice"
    reporting = data.get("reporting")
    if reporting is not None and reporting not in REPORTING:
        return "unknown reporting choice"
    return None


def read(path):
    """Side-effect-free read of the consent authority. Never raises."""
    path = Path(path)
    result = dict(
        state=None, choice=None, reporting=None, choice_at=None,
        reporting_at=None, path=str(path), error=None,
    )
    state, data = _read_raw(path)
    result["state"] = state
    if state == "unreadable":
        result["error"] = "consent file is unreadable"
        return result
    if state == "corrupt":
        result["error"] = "consent file is damaged"
        return result
    if state == "missing":
        return result
    error = _validate(data)
    if error is not None:
        result.update(state="corrupt", error=error)
        return result
    result["choice"] = data.get("choice")
    result["reporting"] = data.get("reporting")
    for key in ("choice_at", "reporting_at"):
        value = data.get(key)
        result[key] = value if type(value) in (int, float) else None
    return result


# -------------------------------------------------------------------- write


def _write_document(path, data):
    """Replace ``path`` atomically: unique temp file, fsync, os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(data, indent=2) + "\n").encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            if os.environ.get("MINDIE_DISABLE_FSYNC") != "1":
                os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _record(path, mutate):
    """Serialized read-modify-write of one explicit field update."""
    path = Path(path)
    with _UpdateLock(path.with_name(path.name + ".lock")):
        state, data = _read_raw(path)
        if state == "ok":
            error = _validate(data)
            state = "corrupt" if error is not None else "ok"
        if state in ("corrupt", "unreadable"):
            raise ConsentError(
                f"consent file is {state}; explicit user repair is required "
                "before a new choice can be recorded",
                state=state,
            )
        document = dict(data) if state == "ok" else {}
        mutate(document)
        if state != "ok" or document != data:
            _write_document(path, document)
    return read(path)


def record_choice(path, choice):
    """Persist one explicit contribution choice; merge everything else.

    The reporting value and all untouched metadata survive. A missing
    file is created (explicit first choice); a corrupt or unreadable one is
    preserved and reported, never silently cleared.
    """
    if choice not in CHOICES:
        raise ValueError("choice must be contribute, read-only, later or disabled")

    def mutate(document):
        if document.get("choice") == choice:
            return
        document.update(schema=SCHEMA, choice=choice, choice_at=time.time())

    return _record(path, mutate)


def record_reporting(path, value):
    """Persist one explicit reporting choice; merge everything else.

    The contribution choice and all untouched metadata survive; the two
    consents never overwrite each other.
    """
    if value not in REPORTING:
        raise ValueError("reporting must be enabled, disabled or later")

    def mutate(document):
        document.update(schema=SCHEMA, reporting=value, reporting_at=time.time())

    return _record(path, mutate)
