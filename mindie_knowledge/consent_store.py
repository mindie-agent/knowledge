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
and keeps old handles valid for reads. A handle opened WITHOUT
FILE_SHARE_DELETE still fails the rename with WinError 32; that transient
sharing violation alone is retried for a bounded time (``_replace_windows``),
and any other failure — or a persistent holder — raises the original
WinError with the preserved document. No fallback silently changes
semantics on a filesystem without that support.
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
RENAME_RETRY_SECONDS = 5.0
_WINERROR_SHARING_VIOLATION = 32

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


if os.name == "nt":
    import ctypes
    from ctypes import wintypes as _wt
    import msvcrt as _msvcrt

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = (
        _wt.LPCWSTR, _wt.DWORD, _wt.DWORD,
        _wt.LPVOID, _wt.DWORD, _wt.DWORD, _wt.HANDLE,
    )
    _CreateFileW.restype = _wt.HANDLE
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = (_wt.HANDLE,)
    _CloseHandle.restype = _wt.BOOL
    _SetFileInformationByHandle = _kernel32.SetFileInformationByHandle
    _SetFileInformationByHandle.argtypes = (
        _wt.HANDLE, ctypes.c_int, _wt.LPVOID, _wt.DWORD,
    )
    _SetFileInformationByHandle.restype = _wt.BOOL
    _GENERIC_READ = 0x80000000
    _DELETE_ACCESS = 0x00010000
    _SHARE_READ_WRITE_DELETE = 0x1 | 0x2 | 0x4
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _INVALID_HANDLE = _wt.HANDLE(-1).value
    _FILE_RENAME_INFO_EX = 22
    _RENAME_REPLACE_IF_EXISTS = 0x1
    _RENAME_POSIX_SEMANTICS = 0x2


def _map_win_error(code, path):
    """Preserve the Windows error as the read/write contract's OSError kind."""
    return OSError(0, ctypes.FormatError(code), str(path), code)


def _open_for_read(path):
    """Open the consent document for reading and return the binary stream.

    On Windows the open carries
    FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE. A stdlib
    ``open(path, "rb")`` on Windows shares read/write but DENIES delete.
    Delete sharing by itself does NOT make ``os.replace`` succeed —
    MoveFileExW still refuses a destination with any open handle (CPython
    issue 90161); the writer side therefore uses the POSIX-semantics rename
    in ``_atomic_replace``. Readers still must share delete so existing
    handles stay valid after the superseding rename instead of pinning the
    old name. Error mapping preserves the read contract: file/path-not-found
    (2/3) becomes FileNotFoundError (``missing``), access-denied/sharing
    (5/32) becomes PermissionError (``unreadable``), anything else a plain
    OSError.
    """
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        return os.fdopen(fd, "rb")
    handle = _CreateFileW(
        str(path), _GENERIC_READ, _SHARE_READ_WRITE_DELETE,
        None, _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None,
    )
    if handle is None or handle == _INVALID_HANDLE:
        raise _map_win_error(ctypes.get_last_error(), path)
    try:
        fd = _msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except OSError:
        _CloseHandle(handle)
        raise
    return os.fdopen(fd, "rb")


def _read_bytes(path):
    with _open_for_read(path) as stream:
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
    """Side-effect-free read of the consent authority. Never raises.

    The reported ``state`` keeps a missing file, an unreadable one, a damaged
    one and a valid saved choice strictly apart. Nothing is created, locked,
    migrated or otherwise modified; safe to call at every gate.
    """
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


def _atomic_replace(source, target):
    """Atomically supersede ``target`` with the already-written ``source``.

    POSIX uses ``os.replace`` unchanged. On Windows, MoveFileExW
    (``os.replace``) refuses a destination with ANY open handle — including
    handles opened with FILE_SHARE_DELETE (CPython issue 90161) — so the
    replace instead opens the source with DELETE access (required for
    rename; full read/write/delete sharing) and issues
    ``SetFileInformationByHandle(FileRenameInfoEx=22)`` with
    FILE_RENAME_REPLACE_IF_EXISTS|FILE_RENAME_POSIX_SEMANTICS (flags 0x3).
    Per MSDN FILE_RENAME_INFORMATION, that supersedes the destination even
    with existing handles, keeps old handles valid for reads, and opens the
    renamed file at the target name afterwards. The source handle is closed
    in all cases. Errors keep the WinError as OSError/PermissionError via
    ``_map_win_error``. No delete-then-move gap, no fallback that would
    silently change the semantics on filesystems without support.
    """
    if os.name != "nt":
        os.replace(source, target)
        return
    _replace_windows(source, target)


def _replace_windows(source, target):
    """One bounded retry loop around ``_rename_windows_once``.

    A handle on the destination opened WITHOUT FILE_SHARE_DELETE (a plain
    stdlib reader, an indexer) still fails the POSIX-semantics rename with
    ERROR_SHARING_VIOLATION (WinError 32). That holder is transient, so the
    replace retries only that exclusive-sharing failure for a bounded time;
    any other WinError is raised at once, and a persistent holder re-raises
    the original WinError 32 with both files byte-identical — never a silent
    skip, never a partial state.
    """
    deadline = time.monotonic() + RENAME_RETRY_SECONDS
    while True:
        try:
            _rename_windows_once(source, target)
            return
        except OSError as exc:
            if (getattr(exc, "winerror", None) != _WINERROR_SHARING_VIOLATION
                    or time.monotonic() >= deadline):
                raise
            time.sleep(LOCK_POLL_SECONDS)


def _rename_windows_once(source, target):
    handle = _CreateFileW(
        str(source), _DELETE_ACCESS, _SHARE_READ_WRITE_DELETE,
        None, _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None,
    )
    if handle is None or handle == _INVALID_HANDLE:
        raise _map_win_error(ctypes.get_last_error(), source)
    try:
        name = os.path.abspath(target)
        name_bytes = name.encode("utf-16-le")  # byte length covers non-BMP
        ptr = ctypes.sizeof(ctypes.c_void_p)
        root_off = (4 + ptr - 1) // ptr * ptr  # Flags DWORD, HANDLE aligned
        length_off = root_off + ptr
        name_off = length_off + 4
        buffer = (ctypes.c_char * (name_off + len(name_bytes) + 2))()  # NUL-terminated
        struct.pack_into(
            "<I", buffer, 0, _RENAME_REPLACE_IF_EXISTS | _RENAME_POSIX_SEMANTICS
        )
        struct.pack_into("<I", buffer, length_off, len(name_bytes))
        ctypes.memmove(ctypes.addressof(buffer) + name_off, name_bytes, len(name_bytes))
        if not _SetFileInformationByHandle(
            handle, _FILE_RENAME_INFO_EX, buffer, len(buffer)
        ):
            raise _map_win_error(ctypes.get_last_error(), target)
    finally:
        _CloseHandle(handle)


def _write_document(path, data):
    """Replace ``path`` atomically: unique temp file, fsync, _atomic_replace."""
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
            os.fsync(stream.fileno())
        _atomic_replace(temporary, path)
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
