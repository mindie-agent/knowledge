"""[Role] Cross-platform artifact liveness for reusable runtime generations.

[Invariants]
- A generation being used by a cooperating 0.13.21+ process carries a shared
  ``.inuse.lock`` lease; the OS releases it on crash, never a PID tombstone.
- An already-running pre-0.13.21 process has no lease. Its command line (POSIX
  ``ps``) or native image path (Windows Toolhelp32) remains protection, and a
  failed, exited-non-zero or truncated inspection is reported as incomplete,
  never as "nothing is running".
- Full command lines are retained beside extracted tokens so a path containing
  spaces or CJK characters is matched by path boundary, not whitespace
  tokenization. Windows system processes that deny ``OpenProcess`` are not an
  incomplete inspection unless the image could be a Python/Node/uv host.
- The current interpreter/prefix are always protected, so a sweep can never
  retire the process performing it.
- Windows never spawns a console helper: the process snapshot uses kernel32
  directly (zero console popups).

[Timeouts & Locks]
- ``live_process_prefixes`` bounds ``ps``/snapshot work by its caller timeout
  (default 2.0 s) and caps output at 8 MiB. POSIX reads only
  ``ps -ww -axo command=`` with ``COLUMNS`` removed; a non-zero exit, timeout
  or capped output is incomplete and never falls back to a truncated table.
  ``is_lease_held`` is one non-blocking lock probe per candidate file; nothing
  waits. ``acquire_retirement_locks`` is non-blocking: POSIX holds the entry
  lease/builder handles until the caller's ``os.replace`` completed, while
  Windows holds a sibling ``.mindie-retire.lock`` and zero entry handles so
  the NTFS rename can succeed.

[Side-Effect Policy]
- Read-only process inspection. ``acquire_runtime_lease`` creates and removes
  only its own ``.inuse.lock``/``.inuse.*.lease`` files inside an existing
  generation directory; no configuration, knowledge state or remote I/O.
"""
from __future__ import annotations

import ntpath
import os
import re
import select
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

from ..filesystem import filesystem_path, path_identity
from ..loop.locks import lock_file, unlock_file

LEASE_NAME = ".inuse.lock"
PREPARE_LOCK_NAME = "prepare.lock"
RETIRE_LOCK_NAME = ".mindie-retire.lock"
BUILDER_LOCK_NAMES = (PREPARE_LOCK_NAME, ".lock")
_IS_WINDOWS = os.name == "nt"
_TRASH_PREFIX = ".trash-"
_LEASE_GLOB = ".inuse.*.lease"
_MAX_COMMAND_OUTPUT = 8 * 1024 * 1024
# Absolute POSIX command-line tokens: interpreter paths, script paths, cwd
# arguments. Full command lines are retained separately for whitespace/CJK
# paths that this ASCII tokenizer cannot split.
_PATH_TOKEN = re.compile(r"/(?:[A-Za-z0-9._+@%=-]+/)*[A-Za-z0-9._+@%=-]+")
_TRAILING = ".,;:'\")]}>"
# Windows image basenames whose denied process handle could hide a host.
_WINDOWS_HOST_IMAGES = frozenset({
    "python.exe", "pythonw.exe", "node.exe", "uv.exe", "dsh.exe", "codex.exe",
    "kimi.exe",
})
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87
_ERROR_NO_MORE_FILES = 18


class LivePaths(set):
    """Extracted path tokens plus the normalized command lines they came from."""

    __slots__ = ("commands",)

    def __init__(self, values=()):
        super().__init__(values)
        self.commands: tuple[str, ...] = ()


def _identity(value) -> str:
    try:
        return path_identity(value)
    except TimeoutError:
        raise
    except (OSError, RuntimeError):
        text = os.path.normpath(os.fspath(value))
        if os.name == "nt":
            import ntpath

            return ntpath.normcase(ntpath.normpath(text))
        return text


class RuntimeLease:
    """One held generation lease; ``release`` is idempotent."""

    def __init__(self, directory, descriptor=None, lease_file=None):
        self.directory = Path(directory)
        self._descriptor = descriptor
        self._lease_file = Path(lease_file) if lease_file is not None else None

    def release(self):
        descriptor, self._descriptor = self._descriptor, None
        lease_file, self._lease_file = self._lease_file, None
        if descriptor is None:
            return
        try:
            try:
                unlock_file(descriptor)
            except TimeoutError:
                raise
            except OSError:
                pass  # The OS still releases the lock when the descriptor closes.
        finally:
            try:
                os.close(descriptor)
            finally:
                if lease_file is not None:
                    try:
                        lease_file.unlink(missing_ok=True)
                    except TimeoutError:
                        raise
                    except OSError:
                        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.release()


def _directory_state(directory: Path):
    return os.stat(filesystem_path(directory))


def _verify_lease_directory(directory: Path, expected) -> None:
    try:
        current = _directory_state(directory)
    except FileNotFoundError:
        raise RuntimeError(
            "runtime generation moved while its lease was acquired") from None
    if (not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)):
        raise RuntimeError(
            "runtime generation moved while its lease was acquired")


def acquire_runtime_lease(directory):
    """Hold a shared lease in ``directory`` until the context exits.

    POSIX locks ``.inuse.lock`` (``LOCK_SH``); Windows also opens a private
    ``.inuse.<pid>.<uuid>.lease`` without ``FILE_SHARE_DELETE``. That open
    handle makes any NTFS ``os.replace(<directory>, ...)`` fail atomically
    while this process is alive, which is the kernel-enforced safety net.
    The directory inode is re-verified after the lock so a concurrent
    two-phase retirement cannot hand this process a lease in ``.trash-*``.
    """
    directory = Path(directory)
    if directory.name.startswith(_TRASH_PREFIX):
        raise RuntimeError("refusing to lease a retired generation")
    before = _directory_state(directory)
    directory_descriptor = None
    if os.name != "nt":
        try:
            directory_descriptor = os.open(filesystem_path(directory), os.O_RDONLY)
        except TimeoutError:
            raise
        except OSError:
            directory_descriptor = None
    try:
        expected = (os.fstat(directory_descriptor)
                    if directory_descriptor is not None else before)
        lock_path = filesystem_path(directory / LEASE_NAME)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            lock_file(descriptor, exclusive=False)
            _verify_lease_directory(directory, expected)
            if os.name == "nt":
                lease_file = directory / (
                    f".inuse.{os.getpid()}.{uuid.uuid4().hex}.lease")
                lease_descriptor = os.open(filesystem_path(lease_file),
                                           os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    try:
                        lock_file(lease_descriptor, exclusive=False)
                    except TimeoutError:
                        raise
                    except (ImportError, OSError):
                        # The open handle already blocks directory rename on NTFS.
                        pass
                    # Windows liveness is the non-share-delete lease handle; the
                    # shared .inuse.lock descriptor is no longer needed.
                    os.close(descriptor)
                    descriptor = None
                    return RuntimeLease(directory, lease_descriptor, lease_file)
                except BaseException:
                    os.close(lease_descriptor)
                    try:
                        lease_file.unlink(missing_ok=True)
                    except TimeoutError:
                        raise
                    except OSError:
                        pass
                    raise
            return RuntimeLease(directory, descriptor)
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            raise
    finally:
        if directory_descriptor is not None:
            os.close(directory_descriptor)


class RetirementLocks:
    """Exclusive locks held across one atomic directory rename.

    POSIX holds ``.inuse.lock``/``prepare.lock`` inside the candidate; Windows
    holds only the sibling ``.mindie-retire.lock`` and zero entry descriptors
    (NTFS rejects a rename while any handle inside is open, even this process).
    """

    def __init__(self, descriptors=(), *, sibling_only: bool = False):
        self._descriptors = list(descriptors)
        self.sibling_only = sibling_only

    @property
    def descriptors_inside_entry(self) -> tuple[int, ...]:
        return () if self.sibling_only else tuple(self._descriptors)

    def release(self):
        descriptors, self._descriptors = self._descriptors, []
        for descriptor in reversed(descriptors):
            try:
                try:
                    unlock_file(descriptor)
                except TimeoutError:
                    raise
                except OSError:
                    pass
            finally:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.release()


def _release_descriptors(descriptors) -> None:
    for descriptor in descriptors:
        try:
            unlock_file(descriptor)
        except TimeoutError:
            raise
        except OSError:
            pass
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass


def acquire_retirement_locks(directory):
    """Hold non-blocking exclusive locks for one candidate retirement.

    Returns ``None`` on contention or an uncreatable lock (treat as "in use").
    POSIX locks the lease/builder files inside the candidate; Windows locks one
    sibling ``.mindie-retire.lock`` and never creates a handle inside it.
    """
    directory = Path(directory)
    if _IS_WINDOWS:
        return _acquire_sibling_lock(directory)
    return _acquire_entry_locks(directory)


def _acquire_sibling_lock(directory):
    """One non-blocking sibling lock descriptor; no handle inside ``directory``."""
    descriptor = None
    try:
        descriptor = os.open(
            filesystem_path(Path(directory).parent / RETIRE_LOCK_NAME),
            os.O_CREAT | os.O_RDWR, 0o600)
        lock_file(descriptor, exclusive=True)
    except BlockingIOError:
        os.close(descriptor)
        return None
    except TimeoutError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        return None
    return RetirementLocks([descriptor], sibling_only=True)


def _acquire_entry_locks(directory):
    """POSIX lease/builder locks that may stay open across a directory rename."""
    directory = Path(directory)
    try:
        before = os.stat(filesystem_path(directory))
    except TimeoutError:
        raise
    except OSError:
        before = None
    descriptors: list[int] = []
    created = False
    names = [LEASE_NAME, PREPARE_LOCK_NAME]
    try:
        if os.path.lexists(filesystem_path(directory / ".lock")):
            names.append(".lock")  # A builder that used the alternate name.
    except TimeoutError:
        raise
    except OSError:
        pass
    for name in names:
        path = filesystem_path(directory / name)
        descriptor = None
        for _attempt in range(2):
            try:
                descriptor = os.open(path, os.O_RDWR)
                break
            except FileNotFoundError:
                try:
                    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                    created = True
                    break
                except FileExistsError:
                    continue
                except TimeoutError:
                    _release_descriptors(descriptors)
                    raise
                except OSError:
                    _release_descriptors(descriptors)
                    return None
            except TimeoutError:
                _release_descriptors(descriptors)
                raise
            except OSError:
                _release_descriptors(descriptors)
                return None
        if descriptor is None:
            _release_descriptors(descriptors)
            return None
        try:
            lock_file(descriptor, exclusive=True)
        except BlockingIOError:
            os.close(descriptor)
            _release_descriptors(descriptors)
            return None
        except TimeoutError:
            os.close(descriptor)
            _release_descriptors(descriptors)
            raise
        except BaseException:
            os.close(descriptor)
            _release_descriptors(descriptors)
            raise
        descriptors.append(descriptor)
    if created and before is not None:
        # Creating our lock files must not make an old generation look like a
        # fresh staging build to the next grace check.
        try:
            os.utime(filesystem_path(directory),
                     ns=(before.st_atime_ns, before.st_mtime_ns))
        except TimeoutError:
            raise
        except OSError:
            pass
    return RetirementLocks(descriptors, sibling_only=False)


def _windows_lease_held(lease_file):
    """A live lease cannot be opened without share-delete, locked, or unlinked."""
    try:
        descriptor = os.open(filesystem_path(lease_file), os.O_RDWR)
    except TimeoutError:
        raise
    except OSError:
        return True
    try:
        try:
            lock_file(descriptor, exclusive=True)
        except BlockingIOError:
            return True
        except TimeoutError:
            raise
        except OSError:
            return True
        try:
            unlock_file(descriptor)
        except TimeoutError:
            raise
        except OSError:
            return True
    finally:
        os.close(descriptor)
    try:
        Path(lease_file).unlink()
    except TimeoutError:
        raise
    except OSError:
        return True
    return False


def windows_lease_present(directory) -> bool:
    """True when a Windows private lease handle still blocks directory rename."""
    if not _IS_WINDOWS:
        return False
    try:
        return any(_windows_lease_held(lease)
                   for lease in Path(directory).glob(_LEASE_GLOB))
    except TimeoutError:
        raise
    except OSError:
        return True


def is_lease_held(generation_dir) -> bool:
    """True when another live process holds this generation's lease.

    An unreadable or unopenable lease is held: cleanup may not treat an
    inspection failure as permission to retire a directory.
    """
    directory = Path(generation_dir)
    lock_path = directory / LEASE_NAME
    try:
        descriptor = os.open(filesystem_path(lock_path), os.O_RDWR)
    except FileNotFoundError:
        descriptor = None
    except TimeoutError:
        raise
    except OSError:
        return True
    if descriptor is not None:
        try:
            try:
                lock_file(descriptor, exclusive=True)
            except BlockingIOError:
                return True
            except TimeoutError:
                raise
            except OSError:
                return True
            try:
                unlock_file(descriptor)
            except TimeoutError:
                raise
            except OSError:
                return True
        finally:
            os.close(descriptor)
    if os.name == "nt":
        return windows_lease_present(directory)
    return False


def _normalize(value) -> str:
    return _identity(value)


def _spellings(value) -> set[str]:
    raw = os.fspath(value)
    identities = {os.path.abspath(raw), os.path.realpath(raw)}
    try:
        identities.add(path_identity(raw))
    except TimeoutError:
        raise
    except (OSError, RuntimeError):
        pass
    spellings = {_normalize(item) for item in identities}
    # Keep the unresolved spelling too: a live ``ps`` command line can name a
    # symlinked prefix that ``path_identity`` has already resolved.
    for item in identities:
        text = os.path.normpath(item)
        spellings.add(ntpath.normcase(text) if os.name == "nt" else text)
    return spellings


def _within(child: str, parent: str) -> bool:
    if child == parent:
        return True
    separator = "\\" if os.name == "nt" else "/"
    prefix = parent if parent.endswith(separator) else parent + separator
    return child.startswith(prefix)


def _command_references(command: str, spelling: str) -> bool:
    """A path-boundary match inside one live command line."""
    if not command or not spelling:
        return False
    if os.name == "nt":
        haystack, needle = command.casefold(), spelling.casefold()
    else:
        haystack, needle = command, spelling
    start = 0
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            return False
        before = haystack[index - 1] if index else ""
        end = index + len(needle)
        after = haystack[end] if end < len(haystack) else ""
        if (not before or before in " \t\"'=") \
                and (not after or after in "/\\ \t\"'"):
            return True
        start = index + 1


def is_path_used_by_live_process(candidate_dir, live_paths) -> bool:
    """Whether ``candidate_dir`` is referenced by, or contains, a live path.

    Also always protects the current Python interpreter and prefix so a sweep
    invoked from inside a managed runtime can never retire itself. Full
    command lines retained by ``live_process_prefixes`` are matched with path
    boundaries, so ``Application Support``/``中文目录`` are not lost to
    whitespace or ASCII tokenization.
    """
    candidate_keys = _spellings(candidate_dir)
    for protected in (sys.executable, sys.prefix, getattr(sys, "base_prefix", sys.prefix)):
        for key in _spellings(protected):
            if any(_within(key, candidate) for candidate in candidate_keys):
                return True
    for command in getattr(live_paths, "commands", ()) or ():
        if isinstance(command, str) and any(
                _command_references(command, candidate) for candidate in candidate_keys):
            return True
    for entry in live_paths or ():
        if not isinstance(entry, (str, os.PathLike)):
            continue
        text = os.fspath(entry)
        if isinstance(text, str) and any(character.isspace() for character in text):
            # A caller may pass raw command lines instead of LivePaths; match
            # them with the same path-boundary rule.
            if any(_command_references(text, candidate) for candidate in candidate_keys):
                return True
        key = _normalize(text)
        if any(_within(key, candidate) for candidate in candidate_keys):
            return True
    return False


class _PsCommandFailed(Exception):
    """``ps`` exited non-zero; the inspection stays incomplete."""


def _run_ps(executable, arguments, timeout):
    """Bounded ``ps`` stdout, or ``None`` when inspection is incomplete.

    ``COLUMNS`` is removed from the child environment and ``-ww`` is supplied
    by the caller, so procps never truncates long command lines. Output is
    read up to one byte past the cap; reaching the cap is truncation, not a
    complete process table. A non-zero exit raises ``_PsCommandFailed``.
    """
    env = dict(os.environ)
    env.pop("COLUMNS", None)
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = subprocess.Popen(
            [executable, *arguments], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, **kwargs)
    except (OSError, ValueError):
        return None
    deadline = None if timeout is None else time.monotonic() + timeout
    chunks: list[bytes] = []
    size = 0
    try:
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return None
            try:
                ready, _, _ = select.select([process.stdout], [], [], remaining)
            except (OSError, ValueError):
                return None
            if not ready:
                continue
            try:
                chunk = os.read(process.stdout.fileno(), 64 * 1024)
            except (OSError, ValueError):
                return None
            if not chunk:
                break
            size += len(chunk)
            if size >= _MAX_COMMAND_OUTPUT:
                return None
            chunks.append(chunk)
        remaining = None if deadline is None else deadline - time.monotonic()
        try:
            code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            return None
        if code:
            raise _PsCommandFailed(code)
        return b"".join(chunks)
    finally:
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
        try:
            process.stdout.close()
        except OSError:
            pass


def _posix_live_paths(timeout):
    from .._host_exec import which_native
    executable = which_native("ps") or ("/bin/ps" if os.path.isfile("/bin/ps") else None)
    if executable is None:
        return LivePaths(), False
    try:
        # ``-ww`` is supported by macOS, BSD and Linux procps. Never fall back
        # to the 80-column ``ps -axo`` form: its truncated command lines silently
        # hide live generations, so a failure here must stay incomplete.
        output = _run_ps(executable, ["-ww", "-axo", "command="], timeout)
    except _PsCommandFailed:
        return LivePaths(), False
    if output is None:
        return LivePaths(), False
    result = LivePaths()
    commands: list[str] = []
    for line in os.fsdecode(output).splitlines():
        if line.strip():
            commands.append(line)
        for match in _PATH_TOKEN.finditer(line):
            token = match.group(0).rstrip(_TRAILING)
            if len(token) < 2:
                continue
            result.update(_spellings(token))
    result.commands = tuple(commands)
    return result, True


if os.name == "nt":  # pragma: no cover - exercised on a real Windows host
    import ctypes
    from ctypes import wintypes

    _TH32CS_SNAPPROCESS = 0x00000002
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]


def _windows_process_error_is_incomplete(error: int, executable: str) -> bool:
    """Classify one failed ``OpenProcess`` without treating SYSTEM as a leak.

    ``ERROR_INVALID_PARAMETER`` means the process exited mid-scan. ``ACCESS_DENIED``
    is normal for ``csrss.exe``/``svchost.exe`` owned by SYSTEM; only a denied
    Python/Node/uv host could hide a user generation and makes the snapshot
    incomplete. Every other error is unknown and therefore incomplete.
    """
    if error == _ERROR_INVALID_PARAMETER:
        return False
    if error == _ERROR_ACCESS_DENIED:
        return str(executable or "").casefold() in _WINDOWS_HOST_IMAGES
    return True


def _windows_live_paths(timeout):
    """Native process image paths; never powershell/wmic/tasklist consoles."""
    if os.name != "nt":  # pragma: no cover - defensive for import parity
        return LivePaths(), False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot is None or snapshot == _INVALID_HANDLE_VALUE:
        return LivePaths(), False
    deadline = None
    if timeout is not None:
        import time
        deadline = time.monotonic() + timeout
    result = LivePaths()
    complete = True
    entry = _ProcessEntry32W()
    entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
    try:
        present = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        if not present and ctypes.get_last_error() != _ERROR_NO_MORE_FILES:
            complete = False
        while present:
            if deadline is not None:
                import time
                if time.monotonic() >= deadline:
                    return result, False
            process = kernel32.OpenProcess(
                _PROCESS_QUERY_LIMITED_INFORMATION, False, entry.th32ProcessID)
            if process:
                try:
                    size = wintypes.DWORD(32768)
                    buffer = ctypes.create_unicode_buffer(size.value)
                    if kernel32.QueryFullProcessImageNameW(
                            process, 0, buffer, ctypes.byref(size)):
                        result.update(_spellings(buffer.value))
                finally:
                    kernel32.CloseHandle(process)
            elif _windows_process_error_is_incomplete(
                    ctypes.get_last_error(), entry.szExeFile):
                complete = False
            present = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            if not present and ctypes.get_last_error() != _ERROR_NO_MORE_FILES:
                complete = False
    finally:
        kernel32.CloseHandle(snapshot)
    return result, complete


def live_process_prefixes(*, timeout=2.0):
    """Return ``(normalized_paths, inspection_complete)`` for live processes.

    POSIX reads only ``ps -ww -axo command=`` with ``COLUMNS`` stripped;
    Windows walks Toolhelp32 and queries each image path through kernel32.
    Both preserve the non-resolved and ``realpath`` spelling of every token so
    a venv symlink prefix also protects its target generation. POSIX
    additionally retains the full command lines for whitespace/CJK paths.
    A non-zero ``ps`` exit or capped output returns ``False`` for
    ``inspection_complete``; it is never reported as an empty complete table.
    """
    if os.name == "nt":
        return _windows_live_paths(timeout)
    return _posix_live_paths(timeout)


def path_token_is_absolute(token: str) -> bool:
    """Small shared predicate for legacy scanners (defensive, not identity)."""
    if not isinstance(token, str) or not token:
        return False
    if os.name == "nt":
        return bool(re.match(r"^[A-Za-z]:[\\/]", token) or token.startswith("\\\\"))
    return token.startswith("/")


def is_directory(value) -> bool:
    """Directory predicated through ``stat`` so ``TimeoutError`` propagates."""
    try:
        return stat.S_ISDIR(os.stat(filesystem_path(value)).st_mode)
    except TimeoutError:
        raise
    except OSError:
        return False


__all__ = [
    "BUILDER_LOCK_NAMES",
    "LEASE_NAME",
    "LivePaths",
    "PREPARE_LOCK_NAME",
    "RETIRE_LOCK_NAME",
    "RetirementLocks",
    "RuntimeLease",
    "acquire_retirement_locks",
    "acquire_runtime_lease",
    "is_directory",
    "is_lease_held",
    "is_path_used_by_live_process",
    "live_process_prefixes",
    "path_token_is_absolute",
    "windows_lease_present",
]
