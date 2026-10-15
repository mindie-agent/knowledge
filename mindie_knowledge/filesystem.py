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
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def filesystem_path(path):
    """Give Python an absolute extended path on Windows, before any I/O."""
    if os.name == "nt":
        return Path(_windows_extended(os.path.abspath(path)))
    return Path(path)


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
