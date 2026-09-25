"""Real local Git writer: bounded argv against a clone of the write remote.

Everything here is a real ``git`` subprocess with a deadline, output cap and
owned process-tree cleanup. Push is always a fast-forward to our own
contribution branch — never a force push, never a write to the base branch.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .common import (
    DEFAULT_GIT_OP_SECONDS,
    MAX_FILE_BYTES,
    CommunityError,
    Deadline,
    TransientError,
    UnknownOutcome,
    lf_bytes,
    run_argv,
)

MAX_GIT_OUTPUT = 256 * 1024

# stderr shapes that mean the environment (network/DNS/rate limit), not the
# content or the authorization. Only these make an attempt retryable as
# "unavailable"; anything else — notably authentication and permission
# refusals — stays a terminal, visible failure.
_TRANSIENT_GIT_MARKERS = (
    "could not resolve",
    "couldn't connect",
    "failed to connect",
    "connection refused",
    "connection reset",
    "connection timed out",
    "network is unreachable",
    "temporary failure in name resolution",
    "operation timed out",
    "timeout",
    "timed out",
    "tls handshake timeout",
    "proxy connect aborted",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "rate limit",
    "service unavailable",
)


def _is_transient_git_error(detail: str) -> bool:
    text = (detail or "").lower()
    return any(marker in text for marker in _TRANSIENT_GIT_MARKERS)
# Per-command credential wiring (no global config writes): clear any inherited
# helper, then delegate to the maintainer's authenticated `gh` CLI. The token
# itself never appears in argv, URLs or logs.
GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_COUNT": "3",
    "GIT_CONFIG_KEY_0": "credential.helper",
    "GIT_CONFIG_VALUE_0": "",
    "GIT_CONFIG_KEY_1": "credential.helper",
    "GIT_CONFIG_VALUE_1": "!gh auth git-credential",
    "GIT_CONFIG_KEY_2": "core.hooksPath",
    "GIT_CONFIG_VALUE_2": os.devnull,
}


def git_env(settings: Mapping[str, Any] | None = None, token_env: str | None = None) -> dict[str, str]:
    """Scoped subprocess env: bind the configured token env var as GH_TOKEN.

    When the variable is unset, ``gh`` falls back to its own authenticated
    account. Plugin-repo mutations pass ``bot.plugin_token_env`` explicitly so
    the separate plugin credential is used for BOTH git and API calls.
    """
    env = dict(GIT_ENV)
    name = token_env or (settings or {}).get("token_env")
    token = os.environ.get(name) if name else None
    if token:
        env["GH_TOKEN"] = token
    return env


def _git(
    argv: Sequence[str],
    deadline: Deadline,
    *,
    cwd: Path | None = None,
    unknown_on_timeout: bool = False,
    env: Mapping[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> str:
    remaining = deadline.step(f"git {argv[0]}")
    result = run_argv(
        ["git", *argv],
        timeout=min(DEFAULT_GIT_OP_SECONDS, remaining),
        max_output=MAX_GIT_OUTPUT,
        input_bytes=input_bytes,
        cwd=cwd,
        env=env or GIT_ENV,
    )
    if result.timed_out:
        if unknown_on_timeout:
            raise UnknownOutcome(f"git {argv[0]} timed out; remote outcome unknown")
        raise TransientError(f"git {argv[0]} timed out")
    if result.code != 0:
        detail = result.err_text.strip()[:300]
        if _is_transient_git_error(detail):
            raise TransientError(f"git {argv[0]} failed: {detail}")
        raise CommunityError(f"git {argv[0]} failed: {detail}")
    return result.out_text


def remote_tip(remote_url: str, ref: str, deadline: Deadline, *, env=None) -> str | None:
    out = _git(["ls-remote", remote_url, ref], deadline, env=env)
    return out.split()[0] if out.strip() else None


def ensure_clone(remote_url: str, work_dir: Path, deadline: Deadline, *, env=None) -> Path:
    """Clone once, fetch afterwards; the clone lives under the private state dir."""
    if (work_dir / ".git").is_dir():
        _git(["fetch", "origin", "--prune"], deadline, cwd=work_dir, env=env)
    else:
        work_dir.parent.mkdir(parents=True, exist_ok=True)
        _git(["clone", "--quiet", remote_url, str(work_dir)], deadline, env=env)
    return work_dir


def checkout_new(work_dir: Path, branch: str, base_ref: str, deadline: Deadline, *, env=None) -> None:
    _git(["checkout", "--quiet", "-B", branch, base_ref], deadline, cwd=work_dir, env=env)


def checkout_existing(work_dir: Path, branch: str, deadline: Deadline, *, env=None) -> bool:
    """Check out our existing remote branch tip. False when it does not exist."""
    remaining = deadline.step("git checkout existing branch")
    from .common import run_argv as _run

    result = _run(
        ["git", "checkout", "--quiet", "-B", branch, f"origin/{branch}"],
        timeout=min(DEFAULT_GIT_OP_SECONDS, remaining),
        max_output=MAX_GIT_OUTPUT,
        cwd=work_dir,
        env=env or GIT_ENV,
    )
    return result.code == 0


def tree_sha256(work_dir: Path, path: str) -> str | None:
    target = work_dir / path
    if not target.is_file() or target.is_symlink():
        return None
    raw = target.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        raise CommunityError(f"{path} in the remote branch exceeds the per-file platform envelope")
    return hashlib.sha256(raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")).hexdigest()


def read_tree_file(work_dir: Path, path: str) -> str | None:
    target = work_dir / path
    if not target.is_file() or target.is_symlink():
        return None
    raw = target.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        raise CommunityError(f"{path} in the remote branch exceeds the per-file platform envelope")
    return raw.decode("utf-8")


def apply_files(work_dir: Path, files: Sequence[Mapping[str, Any]]) -> list[str]:
    """Write canonical LF bytes, plain 100644 files only; returns paths written."""
    written = []
    for item in files:
        path = item["path"]
        target = work_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = lf_bytes(item["content"])
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(raw)
        tmp.replace(target)
        target.chmod(0o644)
        written.append(path)
    return written


def stage_and_commit(
    work_dir: Path, paths: Sequence[str], message: str, deadline: Deadline, *, env=None
) -> str | None:
    """Commit staged changes; returns the commit SHA, or None when unchanged.

    Paths travel as a NUL pathspec on stdin. Putting every path on the
    command line exceeds the OS argument limit once a batch is large, and
    Git never starts.
    """
    if not paths:
        return current_head(work_dir, deadline)
    spec = b"\0".join(path.encode("utf-8") for path in paths) + b"\0"
    _git(
        ["add", "--pathspec-from-file=-", "--pathspec-file-nul"],
        deadline,
        cwd=work_dir,
        env=env,
        input_bytes=spec,
    )
    remaining = deadline.step("git diff cached")
    from .common import run_argv as _run

    diff = _run(
        ["git", "diff", "--cached", "--quiet"],
        timeout=min(DEFAULT_GIT_OP_SECONDS, remaining),
        max_output=4096,
        cwd=work_dir,
        env=env or GIT_ENV,
    )
    if diff.code == 0:
        return None
    _commit(work_dir, message, deadline, env=env)
    return current_head(work_dir, deadline)


def _commit(work_dir: Path, message: str, deadline: Deadline, *, env=None) -> None:
    remaining = deadline.step("git commit")
    from .common import run_argv as _run

    result = _run(
        [
            "git",
            "-c", "user.name=mindie-community-bot",
            "-c", "user.email=mindie-community-bot@localhost.invalid",
            "commit", "--quiet", "-F", "-",
        ],
        timeout=min(DEFAULT_GIT_OP_SECONDS, remaining),
        max_output=MAX_GIT_OUTPUT,
        input_bytes=message.encode("utf-8"),
        cwd=work_dir,
        env=env or GIT_ENV,
    )
    if result.code != 0:
        raise CommunityError(f"git commit failed: {result.err_text.strip()[:300]}")


def current_head(work_dir: Path, deadline: Deadline, *, env=None) -> str:
    return _git(["rev-parse", "HEAD"], deadline, cwd=work_dir, env=env).strip()


def fetch_ref(
    work_dir: Path, remote: str, ref: str, deadline: Deadline, *, env=None
) -> str:
    """Fetch one ref from an explicit URL or remote into FETCH_HEAD.

    Does not move any local branch. A branch name is not accepted as a
    substitute for the requested ref.
    """
    remaining = deadline.step("git fetch ref")
    from .common import run_argv as _run

    result = _run(
        ["git", "fetch", "--quiet", remote, ref],
        timeout=min(DEFAULT_GIT_OP_SECONDS, remaining),
        max_output=MAX_GIT_OUTPUT,
        cwd=work_dir,
        env=env or GIT_ENV,
    )
    if result.timed_out:
        raise TransientError(f"git fetch {ref} timed out")
    if result.code != 0:
        detail = result.err_text.strip()[:300]
        if _is_transient_git_error(detail):
            raise TransientError(f"git fetch {ref} failed: {detail}")
        raise CommunityError(f"cannot fetch {ref}")
    return _git(["rev-parse", "FETCH_HEAD"], deadline, cwd=work_dir, env=env).strip()


def fetch_pr_head(
    work_dir: Path, number: int, head_ref: str, deadline: Deadline, *, env=None, remote_url: str | None = None
) -> str:
    """Fetch ``refs/pull/N/head`` only. ``head_ref`` is not a fallback.

    The fetch's own exception is the result: a timeout stays ``TransientError``
    and any finite ``retry_at`` stays on that exception. This function does
    not reclassify it.
    """
    del head_ref
    remote = remote_url or "origin"
    return fetch_ref(work_dir, remote, f"refs/pull/{int(number)}/head", deadline, env=env)


def ls_tree(work_dir: Path, commit: str, deadline: Deadline, *, env=None) -> dict[str, str]:
    """Map path -> git mode for one commit; catches symlinks/exec/submodules."""
    out = _git(["ls-tree", "-r", commit], deadline, cwd=work_dir, env=env)
    modes: dict[str, str] = {}
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) >= 1 and path:
            modes[path] = parts[0]
    return modes


def _revision_absent(err: str) -> bool:
    text = err.lower()
    return "not a valid object name" in text or "bad revision" in text


def show_file(work_dir: Path, commit: str, path: str, deadline: Deadline, *, env=None) -> str | None:
    """Read one blob. None only when that commit or path is demonstrably absent.

    A timeout or any other unread result raises ``TransientError`` (status
    ``unavailable``). Callers surface that through ``RestoreUnavailable``
    instead of treating it as a deleted file.
    """
    from .common import MAX_FILE_BYTES, run_argv as _run

    env = env or GIT_ENV
    remaining = deadline.step("git show")
    present = _run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        timeout=min(DEFAULT_GIT_OP_SECONDS, remaining),
        max_output=64 * 1024,
        cwd=work_dir,
        env=env,
    )
    if present.timed_out:
        raise TransientError("git show timed out; the blob was not read")
    if present.code != 0:
        if _revision_absent(present.err_text):
            return None
        detail = present.err_text.strip()[:300]
        raise TransientError(f"git show failed: {detail}")

    # The blob may be as large as the platform file envelope. A little extra
    # room covers git's stderr without turning a legal body into a truncation.
    remaining = deadline.step("git show")
    result = _run(
        ["git", "show", f"{commit}:{path}", "--"],
        timeout=min(DEFAULT_GIT_OP_SECONDS, remaining),
        max_output=MAX_FILE_BYTES + 64 * 1024,
        cwd=work_dir,
        env=env,
    )
    if result.timed_out:
        raise TransientError("git show timed out; the blob was not read")
    if result.code != 0:
        err = result.err_text.lower()
        # A present commit with no such path. This Git reports that as
        # "bad revision '<commit>:<path>'" rather than "does not exist".
        missing = (
            "does not exist" in err
            or "exists on disk, but not in" in err
            or ("bad revision" in err and ":" in err)
        )
        if missing:
            return None
        detail = result.err_text.strip()[:300]
        raise TransientError(f"git show failed: {detail}")
    if len(result.out) > MAX_FILE_BYTES:
        raise CommunityError(f"{path} exceeds the per-file platform envelope")
    return result.out_text


def fetch_commit(work_dir: Path, sha: str, deadline: Deadline, *, env=None) -> bool:
    """Best-effort fetch of one exact commit; False when the server refuses
    and the object is not already local."""
    remaining = deadline.step("git fetch commit")
    from .common import run_argv as _run

    have = _run(
        ["git", "cat-file", "-e", sha],
        timeout=min(DEFAULT_GIT_OP_SECONDS, remaining),
        max_output=4096,
        cwd=work_dir,
        env=env or GIT_ENV,
    )
    if have.code == 0:
        return True
    result = _run(
        ["git", "fetch", "--quiet", "origin", sha],
        timeout=min(DEFAULT_GIT_OP_SECONDS, deadline.remaining()),
        max_output=MAX_GIT_OUTPUT,
        cwd=work_dir,
        env=env or GIT_ENV,
    )
    return result.code == 0


def is_ancestor(work_dir: Path, old: str, new: str, deadline: Deadline, *, env=None) -> bool | None:
    """True/False ancestry verdict; None when it cannot be decided locally."""
    remaining = deadline.step("git merge-base")
    from .common import run_argv as _run

    result = _run(
        ["git", "merge-base", "--is-ancestor", old, new],
        timeout=min(DEFAULT_GIT_OP_SECONDS, remaining),
        max_output=MAX_GIT_OUTPUT,
        cwd=work_dir,
        env=env or GIT_ENV,
    )
    if result.code == 0:
        return True
    if result.code == 1:
        return False
    return None


def push_branch(work_dir: Path, branch: str, deadline: Deadline, *, env=None, cancel=None) -> None:
    """Fast-forward-only push of our own contribution branch. Never forced."""
    remaining = deadline.step("git push")
    from .common import run_argv as _run

    result = _run(
        ["git", "push", "--no-force-with-lease", "origin", f"HEAD:refs/heads/{branch}"],
        timeout=min(DEFAULT_GIT_OP_SECONDS, remaining),
        max_output=MAX_GIT_OUTPUT,
        cwd=work_dir,
        env=env or GIT_ENV,
        cancel=cancel,
    )
    if result.timed_out:
        raise UnknownOutcome("git push timed out; remote outcome unknown")
    if result.code != 0:
        detail = result.err_text.strip()[:300]
        if "non-fast-forward" in detail or "fetch first" in detail:
            raise CommunityError(
                "contribution branch diverged on the remote; kept for review",
                status="needs_review",
            )
        if _is_transient_git_error(detail):
            # The push may or may not have landed: this is an uncertain
            # remote write, reconciled read-only — never a clean refusal and
            # never an automatic blind retry.
            raise UnknownOutcome(
                f"git push outcome uncertain (network failure): {detail[:200]}"
            )
        raise CommunityError(f"git push failed: {detail}")
