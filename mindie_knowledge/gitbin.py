r"""One deterministic resolution of the git executable for every spawn.

Reduced-PATH environments (Windows Task Scheduler Session 0, minimal
containers) do not carry Git on PATH, so a bare ``"git"`` argv[0] fails at
spawn time. Resolution order is fixed and never guesses:

1. ``MINDIE_GIT_BIN`` — an explicit absolute-path pin. Adapters that verify
   their own Git propagate it to service/spawn environments under this name.
   A pin that does not name an existing file fails honestly, naming the
   variable — it is never silently ignored in favor of another source.
2. ``shutil.which("git")`` — the ambient PATH, identical to today.
3. Windows only: the standard Git-for-Windows installation directories
   (``%ProgramFiles%`` / ``%ProgramFiles(x86)%`` ``\Git\cmd\git.exe``).

Anything else raises :class:`GitUnavailableError` with the remedy; no
candidate is ever fabricated.
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
        yield Path(os.environ.get(variable, fallback)) / "Git" / "cmd" / "git.exe"


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
