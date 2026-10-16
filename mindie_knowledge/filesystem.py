"""Native Windows paths without changing the physical state location."""

import ntpath
import os
from pathlib import Path
from urllib.parse import quote


def _windows_extended(path):
    path = ntpath.normpath(path)
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def _windows_logical(path):
    if path[:8].casefold() == "\\\\?\\unc\\":
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def filesystem_path(path):
    """Give Python an absolute extended path on Windows, before any I/O."""
    if os.name == "nt":
        return Path(_windows_extended(os.path.abspath(path)))
    return Path(path)


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
