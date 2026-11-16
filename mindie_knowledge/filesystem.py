"""Native Windows paths without changing the physical state location.

[Role] Native Windows path spelling while keeping physical state locations stable.
[Invariants] Extended paths preserve the same identity as their logical spelling; comparisons resolve aliases before checking components. NTFS reparse points are detected from ``lstat`` metadata, so Python 3.11 does not silently miss a junction that only 3.12+ ``os.path.isjunction`` would report.
[Timeouts & Locks] Path helpers stay arithmetic; ``is_junction`` performs one ``lstat`` and always propagates ``TimeoutError``.
[Side-Effect Policy] Returns path/URI values only; never creates, renames or deletes files.
"""

import ntpath
import os
from pathlib import Path
import stat
from urllib.parse import quote


def _windows_extended(path):
    logical = _windows_logical(path)
    path = ntpath.normpath(logical if logical != path else path)
    if path.startswith("\\\\?\\"):
        return path.replace("/", "\\")
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def _windows_logical(path):
    if path[:8].casefold() == "\\\\?\\unc\\":
        return "\\\\" + path[8:]
    if (
        path.startswith("\\\\?\\")
        and len(path) >= 7
        and path[4].isascii()
        and path[4].isalpha()
        and path[5] == ":"
        and path[6] in ("\\", "/")
    ):
        return path[4:]
    return path


def filesystem_path(path):
    """Give Python an absolute extended path on Windows, before any I/O."""
    if os.name == "nt":
        return Path(_windows_extended(os.path.abspath(path)))
    return Path(path)


def is_junction(path) -> bool:
    """NTFS reparse-point detection that also works on Python 3.11.

    ``os.path.isjunction`` was added in Python 3.12. Windows ``lstat`` already
    exposes the reparse tag and file attributes on every supported interpreter,
    so a junction is recognised without that helper. Symlinks are excluded
    because they are handled separately by ``is_symlink()``.
    """
    try:
        info = os.lstat(filesystem_path(path))
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return False
    if stat.S_ISLNK(info.st_mode):
        return False
    reparse_tag = getattr(info, "st_reparse_tag", 0)
    attributes = getattr(info, "st_file_attributes", 0)
    if reparse_tag or attributes & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        return True
    checker = getattr(os.path, "isjunction", None)
    if checker is None:
        return False
    try:
        return bool(checker(filesystem_path(path)))
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return False


def resolved_path(path, *, strict=False):
    """Resolve filesystem aliases while retaining a Windows-safe I/O spelling."""
    return filesystem_path(Path(path).expanduser()).resolve(strict=strict)


def path_identity(path):
    """Canonical path components for comparison, never a substitute I/O path."""
    value = str(resolved_path(path))
    return ntpath.normcase(_windows_logical(value)) if os.name == "nt" else value


def path_is_within(path, root):
    """Resolve junctions/symlinks before comparing components, not prefixes."""
    return Path(path_identity(path)).is_relative_to(Path(path_identity(root)))


def path_aliases(path):
    """Bounded spellings used by earlier path-keyed cursor versions."""
    resolved = str(resolved_path(path))
    if os.name != "nt":
        return (resolved,)
    logical = _windows_logical(resolved)
    return tuple(dict.fromkeys((ntpath.normcase(logical), logical,
                                _windows_extended(logical),
                                ntpath.normcase(_windows_extended(logical)))))


def git_path(path):
    """Git for Windows needs ordinary paths, with its own core.longpaths."""
    value = os.fspath(path)
    return _windows_logical(value) if os.name == "nt" else value


def sqlite_file_uri(path):
    """Keep extended paths intact and escape URI syntax in native filenames."""
    path = filesystem_path(path).absolute()
    if os.name == "nt":
        # Path.as_uri treats the namespace's '?' as a URI authority. SQLite
        # instead accepts the percent-encoded native filename after 'file:'.
        return "file:" + quote(str(path), safe=":")
    return path.as_uri()
