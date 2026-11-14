"""Cross-platform native binary lookup and WSL2 DrvFS/PE shadow exclusion helpers.

[Role]
  Resolve native host executables (`git`, `gh`, `uv`, `ps`, `systemctl`,
  `systemd-run`, `busctl`) without selecting Windows PE/DrvFS binaries appended
  on WSL2 `$PATH` or swallowing `TimeoutError` inside filesystem probes.

[Invariants]
  * On POSIX, `which_native` resolves symlinks via `os.path.realpath(..., strict=True)`
    without pre-collapsing `..` segments before checking `/proc/self/mountinfo`
    and 4 KiB PE (`MZ..PE\\0\\0`) headers.
  * File existence probes use `os.stat` + `stat.S_ISREG` directly rather than
    `Path.is_file()` so `TimeoutError` always propagates.

[Timeouts & Locks]
  Bounded in-process `stat`, `mountinfo`, and 4 KiB header reads; no locks.

[Side-Effect Policy]
  Read-only over environment variables and filesystem metadata.
"""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

_WIN_SUFFIXES = (".exe", ".com", ".bat", ".cmd", ".ps1")
_MOUNTINFO_PATH = "/proc/self/mountinfo"


def is_regular_file(value: str | os.PathLike[str]) -> bool:
    try:
        return stat.S_ISREG(os.stat(value).st_mode)
    except TimeoutError:
        raise
    except OSError:
        return False


def _decode_mount_field(field: str) -> str:
    for seq, ch in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        field = field.replace(seq, ch)
    return field


def _strict_realpath(raw_text: str) -> str | None:
    probe = raw_text if os.path.isabs(raw_text) else os.path.join(os.getcwd(), raw_text)
    suffix: list[str] = []
    while True:
        try:
            resolved = os.path.realpath(probe, strict=True)
            return os.path.normpath(os.path.join(resolved, *reversed(suffix))) if suffix else resolved
        except TimeoutError:
            raise
        except (FileNotFoundError, NotADirectoryError):
            parent, name = os.path.split(probe.rstrip("/"))
            if not name or parent == probe:
                return None
            suffix.append(name)
            probe = parent
        except OSError:
            return None


def is_wsl_drvfs(value: str | os.PathLike[str]) -> bool:
    if os.name == "nt":
        return False
    try:
        raw_mountinfo = Path(_MOUNTINFO_PATH).read_text(encoding="utf-8", errors="replace")
    except TimeoutError:
        raise
    except OSError:
        return False
    raw_text = os.fspath(value)
    real = _strict_realpath(raw_text)
    if real is None:
        return True
    abs_target = os.path.abspath(raw_text)
    targets = [real] if real == abs_target else [real, abs_target]
    mounts: list[tuple[str, bool]] = []
    for line in raw_mountinfo.splitlines():
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        lfields = left.split()
        rfields = right.split()
        if len(lfields) < 5 or len(rfields) < 3:
            continue
        mp = _decode_mount_field(lfields[4])
        fstype, opts = rfields[0], rfields[2]
        on_drvfs = fstype == "drvfs" or (fstype == "9p" and "aname=drvfs" in opts)
        mounts.append((mp, on_drvfs))
    for target in targets:
        best_len = -1
        best_drvfs = False
        for mp, on_drvfs in mounts:
            prefix = "/" if mp == "/" else mp.rstrip("/")
            if target == prefix or (prefix == "/" and target.startswith("/")) or target.startswith(prefix + "/"):
                if len(prefix) > best_len:
                    best_len = len(prefix)
                    best_drvfs = on_drvfs
        if best_drvfs:
            return True
    return False


def is_pe_executable(value: str | os.PathLike[str]) -> bool:
    try:
        with Path(value).open("rb") as handle:
            head = handle.read(4096)
    except TimeoutError:
        raise
    except OSError:
        return False
    if len(head) < 64 or head[:2] != b"MZ":
        return False
    pe_offset = int.from_bytes(head[0x3C:0x40], "little", signed=False)
    return 0 <= pe_offset <= len(head) - 4 and head[pe_offset : pe_offset + 4] == b"PE\x00\x00"


def _is_exec_regular_file(path_str: str) -> bool:
    try:
        st = os.stat(path_str)
        return stat.S_ISREG(st.st_mode) and os.access(path_str, os.X_OK)
    except TimeoutError:
        raise
    except OSError:
        return False


def which_native(command: str, *, path: str | None = None) -> str | None:
    if not command:
        return None
    if os.name == "nt":
        return shutil.which(command, path=path)
    if "/" in command or (os.path.altsep and os.path.altsep in command):
        if _is_exec_regular_file(command) and not is_pe_executable(command) and not is_wsl_drvfs(command):
            return command
        return None
    if command.lower().endswith(_WIN_SUFFIXES):
        return None
    raw_path = os.environ.get("PATH", "") if path is None else path
    for entry in raw_path.split(os.pathsep):
        if not entry:
            continue
        candidate = os.path.join(entry, command)
        if _is_exec_regular_file(candidate):
            if is_pe_executable(candidate) or is_wsl_drvfs(candidate):
                continue
            return candidate
    return None


def resolve_uv(*, path: str | None = None) -> str | None:
    found = which_native("uv", path=path)
    if found:
        return found
    if path is not None:
        return None
    if os.name == "nt":
        candidates = []
        userprofile = os.environ.get("USERPROFILE")
        if userprofile:
            candidates.append(Path(userprofile) / ".local" / "bin" / "uv.exe")
            candidates.append(Path(userprofile) / ".cargo" / "bin" / "uv.exe")
        localapp = os.environ.get("LOCALAPPDATA")
        if localapp:
            candidates.append(Path(localapp) / "Microsoft" / "WinGet" / "Links" / "uv.exe")
        for cand in candidates:
            if is_regular_file(cand):
                return str(cand)
        return None
    home = Path.home()
    for cand in (home / ".local" / "bin" / "uv", home / ".cargo" / "bin" / "uv"):
        resolved = which_native(str(cand))
        if resolved:
            return resolved
    return None
