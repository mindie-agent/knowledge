"""Temporary summary profiles share the process tree's cleanup owner.

[Role] Temporary summary profiles share the process tree cleanup owner.
[Invariants] Only the core deletes its own fresh root; a declared job/parent mismatch raises instead of adopting a foreign directory.
[Timeouts & Locks] Ownership checks use bounded stat/pipe operations; no long-lived lock is retained.
[Side-Effect Policy] Creates and removes a private scratch directory for one summary worker run.

The declaration is an internal parent/worker protocol, never user configuration
or a worker-provided deletion target. Only the core deletes its own fresh root.
"""
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time
import uuid

ENVIRONMENT_KEY = "MINDIE_SUMMARY_SCRATCH"
OWNER_MARKER = ".mindie-owner.json"
STALE_OWNER_SECONDS = 3600.0
_STALE_PREFIXES = ("mindie-summary-owner-", "mindie-distill-")


class ScratchOwnershipError(ValueError):
    """An incomplete owner contract must fail before invoking a model."""

    mindie_category = "configuration"


def _writer_exited(pid) -> bool:
    """Prove a recorded writer process is gone; a live/unknown pid is not proof."""
    if type(pid) is not int or pid <= 0 or pid == os.getpid():
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value != 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except TimeoutError:
        raise
    except (OSError, OverflowError):
        pass
    return False


def _sweep_stale_roots(parent: Path) -> None:
    """Remove only dirs carrying our explicit owner marker, dead and >1h old."""
    try:
        entries = list(parent.iterdir())
    except TimeoutError:
        raise
    except OSError:
        return
    now = time.time()
    for entry in entries:
        if not entry.name.startswith(_STALE_PREFIXES):
            continue
        marker = entry / OWNER_MARKER
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
            if type(value) is not dict or set(value) != {"pid", "created_at"}:
                continue
            if type(value["pid"]) is not int or not isinstance(value["created_at"], (int, float)):
                continue
            if now - value["created_at"] < STALE_OWNER_SECONDS:
                continue
            if not _writer_exited(value["pid"]):
                continue
            info = entry.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) \
                    or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                continue
            shutil.rmtree(entry, ignore_errors=True)
        except TimeoutError:
            raise
        except (OSError, ValueError, TypeError):
            continue


def create_root():
    parent = Path(tempfile.gettempdir())
    _sweep_stale_roots(parent)
    root = parent / ("mindie-summary-owner-" + uuid.uuid4().hex)
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    (root / OWNER_MARKER).write_text(
        json.dumps({"pid": os.getpid(), "created_at": time.time()}), encoding="utf-8",
        newline="\n")
    return root


def job_name(root):
    return "Local\\" + root.name if os.name == "nt" else None


def declaration(root):
    return json.dumps(dict(schema=1, owner=os.getpid(), path=str(root), job=job_name(root)))


def create_profile(*, prefix):
    """Return a private profile and whether the core owns its final deletion."""
    raw = os.environ.get(ENVIRONMENT_KEY)
    grouped = os.environ.get("MINDIE_MAINTENANCE_GROUP")
    owner = os.environ.get("MINDIE_MAINTENANCE_OWNER")
    if raw is None and grouped is None and owner is None:
        return tempfile.mkdtemp(prefix=prefix), False
    try:
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {"schema", "owner", "path", "job"}
                or type(value["schema"]) is not int or value["schema"] != 1
                or type(value["owner"]) is not int or value["owner"] <= 0
                or grouped != "1" or owner != str(value["owner"])
                or not isinstance(value["path"], str)):
            raise ValueError
        root = Path(value["path"])
        if os.name == "nt":
            from mindie_knowledge.windows_process import in_owned_job
            name = value["job"]
            if (not isinstance(name, str) or name != job_name(root)
                    or not name.startswith("Local\\mindie-summary-owner-")
                    or not in_owned_job(name)):
                raise ValueError
        elif (value["job"] is not None or os.getppid() != value["owner"]
              or os.getpgrp() != os.getpid()):
            raise ValueError
        info = root.lstat()
        if (not root.is_absolute() or not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError
    except TimeoutError:
        raise
    except (OSError, ValueError, TypeError):
        raise ScratchOwnershipError("summary scratch ownership is unavailable") from None
    return tempfile.mkdtemp(prefix=prefix, dir=root), True
