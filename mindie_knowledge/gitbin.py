r"""[Role] Deterministic resolution of the git executable and PATH propagation for spawns.

[Invariants] Resolution order is fixed and never guesses: (1) explicit
``MINDIE_GIT_BIN`` pin (failing visibly if the path does not exist), (2) ambient
``shutil.which("git")``, (3) Windows standard Git-for-Windows directories
(``%ProgramFiles%`` / ``%ProgramFiles(x86)%`` ``\Git\cmd\git.exe`` and
``\Git\bin\git.exe``). When resolved outside ambient ``PATH``,
``ensure_git_on_path`` prepends the resolved directory so child tools like
``gh`` never fail with ``unable to find git executable in PATH``.
[Timeouts & Locks] Pure in-memory/filesystem metadata checks; zero subprocesses.
[Side-Effect Policy] Read-only over environment variables and filesystem metadata.
"""

import os
from os.path import dirname
from pathlib import Path
import shutil

from ._host_exec import is_regular_file, which_native

ENV_NAME = "MINDIE_GIT_BIN"
_ORIG_WHICH = shutil.which


class GitUnavailableError(FileNotFoundError):
    """No git executable is resolvable in this process environment."""


def _standard_locations():
    localapp = os.environ.get("LOCALAPPDATA")
    if localapp:
        user_git = Path(localapp) / "Programs" / "Git"
        yield user_git / "cmd" / "git.exe"
        yield user_git / "bin" / "git.exe"
    for variable, fallback in (
        ("ProgramFiles", r"C:\Program Files"),
        ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ):
        root = Path(os.environ.get(variable, fallback)) / "Git"
        yield root / "cmd" / "git.exe"
        yield root / "bin" / "git.exe"


def resolve_git(env=None):
    """The git executable this process may spawn, or an honest failure."""
    source_env = os.environ if env is None else env
    override = source_env.get(ENV_NAME) or os.environ.get(ENV_NAME)
    if override:
        if not is_regular_file(override):
            raise GitUnavailableError(
                f"{ENV_NAME} does not name an existing file: {override}")
        return override
    raw_path = source_env["PATH"] if "PATH" in source_env else os.environ.get("PATH")
    if os.name == "nt" or shutil.which is not _ORIG_WHICH:
        found = shutil.which("git") if env is None else shutil.which("git", path=raw_path)
    else:
        found = which_native("git", path=raw_path)
    if found:
        return found
    if os.name == "nt":
        for candidate in _standard_locations():
            if is_regular_file(candidate):
                return str(candidate)
    raise GitUnavailableError(
        "git executable not found; set MINDIE_GIT_BIN to its absolute path"
        + (" or install Git for Windows" if os.name == "nt" else ""))


def ensure_git_on_path(env=None) -> dict[str, str] | None:
    """Prepend the resolved git executable's directory ahead of missing/shadowed PATH entries.

    Tools like ``gh`` shell out to ``git`` via ``PATH``; when Git was resolved
    through ``MINDIE_GIT_BIN`` / Windows standard locations or behind a rejected
    WSL2 DrvFS/PE ``git`` entry on ``PATH``, child lookup must select ``git_bin``.
    """
    try:
        git_bin = resolve_git(env)
    except GitUnavailableError:
        return {str(k): str(v) for k, v in env.items()} if env is not None else None
    git_dir = dirname(git_bin)
    if not git_dir:
        return {str(k): str(v) for k, v in env.items()} if env is not None else None
    pathsep = getattr(os, "pathsep", ":")
    has_env_path = bool(env is not None and "PATH" in env)
    raw_path = env["PATH"] if has_env_path else os.environ.get("PATH")
    effective_path = getattr(os, "defpath", "") if raw_path is None else raw_path
    parts = [part for part in effective_path.split(pathsep) if part]
    normcase = getattr(os.path, "normcase", lambda s: s)
    normpath = getattr(os.path, "normpath", lambda s: s)
    ambient_git = _ORIG_WHICH("git", path=effective_path) if effective_path else None
    same_git = bool(ambient_git and normcase(normpath(ambient_git)) == normcase(normpath(git_bin)))
    if not same_git:
        norm_git_dir = normcase(normpath(git_dir))
        reordered = [git_dir, *[p for p in parts if normcase(normpath(p)) != norm_git_dir]]
        out = {str(k): str(v) for k, v in (env or {}).items()}
        out["PATH"] = pathsep.join(reordered)
        return out
    return {str(k): str(v) for k, v in env.items()} if env is not None else None
