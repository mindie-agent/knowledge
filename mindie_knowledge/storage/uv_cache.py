"""[Role] Same-volume uv cache placement for disposable runtime generations.

[Invariants]
- A caller-supplied non-empty ``UV_CACHE_DIR`` is never overwritten; only uv's
  effective platform default is considered for relocation.
- A fallback ``.uv-cache`` is placed on the target's own filesystem (or inside a
  mountpoint root, whose parent is a different device), so uv can clone or
  hardlink instead of copying every generation.
- Every path inspection here is read-only and side-effect free.

[Timeouts & Locks]
- Helpers are arithmetic plus ``os.stat``; no lock is acquired and a filesystem
  ``TimeoutError`` always propagates. No subprocess, network or model call.

[Side-Effect Policy]
- Reads the caller's ``env`` mapping and returns it; never creates a directory,
  spawns a process or writes a file.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

UV_CACHE_DIRECTORY = ".uv-cache"


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
            os.stat(probe)
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
        return os.stat(ancestor).st_dev
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
    "UV_CACHE_DIRECTORY",
    "configure_same_volume_uv_cache",
    "same_volume_cache_dir",
]
