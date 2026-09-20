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
    CommunityError,
    Deadline,
    UnknownOutcome,
    lf_bytes,
    run_argv,
)

MAX_GIT_OUTPUT = 256 * 1024
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
) -> str:
    remaining = deadline.step(f"git {argv[0]}")
    result = run_argv(
        ["git", *argv],
        timeout=min(DEFAULT_GIT_OP_SECONDS, remaining),
        max_output=MAX_GIT_OUTPUT,
        cwd=cwd,
        env=env or GIT_ENV,
    )
    if result.timed_out:
        if unknown_on_timeout:
            raise UnknownOutcome(f"git {argv[0]} timed out; remote outcome unknown")
        raise CommunityError(f"git {argv[0]} timed out")
    if result.code != 0:
        raise CommunityError(f"git {argv[0]} failed: {result.err_text.strip()[:300]}")
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
    if len(raw) > 128 * 1024:
        raise CommunityError(f"{path} in the remote branch exceeds the file limit")
    return hashlib.sha256(raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")).hexdigest()


def read_tree_file(work_dir: Path, path: str) -> str | None:
    target = work_dir / path
    if not target.is_file() or target.is_symlink():
        return None
    raw = target.read_bytes()
    if len(raw) > 128 * 1024:
        raise CommunityError(f"{path} in the remote branch exceeds the file limit")
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
    """Commit staged changes; returns the commit SHA, or None when unchanged."""
    if not paths:
        return current_head(work_dir, deadline)
    _git(["add", "--", *paths], deadline, cwd=work_dir, env=env)
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


def fetch_pr_head(work_dir: Path, number: int, head_ref: str, deadline: Deadline, *, env=None) -> str:
    """Fetch the exact PR head commit; GitHub exposes refs/pull/N/head."""
    remaining = deadline.step("git fetch pr head")
    from .common import run_argv as _run

    for ref in (f"refs/pull/{int(number)}/head", f"refs/heads/{head_ref}"):
        result = _run(
            ["git", "fetch", "--quiet", "origin", ref],
            timeout=min(DEFAULT_GIT_OP_SECONDS, remaining),
            max_output=MAX_GIT_OUTPUT,
            cwd=work_dir,
            env=env or GIT_ENV,
        )
        if result.code == 0:
            return _git(["rev-parse", "FETCH_HEAD"], deadline, cwd=work_dir).strip()
    raise CommunityError("cannot fetch the PR head commit")


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


def show_file(work_dir: Path, commit: str, path: str, deadline: Deadline, *, env=None) -> str | None:
    remaining = deadline.step("git show")
    from .common import run_argv as _run

    result = _run(
        ["git", "show", f"{commit}:{path}"],
        timeout=min(DEFAULT_GIT_OP_SECONDS, remaining),
        max_output=256 * 1024,
        cwd=work_dir,
        env=env or GIT_ENV,
    )
    if result.code != 0:
        return None
    return result.out_text


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
        raise CommunityError(f"git push failed: {detail}")
