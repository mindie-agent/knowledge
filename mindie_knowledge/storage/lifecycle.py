"""[Role] Two-phase retirement of disposable runtime generations.

[Invariants]
- A directory leaves the reusable namespace only through one atomic
  ``os.replace`` into a sibling ``.trash-*`` name. If that rename fails (a
  live Windows handle, a scanner, a race), the source directory is untouched:
  it is never deleted in place.
- The rename holds non-blocking exclusive ``.inuse.lock`` and
  ``prepare.lock``/``.lock`` handles across eligibility re-verification and
  ``os.replace``; they are released only after the directory left the
  reusable namespace.
- A verified generation is ``components.json``; a younger-than-grace directory
  without one is an in-flight staging build and is kept. An incomplete
  generation older than the staging grace is a failed build and is reclaimed,
  but only after the same lease/live/builder/grace checks as a verified excess.
- Leases, live process paths, ``prepare.lock`` holders, explicit keep paths,
  and the current interpreter prefix always win over any retention count.
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
  Expiry stops immediately mid-directory, leaves the remainder under
  ``.trash-*`` for the next sweep, and is reported as ``partial``. No lock is
  waited on; contention is a keep.

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

TRASH_PREFIX = ".trash-"
STAGING_GRACE_SECONDS = 1800.0
DEFAULT_PRUNE_BUDGET_SECONDS = 5.0
UV_CACHE_DIRECTORY = ".uv-cache"


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


def _snapshot(path: Path):
    """Names and non-followed directory flags; ``None`` when unreadable."""
    items = []
    try:
        with os.scandir(fs_path(path)) as iterator:
            for item in iterator:
                try:
                    is_dir = item.is_dir(follow_symlinks=False)
                except TimeoutError:
                    raise
                except OSError:
                    is_dir = False
                items.append((item.name, is_dir))
    except FileNotFoundError:
        return None
    except TimeoutError:
        raise
    except OSError:
        return None
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
    children = _snapshot(root)
    if children is None:
        return True
    stack: list[list] = [[root, children, 0]]
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
            nested = _snapshot(child)
            if nested is None:
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
    if not parent.is_dir():
        return {"removed": 0, "remaining": 0, "warnings": warnings,
                "partial": False}
    try:
        entries = sorted(parent.iterdir(), key=lambda item: item.name)
    except TimeoutError:
        raise
    except OSError as exc:
        return {"removed": 0, "remaining": 0,
                "warnings": ["trash scan: " + type(exc).__name__],
                "partial": True}
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

    ``recheck`` runs while the exclusive lease/builder locks are held, directly
    before ``os.replace``. A false result leaves the directory untouched.
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
        if windows_lease_present(entry):
            result.update(status="deferred_in_use", error="LeaseHeld")
            return result
        if recheck is not None and not recheck():
            result.update(status="deferred_in_use", error="EligibilityChanged")
            return result
        if not os.path.lexists(fs_path(entry)):
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
    explicitly disabled for an isolated unit test.
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
    if not root.is_dir():
        accounting["status"] = "absent"
        return accounting
    trash = sweep_trash(root, deadline=limit)
    accounting["reclaimed_trash"] = trash["removed"]
    accounting["warnings"].extend(trash["warnings"])
    accounting["partial"] = accounting["partial"] or bool(trash.get("partial"))
    keep = list(keep_paths or ())
    if include_global_roots:
        from .legacy_roots import collect_global_protected_roots

        try:
            keep.extend(collect_global_protected_roots(deadline=limit))
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
    try:
        entries = sorted((item for item in root.iterdir()
                          if item.is_dir() or _is_link(item)),
                         key=lambda item: item.name)
    except TimeoutError:
        raise
    except OSError as exc:
        accounting["status"] = "unreadable"
        accounting["warnings"].append("scan: " + type(exc).__name__)
        return accounting
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
                    forbidden_roots=forbidden_roots)
            continue
        if is_protected_path(entry, keep) or is_path_used_by_live_process(entry, live_paths) \
                or is_lease_held(entry) or _builder_locked(entry) \
                or is_within_roots(entry, forbidden_roots):
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
                        forbidden_roots=forbidden_roots)
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
                forbidden_roots=forbidden_roots)
    return accounting


def _uv_default_cache(env=None) -> Path:
    env = env if isinstance(env, dict) else {}
    xdg = env.get("XDG_CACHE_HOME")
    if isinstance(xdg, str) and xdg.strip():
        return Path(xdg) / "uv"
    if os.name == "nt":
        base = env.get("LOCALAPPDATA") or os.environ.get("LOCALAPPDATA") \
            or str(Path.home() / "AppData" / "Local")
        return Path(base) / "uv" / "cache"
    home = Path.home()
    if sys.platform == "darwin":
        legacy = home / ".cache" / "uv"
        try:
            if legacy.exists() or legacy.is_symlink():
                return legacy
        except TimeoutError:
            raise
        except OSError:
            pass
        return home / "Library" / "Caches" / "uv"
    return home / ".cache" / "uv"


def _existing_ancestor(path: Path) -> Path | None:
    probe = Path(path)
    while True:
        try:
            os.stat(fs_path(probe))
            return probe
        except FileNotFoundError:
            if probe.parent == probe:
                return None
            probe = probe.parent
        except TimeoutError:
            raise
        except OSError:
            return None


def _device(path: Path) -> int | None:
    ancestor = _existing_ancestor(path)
    if ancestor is None:
        return None
    try:
        return os.stat(fs_path(ancestor)).st_dev
    except TimeoutError:
        raise
    except OSError:
        return None


def _same_filesystem(first: Path, second: Path) -> bool:
    left, right = _device(first), _device(second)
    return left is not None and left == right


def same_volume_cache_dir(target_dir: Path) -> Path:
    """A uv cache on the target's own device (clone/hardlink safe).

    When ``target_dir`` is itself a mountpoint root, its parent is a different
    device, so the cache is placed inside the target instead of outside it.
    """
    target = Path(target_dir)
    try:
        resolved = target.resolve()
    except TimeoutError:
        raise
    except OSError:
        resolved = target.absolute()
    for candidate in (resolved.parent / UV_CACHE_DIRECTORY,
                      resolved / UV_CACHE_DIRECTORY):
        if _same_filesystem(candidate, resolved):
            return candidate
    return resolved.parent / UV_CACHE_DIRECTORY


def configure_same_volume_uv_cache(target_dir: Path, env: dict[str, str]) -> dict[str, str]:
    """Pin uv's cache to the target device when the default would cross it.

    A cross-device cache turns ``clonefile``/hardlinks into a full copy per
    generation. An explicit non-empty ``UV_CACHE_DIR`` is the caller's choice
    and is left byte-for-byte unchanged. Default resolution follows uv's
    precedence using the passed child ``env``: ``XDG_CACHE_HOME`` first, then
    uv's platform default (both ``~/.cache/uv`` and ``~/Library/Caches/uv`` are
    considered on macOS).
    """
    if not isinstance(env, dict):
        return env
    explicit = env.get("UV_CACHE_DIR")
    if isinstance(explicit, str) and explicit.strip():
        return env
    cache = _uv_default_cache(env)
    target_device = _device(Path(target_dir))
    if target_device is None:
        return env
    cache_devices = {_device(cache)}
    try:
        real_cache = cache.resolve()
    except TimeoutError:
        raise
    except OSError:
        real_cache = None
    if real_cache is not None:
        cache_devices.add(_device(real_cache))
    if target_device in cache_devices:
        return env
    chosen = same_volume_cache_dir(Path(target_dir))
    if not _same_filesystem(chosen, Path(target_dir)):
        return env
    env["UV_CACHE_DIR"] = str(chosen)
    return env


__all__ = [
    "BUILDER_LOCK_NAMES",
    "DEFAULT_PRUNE_BUDGET_SECONDS",
    "STAGING_GRACE_SECONDS",
    "TRASH_PREFIX",
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
