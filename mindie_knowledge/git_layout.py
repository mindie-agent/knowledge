"""Preflight the supported Windows Git directory, not long material paths."""
import ntpath
import os

from .filesystem import _windows_logical

WINDOWS = os.name == "nt"
# Git for Windows setup_explicit_git_dir_str checks PATH_MAX - 40. The
# supported native Git rejects 221 UTF-8 bytes before repository validation.
MAX_GIT_DIRECTORY_BYTES = 220


class UnsupportedGitLayout(OSError):
    code = "unsupported_windows_git_root"
    mindie_category = "configuration"
    mindie_execution = "not_started"
    external_write_attempted = False

    def __init__(self, role, path_bytes):
        self.role = role
        self.path_bytes = path_bytes
        self.limit_bytes = MAX_GIT_DIRECTORY_BYTES
        super().__init__(
            f"{self.code}: {role} Git directory is {path_bytes} UTF-8 bytes; "
            f"limit={self.limit_bytes}; git_operation_execution=not_started; "
            "existing state preserved"
        )


def validate_git_directory(path, *, bare, role):
    """Reject an unsupported actual Git root before mkdir, fetch or clone."""
    if not WINDOWS:
        return
    ordinary = ntpath.normpath(_windows_logical(ntpath.abspath(os.fspath(path))))
    directory = ordinary if bare else ntpath.join(ordinary, ".git")
    size = len(directory.encode("utf-8"))
    if size > MAX_GIT_DIRECTORY_BYTES:
        raise UnsupportedGitLayout(role, size)


def feed_repository_path(state, ident):
    return state / "feed" / ident / "repo.git"


def validate_config_feed_layout(config):
    """Read-only installation preflight through the existing layout identity."""
    if not WINDOWS or not config.get("feeds"):
        return
    from .state_layout import state_root
    from .loop.feed import feed_ident

    state = state_root(config["root"], config["domain"])
    for item in config["feeds"]:
        ident = feed_ident(item["repository"], item.get("ref", "main"),
                           (item.get("prefix") or "").strip("/"))
        validate_git_directory(feed_repository_path(state, ident),
                               bare=True, role="feed")
