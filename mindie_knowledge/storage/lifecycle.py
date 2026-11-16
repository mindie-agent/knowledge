"""[Role] Two-phase retirement of disposable runtime generations.

[Invariants]
- A directory leaves the reusable namespace only through one atomic
  ``os.replace`` into a sibling ``.trash-*`` name. If that rename fails (a
  live Windows handle, a scanner, a race), the source directory is untouched:
  it is never deleted in place.
- On POSIX the rename holds non-blocking exclusive ``.inuse.lock`` and
  ``prepare.lock``/``.lock`` handles across eligibility re-verification and
  ``os.replace``; they are released only after the directory left the reusable
  namespace. On Windows no handle inside the directory may stay open (NTFS
  rejects the rename even when the holder is the same process), so one sibling
  ``.mindie-retire.lock`` is held instead and every lease/builder probe opens
  and closes its descriptor before the rename.
- A symlink/junction ``.trash-*`` parent or component-runtimes root is refused
  outright: the link target is foreign storage and is never scanned or swept.
- A verified generation is ``components.json``; a younger-than-grace directory
  without one is an in-flight staging build and is kept. An incomplete
  generation older than the staging grace is a failed build and is reclaimed,
  but only after the same lease/live/builder/grace checks as a verified excess.
- Leases, live process paths, ``prepare.lock`` holders, explicit keep paths,
  configured ``source_root``/``package_dir`` trees, and the current interpreter
  prefix always win over any retention count.
- Symlinks and NTFS directory junctions are checked with ``is_symlink`` and
  ``is_junction`` (reparse metadata on every supported Python) before any
  descent. Their target contents are never read or deleted; the link itself is
  unlinked or removed with ``os.rmdir``.
- Windows deletion uses extended ``\\\\?\\`` paths so a deep
  ``.venv\\Lib\\site-packages`` tree cannot fail on ``MAX_PATH``.
- ``UV_CACHE_DIR`` is only added when uv's effective default cache is on
  another device; an explicit caller value is never overwritten. A fallback
  ``.uv-cache`` is placed only on the target's own filesystem.

[Timeouts & Locks]
- ``deadline`` (absolute ``time.monotonic``) bounds every scan and deletion.
  Enumeration itself checks the deadline entry by entry; expiry stops
  immediately mid-directory, leaves the remainder under ``.trash-*`` for the
  next sweep, and is reported as ``partial``. A truncated prune enumeration
  retires nothing rather than ranking retention from a partial table. No lock
  is waited on; contention is a keep.

[Side-Effect Policy]
- Deletes only caller-named disposable generations and their own ``.trash-*``
  siblings, plus a same-volume ``.uv-cache`` the caller may opt into. Never a
  Git checkout, authoritative state, or an external installation.
"""
from __future__ import annotations

import ntpath
import os
from pathlib import Path
import stat
import sys
import time
import uuid

from ..filesystem import filesystem_path, is_junction, path_identity
from .leases import (
    BUILDER_LOCK_NAMES, acquire_retirement_locks, is_lease_held,
    is_path_used_by_live_process, windows_lease_present,
)
from .uv_cache import (
    UV_CACHE_DIRECTORY, _device, configure_same_volume_uv_cache,
    same_volume_cache_dir,
)

TRASH_PREFIX = ".trash-"
STAGING_GRACE_SECONDS = 1800.0
DEFAULT_PRUNE_BUDGET_SECONDS = 5.0
_SNAPSHOT_GONE = object()
_SNAPSHOT_UNREADABLE = object()
_SNAPSHOT_EXPIRED = object()


def fs_path(path: Path) -> Path:
    """Absolute extended Windows path; identity spelling everywhere else."""
    candidate = Path(path)
    if os.name != "nt":
        return candidate
    return filesystem_path(candidate)


def _expired(deadline) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _is_link(path: Path) -> bool:
    return path.is_symlink() or is_junction(path)


def _chmod_writable(path: Path) -> None:
    try:
        os.chmod(fs_path(path), stat.S_IWRITE | stat.S_IREAD)
    except TimeoutError:
        raise
    except OSError:
        pass


def _unlink(path: Path) -> None:
    try:
        os.unlink(fs_path(path))
    except FileNotFoundError:
        return
    except TimeoutError:
        raise
    except PermissionError:
        _chmod_writable(path)
        os.unlink(fs_path(path))


def _rmdir(path: Path) -> None:
    try:
        os.rmdir(fs_path(path))
    except FileNotFoundError:
        return
    except TimeoutError:
        raise
    except PermissionError:
        _chmod_writable(path)
        os.rmdir(fs_path(path))


def _remove_link(path: Path) -> None:
    """Remove a symlink/junction itself; never the target it references."""
    try:
        os.unlink(fs_path(path))
        return
    except FileNotFoundError:
        return
    except TimeoutError:
        raise
    except (IsADirectoryError, PermissionError):
        _rmdir(path)


def _snapshot(path: Path, deadline=None):
    """Names and non-followed directory flags for one bounded enumeration.

    Returns the item list, or ``_SNAPSHOT_GONE`` (the directory vanished),
    ``_SNAPSHOT_UNREADABLE`` (an OS error means its parent cannot be finished)
    and ``_SNAPSHOT_EXPIRED`` (the deadline stopped the enumeration; the
    remainder is resumable). The three cases must never collapse into the same
    value: only "gone" lets a caller report a completed deletion.
    """
    if _expired(deadline):
        return _SNAPSHOT_EXPIRED
    items = []
    try:
        with os.scandir(fs_path(path)) as iterator:
            for item in iterator:
                if _expired(deadline):
                    return _SNAPSHOT_EXPIRED
                try:
                    is_dir = item.is_dir(follow_symlinks=False)
                except TimeoutError:
                    raise
                except OSError:
                    is_dir = False
                items.append((item.name, is_dir))
    except FileNotFoundError:
        return _SNAPSHOT_GONE
    except TimeoutError:
        raise
    except OSError:
        return _SNAPSHOT_UNREADABLE
    return items


def _bounded_rmtree(root: Path, deadline) -> bool:
    """Delete ``root`` bottom-up without ever descending into a link.

    The iterative top-down ``scandir`` stack checks ``is_symlink`` and
    ``is_junction`` before pushing a child, so Windows NTFS junction
    traversal can never delete an external target. ``False`` means the
    deadline left a resumable remainder.
    """
    root = Path(root)
    if _is_link(root):
        _remove_link(root)
        return True
    if not root.is_dir():
        _unlink(root)
        return True
    if _expired(deadline):
        return False
    snapshot = _snapshot(root, deadline)
    if snapshot is _SNAPSHOT_EXPIRED:
        return False
    if snapshot is _SNAPSHOT_GONE:
        return True
    if snapshot is _SNAPSHOT_UNREADABLE:
        return False
    stack: list[list] = [[root, snapshot, 0]]
    while stack:
        frame = stack[-1]
        directory, entries, index = frame
        if index >= len(entries):
            stack.pop()
            if _expired(deadline):
                return False
            _rmdir(directory)
            continue
        name, is_dir = entries[index]
        frame[2] = index + 1
        if _expired(deadline):
            return False
        child = directory / name
        if _is_link(child):
            _remove_link(child)
        elif is_dir:
            nested = _snapshot(child, deadline)
            if nested is _SNAPSHOT_EXPIRED or nested is _SNAPSHOT_UNREADABLE:
                return False
            if nested is _SNAPSHOT_GONE:
                continue
            stack.append([child, nested, 0])
        else:
            _unlink(child)
    return True


def sweep_trash(parent: Path, *, deadline: float | None = None) -> dict:
    """Finish deleting leftover two-phase retirement directories."""
    parent = Path(parent)
    removed = 0
    remaining = 0
    partial = False
    warnings: list[str] = []
    if _is_link(parent):
        # A symlinked/junction root is a foreign target: never scan or delete
        # ``parent/.trash-*`` through the link.
        return {"removed": 0, "remaining": 0,
                "warnings": ["trash parent is a link: " + parent.name[:32]],
                "partial": False}
    if not parent.is_dir():
        return {"removed": 0, "remaining": 0, "warnings": warnings,
                "partial": False}
    entries: list[Path] = []
    try:
        with os.scandir(fs_path(parent)) as iterator:
            for item in iterator:
                if _expired(deadline):
                    return {"removed": 0, "remaining": 0,
                            "warnings": ["trash budget exhausted"],
                            "partial": True}
                entries.append(parent / item.name)
    except TimeoutError:
        raise
    except OSError as exc:
        return {"removed": 0, "remaining": 0,
                "warnings": ["trash scan: " + type(exc).__name__],
                "partial": True}
    entries.sort(key=lambda item: item.name)
    for entry in entries:
        if not entry.name.startswith(TRASH_PREFIX):
            continue
        if _expired(deadline):
            remaining += 1
            partial = True
            warnings.append("trash budget exhausted")
            continue
        try:
            if _is_link(entry):
                _remove_link(entry)
                removed += 1
                continue
            if not entry.is_dir():
                _unlink(entry)
                removed += 1
                continue
            if _bounded_rmtree(entry, deadline):
                removed += 1
            else:
                remaining += 1
                partial = True
                warnings.append("trash deadline: " + entry.name[:32])
        except TimeoutError:
            raise
        except OSError as exc:
            remaining += 1
            warnings.append("trash deferred: " + type(exc).__name__)
    return {"removed": removed, "remaining": remaining,
            "warnings": warnings, "partial": partial}


def _trash_name(entry: Path) -> str:
    return TRASH_PREFIX + entry.name[:16] + "-" + uuid.uuid4().hex[:8]


def _same_device(first: Path, second: Path) -> bool:
    left, right = _device(first), _device(second)
    return left is not None and left == right


def retire_directory(entry: Path, *, trash_root: Path | None = None,
                     deadline: float | None = None,
                     recheck=None) -> dict:
    """Atomically rename ``entry`` out of use, then remove the trash copy.

    The rename is the liveness gate: on NTFS a running ``python.exe`` from the
    directory (or any open handle without ``FILE_SHARE_DELETE``) makes it fail
    with ``PermissionError`` before a single file inside is touched.

    POSIX holds the exclusive lease/builder locks inside ``entry`` across the
    rename. Windows must not: NTFS rejects the rename while *any* handle inside
    the directory is open, even this process's own. There the caller holds one
    sibling ``.mindie-retire.lock`` and probes every lease/builder lock with
    descriptors that are closed before ``os.replace``.

    ``recheck`` runs while the locks are held, directly before ``os.replace``.
    A false result leaves the directory untouched.
    """
    entry = Path(entry)
    result = {"status": "absent", "path": str(entry), "reclaimed": False,
              "partial": False}
    if not os.path.lexists(fs_path(entry)):
        return result
    parent = entry.parent
    if trash_root is not None:
        candidate = Path(trash_root)
        if candidate.is_dir() and _same_device(entry, candidate):
            parent = candidate
    try:
        parent.mkdir(parents=True, exist_ok=True)
        trash = parent / _trash_name(entry)
        while os.path.lexists(fs_path(trash)):  # Never overwrite a sibling.
            trash = parent / _trash_name(entry)
    except TimeoutError:
        raise
    except OSError as exc:
        result.update(status="deferred_in_use", error=type(exc).__name__)
        return result
    if _is_link(entry):
        # A link/junction is renamed as a link; no lock file may be created in
        # the external target and the target is never traversed.
        return _retire_link(entry, trash, result, deadline)
    locks = acquire_retirement_locks(entry)
    if locks is None:
        result.update(status="deferred_in_use", error="LockContention")
        return result
    try:
        if locks.sibling_only:
            # Windows: every probe opens and closes a descriptor inside
            # ``entry``; a single surviving handle would block the rename.
            if (is_lease_held(entry) or windows_lease_present(entry)
                    or _builder_locked(entry)):
                result.update(status="deferred_in_use", error="LeaseHeld")
                return result
        elif windows_lease_present(entry):
            result.update(status="deferred_in_use", error="LeaseHeld")
            return result
        if recheck is not None and not recheck():
            result.update(status="deferred_in_use", error="EligibilityChanged")
            return result
        if not os.path.lexists(fs_path(entry)):
            return result
        if locks.sibling_only and locks.descriptors_inside_entry:
            # Invariant guard: a leaked entry descriptor would make the rename
            # fail after touching nothing; refuse instead of looping forever.
            result.update(status="deferred_in_use", error="LockInsideEntry")
            return result
        try:
            os.replace(fs_path(entry), fs_path(trash))
        except TimeoutError:
            raise
        except FileNotFoundError:
            return result
        except (PermissionError, OSError) as exc:
            # A failed atomic rename is proof that the tree may be in use. The
            # original directory is deliberately left exactly as it was.
            result.update(status="deferred_in_use", error=type(exc).__name__)
            return result
    finally:
        # The rename has already left the reusable namespace; only now may a
        # cooperating builder acquire a lease in the next generation.
        locks.release()
    result.update(status="retired", trash=str(trash), reclaimed=True)
    return _finish_trash(trash, result, deadline)


def _retire_link(entry: Path, trash: Path, result: dict, deadline) -> dict:
    try:
        os.replace(fs_path(entry), fs_path(trash))
    except TimeoutError:
        raise
    except FileNotFoundError:
        return result
    except (PermissionError, OSError) as exc:
        result.update(status="deferred_in_use", error=type(exc).__name__)
        return result
    result.update(status="retired", trash=str(trash), reclaimed=True)
    try:
        _remove_link(trash)
        result["trash_removed"] = True
    except TimeoutError:
        raise
    except OSError as exc:
        result.update(status="trashed_partial", trash_removed=False,
                      partial=True, error=type(exc).__name__)
    return result


def _finish_trash(trash: Path, result: dict, deadline) -> dict:
    try:
        if _bounded_rmtree(trash, deadline):
            result["trash_removed"] = True
        else:
            result["status"] = "trashed_partial"
            result["trash_removed"] = False
            result["partial"] = True
    except TimeoutError:
        raise
    except OSError as exc:
        result["status"] = "trashed_partial"
        result["trash_removed"] = False
        result["partial"] = True
        result["error"] = type(exc).__name__
    return result


def _identity(value) -> str:
    raw = os.fspath(value)
    try:
        return path_identity(raw)
    except TimeoutError:
        raise
    except (OSError, RuntimeError):
        text = os.path.normpath(raw)
        return ntpath.normcase(text) if os.name == "nt" else text


def _normalized_spellings(value) -> set[str]:
    raw = os.fspath(value)
    spellings = {os.path.abspath(raw), os.path.realpath(raw), _identity(raw)}
    if os.name == "nt":
        spellings = {ntpath.normcase(ntpath.normpath(item)) for item in spellings}
    return {os.path.normpath(item) for item in spellings}


def _within(child: str, parent: str) -> bool:
    if child == parent:
        return True
    separator = "\\" if os.name == "nt" else "/"
    prefix = parent if parent.endswith(separator) else parent + separator
    return child.startswith(prefix)


def is_protected_path(entry: Path, keep_paths) -> bool:
    entry_keys = _normalized_spellings(entry)
    for keep in keep_paths:
        for keep_key in _normalized_spellings(keep):
            for entry_key in entry_keys:
                # Keep the generation when it or a child such as source/ or
                # runtime/.venv is explicitly referenced by a host pin.
                if keep_key == entry_key or _within(keep_key, entry_key):
                    return True
    return False


def is_within_roots(entry: Path, roots) -> bool:
    """True when ``entry`` is one of ``roots`` or lies inside one of them."""
    if not roots:
        return False
    entry_keys = _normalized_spellings(entry)
    for root in roots:
        for root_key in _normalized_spellings(root):
            for entry_key in entry_keys:
                if root_key == entry_key or _within(entry_key, root_key):
                    return True
    return False


def is_own_runtime_path(entry: Path) -> bool:
    """True when ``entry`` contains (or is) the running interpreter/prefix."""
    entry_keys = _normalized_spellings(entry)
    for protected in (sys.executable, sys.prefix,
                      getattr(sys, "base_prefix", sys.prefix)):
        for key in _normalized_spellings(protected):
            if any(key == candidate or _within(key, candidate)
                   for candidate in entry_keys):
                return True
    return False


def _lock_held(entry: Path, name: str) -> bool:
    lock = Path(entry) / name
    try:
        if not os.path.lexists(fs_path(lock)):
            return False  # No builder ever created this lock: unlocked.
    except TimeoutError:
        raise
    except OSError:
        return True
    from ..loop.locks import lock_held

    return lock_held(lock) is not False  # Unknown inspection stays a keep.


def _builder_locked(entry: Path) -> bool:
    return any(_lock_held(entry, name) for name in BUILDER_LOCK_NAMES)


def _within_grace(entry: Path, grace_seconds: float) -> bool:
    if grace_seconds <= 0:
        return False
    try:
        mtime = os.stat(fs_path(entry)).st_mtime
    except TimeoutError:
        raise
    except OSError:
        return True
    return time.time() - mtime < grace_seconds


def has_git_ancestor(entry: Path) -> bool:
    """True when ``entry`` or any ancestor is a Git checkout."""
    probe = Path(os.path.abspath(os.fspath(entry)))
    while True:
        try:
            if (probe / ".git").exists():
                return True
        except TimeoutError:
            raise
        except OSError:
            return True  # Unknown inspection is a keep.
        if probe.parent == probe:
            return False
        probe = probe.parent


def _retire(entry: Path, deadline, accounting: dict, *, keep_paths, live_paths,
            grace_seconds: float = STAGING_GRACE_SECONDS,
            forbidden_roots=()) -> None:
    entry = Path(entry)
    linked = _is_link(entry)
    if (is_protected_path(entry, keep_paths) or is_own_runtime_path(entry)
            or is_within_roots(entry, forbidden_roots)
            or ".git" in entry.parts or has_git_ancestor(entry)):
        accounting["kept_dirs"] += 1
        return
    if is_path_used_by_live_process(entry, live_paths):
        accounting["kept_dirs"] += 1
        return
    if not linked and (is_lease_held(entry) or _builder_locked(entry)):
        accounting["kept_dirs"] += 1
        return
    if not linked and _within_grace(entry, grace_seconds):
        accounting["kept_dirs"] += 1
        return
    if _expired(deadline):
        accounting["warnings"].append("budget exhausted")
        accounting["deferred_dirs"] += 1
        accounting["partial"] = True
        return
    outcome = retire_directory(
        entry, deadline=deadline,
        recheck=(None if linked else
                 (lambda: not is_protected_path(entry, keep_paths)
                  and not is_within_roots(entry, forbidden_roots)
                  and not is_own_runtime_path(entry)
                  and ".git" not in entry.parts
                  and not has_git_ancestor(entry)
                  and not is_path_used_by_live_process(entry, live_paths))))
    if outcome["status"] in {"retired", "trashed_partial"}:
        accounting["reclaimed_dirs"] += 1
    elif outcome["status"] == "deferred_in_use":
        accounting["deferred_dirs"] += 1
    elif outcome.get("error"):
        accounting["warnings"].append(
            "retire " + entry.name[:32] + ": " + outcome["error"])
    if outcome.get("partial"):
        accounting["partial"] = True


def prune_component_runtimes(root: Path, *, keep_paths=(),
                             keep_recent_verified: int = 2,
                             staging_grace_seconds: float = STAGING_GRACE_SECONDS,
                             budget_seconds: float = DEFAULT_PRUNE_BUDGET_SECONDS,
                             live_paths: set[str] | None = None,
                             inspection_complete: bool | None = None,
                             include_global_roots: bool = True,
                             forbidden_roots=(),
                             deadline: float | None = None) -> dict:
    """Sweep trash, then reclaim failed and obsolete component generations.

    With ``live_paths=None`` the process table is inspected here instead of
    assuming an empty (and therefore complete) snapshot. Caller keep paths are
    unioned with every global host binding unless ``include_global_roots`` is
    explicitly disabled for an isolated unit test. Global discovery also adds
    every configured ``source_root``/``package_dir`` as a forbidden ancestor
    root: a candidate *inside* an exported source tree is never retired, even
    when that tree has no ``.git`` and contains no keep pin.
    """
    root = Path(root)
    started = time.monotonic()
    limit = started + max(0.0, float(budget_seconds))
    if deadline is not None:
        limit = min(limit, deadline)
    accounting = {
        "status": "completed", "root": str(root), "reclaimed_dirs": 0,
        "deferred_dirs": 0, "kept_dirs": 0, "reclaimed_trash": 0,
        "warnings": [], "inspection_complete": bool(inspection_complete),
        "partial": False,
    }
    if live_paths is None:
        from .leases import live_process_prefixes

        live_paths, inspected = live_process_prefixes()
        if inspection_complete is None or inspection_complete is True:
            # An auto-inspected snapshot is the authority; only an explicit
            # conservative False may keep verified generations untouched.
            inspection_complete = inspected
    elif inspection_complete is None:
        inspection_complete = True
    accounting["inspection_complete"] = bool(inspection_complete)
    if _is_link(root):
        # The configured runtimes root is itself a symlink/junction: its target
        # is foreign storage and must never be scanned or swept through it.
        accounting["status"] = "refused_link"
        accounting["warnings"].append("component root is a link: " + root.name[:32])
        return accounting
    if not root.is_dir():
        accounting["status"] = "absent"
        return accounting
    trash = sweep_trash(root, deadline=limit)
    accounting["reclaimed_trash"] = trash["removed"]
    accounting["warnings"].extend(trash["warnings"])
    accounting["partial"] = accounting["partial"] or bool(trash.get("partial"))
    keep = list(keep_paths or ())
    forbidden = list(forbidden_roots or ())
    if include_global_roots:
        from .legacy_roots import (
            collect_global_protected_roots, protected_source_roots,
        )

        try:
            keep.extend(collect_global_protected_roots(deadline=limit))
            forbidden.extend(protected_source_roots(deadline=limit))
        except TimeoutError:
            accounting["status"] = "partial"
            accounting["partial"] = True
            accounting["inspection_complete"] = False
            accounting["warnings"].append("protected roots budget exhausted")
            return accounting
    if sys.prefix not in keep:
        keep.append(sys.prefix)
    if sys.executable not in keep:
        keep.append(sys.executable)
    entries: list[Path] = []
    try:
        with os.scandir(fs_path(root)) as iterator:
            for item in iterator:
                if _expired(limit):
                    # A partial enumeration cannot rank retained generations;
                    # retire nothing rather than guess from a truncated table.
                    accounting["status"] = "partial"
                    accounting["partial"] = True
                    accounting["warnings"].append("budget exhausted")
                    return accounting
                entry = root / item.name
                if item.is_dir() or _is_link(entry):
                    entries.append(entry)
    except TimeoutError:
        raise
    except OSError as exc:
        accounting["status"] = "unreadable"
        accounting["warnings"].append("scan: " + type(exc).__name__)
        return accounting
    entries.sort(key=lambda item: item.name)
    verified: list[tuple[float, float, Path]] = []
    now = time.time()
    for entry in entries:
        if entry.name.startswith(TRASH_PREFIX):
            continue
        if _expired(limit):
            accounting["status"] = "partial"
            accounting["partial"] = True
            accounting["warnings"].append("budget exhausted")
            break
        linked = _is_link(entry)
        if linked:
            _retire(entry, limit, accounting, keep_paths=keep,
                    live_paths=live_paths, grace_seconds=staging_grace_seconds,
                    forbidden_roots=forbidden)
            continue
        if is_protected_path(entry, keep) or is_path_used_by_live_process(entry, live_paths) \
                or is_lease_held(entry) or _builder_locked(entry) \
                or is_within_roots(entry, forbidden):
            accounting["kept_dirs"] += 1
            continue
        manifest = entry / "components.json"
        try:
            manifest_info = os.stat(fs_path(manifest))
        except TimeoutError:
            raise
        except OSError:
            manifest_info = None
        if manifest_info is None:
            try:
                age = now - os.stat(fs_path(entry)).st_mtime
            except TimeoutError:
                raise
            except OSError:
                accounting["kept_dirs"] += 1
                continue
            if age < staging_grace_seconds:
                accounting["kept_dirs"] += 1
            else:
                _retire(entry, limit, accounting, keep_paths=keep,
                        live_paths=live_paths,
                        grace_seconds=staging_grace_seconds,
                        forbidden_roots=forbidden)
            continue
        try:
            entry_time = os.stat(fs_path(entry)).st_mtime
        except TimeoutError:
            raise
        except OSError:
            entry_time = 0.0
        verified.append((manifest_info.st_mtime, entry_time, entry))
    verified.sort(key=lambda row: (row[0], row[1], row[2].name), reverse=True)
    retained = {id(row[2]) for row in verified[:max(0, int(keep_recent_verified))]}
    if not inspection_complete:
        accounting["status"] = "inspection_incomplete"
        accounting["kept_dirs"] += len([row for row in verified if id(row[2]) not in retained])
        return accounting
    for _manifest_time, _entry_time, entry in verified:
        if id(entry) in retained:
            accounting["kept_dirs"] += 1
            continue
        if _expired(limit):
            accounting["status"] = "partial"
            accounting["partial"] = True
            accounting["warnings"].append("budget exhausted")
            break
        _retire(entry, limit, accounting, keep_paths=keep,
                live_paths=live_paths, grace_seconds=staging_grace_seconds,
                forbidden_roots=forbidden)
    return accounting


__all__ = [
    "BUILDER_LOCK_NAMES",
    "DEFAULT_PRUNE_BUDGET_SECONDS",
    "STAGING_GRACE_SECONDS",
    "TRASH_PREFIX",
    "UV_CACHE_DIRECTORY",
    "configure_same_volume_uv_cache",
    "fs_path",
    "is_junction",
    "is_own_runtime_path",
    "is_protected_path",
    "is_within_roots",
    "prune_component_runtimes",
    "retire_directory",
    "same_volume_cache_dir",
    "sweep_trash",
]
