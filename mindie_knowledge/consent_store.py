"""Install-level one-time choices: the single persistent consent authority.

One small JSON document (``mindie-consent/1``) per installation profile. Every
adapter sharing the profile reads the same saved choice; an isolated profile
has its own file and never inherits another profile's choice.

This module is the ONE shared implementation of that document: pure stdlib
and fully self-contained (no imports from the ``mindie_knowledge`` package),
so an adapter that cannot load the runtime before first setup may carry a
byte-identical bootstrap copy instead of a third hand-written fork.

Semantics (the precise contract is the lane's ``API.md``):

- ``read`` has zero side effects and never migrates; it distinguishes
  ``ok``/``missing``/``unreadable``/``corrupt`` strictly. A damaged document
  is a fault, never a fresh install and never a guessed opt-in or opt-out.
- ``record_choice``/``record_reporting`` apply explicit user-intent updates
  under a cross-process OS lock (never only a thread lock): re-read, merge
  the other field and every untouched key, then a unique temp file, fsync
  and an atomic replace. A corrupt or unreadable file is never silently
  cleared by a field update — the update fails and the bytes stay.
- ``migrate`` imports already validated legacy choices exactly once at an
  explicit install/upgrade/entry boundary. An existing valid authority
  always wins; evidence that is missing or conflicting yields a diagnosable
  no-write result, preserving the original data.

Platform note: on Windows a stdlib ``open`` shares read/write but denies
delete, so an atomic ``os.replace`` would fail whenever a reader holds the
file open at that instant. This module's read path therefore opens with
FILE_SHARE_DELETE on Windows (see ``_open_for_read``): a writer never fails
because the authority was read. Readers outside this module that hold the
file open WITHOUT delete sharing can still transiently block a writer on
Windows — no writer-side mechanism exists for that (no retry wrapper is
added by design).
"""

from __future__ import annotations

import json
import os
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
    "migrate",
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
            if os.name == "nt" and os.fstat(fd).st_size == 0:
                os.write(fd, b" ")  # msvcrt.locking needs byte 0 to exist
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
    _GENERIC_READ = 0x80000000
    _SHARE_READ_WRITE_DELETE = 0x1 | 0x2 | 0x4
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _INVALID_HANDLE = _wt.HANDLE(-1).value


def _open_for_read(path):
    """Open the consent document for reading and return the binary stream.

    Windows only (used nowhere else): the open carries
    FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE. A stdlib
    ``open(path, "rb")`` on Windows shares read/write but DENIES delete, so
    ``os.replace`` (MoveFileEx) in ``_write_document`` fails with
    ERROR_SHARING_VIOLATION whenever a reader holds the file open at that
    instant — a writer must never fail because someone read the authority.
    With delete sharing, the atomic replace always wins against this reader.
    Error mapping preserves the read contract: file/path-not-found (2/3)
    becomes FileNotFoundError (``missing``), access-denied/sharing (5/32)
    becomes PermissionError (``unreadable``), anything else a plain OSError.
    """
    if os.name != "nt":
        return open(path, "rb")
    handle = _CreateFileW(
        str(path), _GENERIC_READ, _SHARE_READ_WRITE_DELETE,
        None, _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None,
    )
    if handle is None or handle == _INVALID_HANDLE:
        code = ctypes.get_last_error()
        if code in (2, 3):  # ERROR_FILE_NOT_FOUND / ERROR_PATH_NOT_FOUND
            raise FileNotFoundError(2, "consent file does not exist", str(path))
        if code in (5, 32):  # ERROR_ACCESS_DENIED / ERROR_SHARING_VIOLATION
            raise PermissionError(13, "consent file is not readable", str(path))
        raise OSError(None, f"cannot open consent file (winerror {code})", str(path))
    try:
        fd = _msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except OSError:
        _kernel32.CloseHandle(handle)
        raise
    return os.fdopen(fd, "rb")


def _read_bytes(path):
    with _open_for_read(path) as stream:
        return stream.read()


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
        _write_document(path, document)
    return read(path)


def record_choice(path, choice):
    """Persist one explicit contribution choice; merge everything else.

    The reporting value and all untouched metadata survive. ``migrated_from``
    is dropped: an explicit choice supersedes migration provenance. A missing
    file is created (explicit first choice); a corrupt or unreadable one is
    preserved and reported, never silently cleared.
    """
    if choice not in CHOICES:
        raise ValueError("choice must be contribute, read-only, later or disabled")

    def mutate(document):
        document.pop("migrated_from", None)
        document.update(schema=SCHEMA, choice=choice, choice_at=time.time())

    return _record(path, mutate)


def record_reporting(path, value):
    """Persist one explicit reporting choice; merge everything else.

    The contribution choice and all untouched metadata (including
    ``migrated_from``) survive; the two consents never overwrite each other.
    """
    if value not in REPORTING:
        raise ValueError("reporting must be enabled, disabled or later")

    def mutate(document):
        document.update(schema=SCHEMA, reporting=value, reporting_at=time.time())

    return _record(path, mutate)


# ------------------------------------------------------------------ migrate


def _check_candidates(candidates):
    validated = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("each migration candidate must be a mapping")
        choice = candidate.get("choice")
        reporting = candidate.get("reporting")
        source = candidate.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("each migration candidate names its source")
        if choice is not None and choice not in CHOICES:
            raise ValueError(f"candidate {source!r} carries an invalid choice")
        if reporting is not None and reporting not in REPORTING:
            raise ValueError(f"candidate {source!r} carries an invalid reporting value")
        validated.append(
            dict(choice=choice, reporting=reporting, source=source.strip())
        )
    return validated


def migrate(path, candidates):
    """Import validated legacy choices exactly once, at an explicit boundary.

    An existing valid saved choice always wins (``kept``). Otherwise the
    import writes only when every choice-carrying candidate agrees
    (``migrated``); missing evidence is ``absent`` and conflicting evidence is
    a diagnosable ``conflict`` — both without a write. A corrupt or
    unreadable authority is an ``error``: the original data is preserved and
    no public authorization is ever guessed.
    """
    path = Path(path)
    validated = _check_candidates(candidates)
    with _UpdateLock(path.with_name(path.name + ".lock")):
        state, data = _read_raw(path)
        if state == "ok":
            error = _validate(data)
            state = "corrupt" if error is not None else "ok"
        result = dict(
            status=None, state=state, choice=None, reporting=None,
            path=str(path), error=None, detail="", sources=[],
        )
        if state in ("corrupt", "unreadable"):
            result.update(
                status="error",
                error=f"consent file is {state}; original data preserved",
            )
            return result
        if state == "ok" and data.get("choice") in CHOICES:
            view = read(path)
            result.update(
                status="kept", choice=view["choice"],
                reporting=view["reporting"],
                detail="a valid saved choice already exists; authority kept",
            )
            return result
        choices = sorted({c["choice"] for c in validated if c["choice"] is not None})
        if len(choices) > 1:
            result.update(
                status="conflict",
                error="legacy sources disagree on the saved choice",
                detail="conflicting choices: " + ", ".join(
                    f"{c['source']}={c['choice']}"
                    for c in validated
                    if c["choice"] is not None
                ),
                sources=[c["source"] for c in validated if c["choice"] is not None],
            )
            return result
        document = dict(data) if state == "ok" else {}
        if not choices:
            if state == "ok":
                result["reporting"] = read(path)["reporting"]
            result.update(
                status="absent",
                detail="no legacy choice evidence; nothing written",
            )
            return result
        reportings = sorted(
            {c["reporting"] for c in validated if c["reporting"] is not None}
        )
        fills_reporting = document.get("reporting") not in REPORTING and reportings
        if fills_reporting and len(reportings) > 1:
            result.update(
                status="conflict",
                error="legacy sources disagree on the reporting choice",
                detail="conflicting reporting values: " + ", ".join(
                    f"{c['source']}={c['reporting']}"
                    for c in validated
                    if c["reporting"] is not None
                ),
                sources=[c["source"] for c in validated if c["reporting"] is not None],
            )
            return result
        document["schema"] = SCHEMA
        document["choice"] = choices[0]
        document["choice_at"] = time.time()
        if fills_reporting:
            document["reporting"] = reportings[0]
            document["reporting_at"] = time.time()
        document["migrated_from"] = sorted(
            {c["source"] for c in validated if c["choice"] or c["reporting"]}
        )
        _write_document(path, document)
        view = read(path)
        result.update(
            status="migrated", state="ok", choice=view["choice"],
            reporting=view["reporting"],
            detail="legacy choice imported once",
            sources=list(document["migrated_from"]),
        )
        return result
