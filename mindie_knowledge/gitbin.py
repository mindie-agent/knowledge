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
from pathlib import Path
import shutil

ENV_NAME = "MINDIE_GIT_BIN"


class GitUnavailableError(FileNotFoundError):
    """No git executable is resolvable in this process environment."""


def _standard_locations():
    for variable, fallback in (
        ("ProgramFiles", r"C:\Program Files"),
        ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ):
        root = Path(os.environ.get(variable, fallback)) / "Git"
        yield root / "cmd" / "git.exe"
        yield root / "bin" / "git.exe"


def resolve_git():
    """The git executable this process may spawn, or an honest failure."""
    override = os.environ.get(ENV_NAME)
    if override:
        if not Path(override).is_file():
            raise GitUnavailableError(
                f"{ENV_NAME} does not name an existing file: {override}")
        return override
    found = shutil.which("git")
    if found:
        return found
    if os.name == "nt":
        for candidate in _standard_locations():
            if candidate.is_file():
                return str(candidate)
    raise GitUnavailableError(
        "git executable not found; set MINDIE_GIT_BIN to its absolute path"
        + (" or install Git for Windows" if os.name == "nt" else ""))


def ensure_git_on_path(env=None) -> dict[str, str] | None:
    """Prepend the resolved git executable's directory to PATH when not on PATH.

    Tools like ``gh`` shell out to ``git`` via ``PATH``; when Git was resolved
    through ``MINDIE_GIT_BIN`` or Windows standard locations rather than ambient
    ``PATH``, ``gh`` otherwise fails with ``unable to find git executable in PATH``.
    """
    try:
        git_bin = resolve_git()
    except GitUnavailableError:
        return {str(k): str(v) for k, v in env.items()} if env is not None else None
    git_dir = os.path.dirname(git_bin)
    ambient_path = (env or {}).get("PATH") if env and "PATH" in env else os.environ.get("PATH", "")
    parts = [part for part in ambient_path.split(os.pathsep) if part]
    if git_dir and git_dir not in parts:
        out = {str(k): str(v) for k, v in (env or {}).items()}
        out["PATH"] = git_dir + (os.pathsep + ambient_path if ambient_path else "")
        return out
    return {str(k): str(v) for k, v in env.items()} if env is not None else None
