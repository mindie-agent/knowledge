"""GitHub transport boundary: real ``gh`` CLI plus a file-backed dev double.

All GitHub traffic goes through the narrow ``Transport`` surface below, so
tests and fault injection substitute the boundary without touching the state
machines. The real implementation shells out to the maintainer-owned ``gh``
CLI with argv only (never a shell), body payloads via temp files, and the token
reaching ``gh`` exclusively through its own environment variable.

``FileTransport`` is a development double backed by a JSON document and real
local Git remotes: it exists so mechanism tests can drive deterministic GitHub
shapes. Passing tests against it are mechanism evidence only — they are not
GitHub acceptance evidence.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Mapping

from .common import (
    DEFAULT_API_OP_SECONDS,
    CommunityError,
    Deadline,
    TransientError,
    UnknownOutcome,
    check_repository,
    run_argv,
)

MAX_API_OUTPUT = 512 * 1024


def _brief(payload: Any) -> str:
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(payload)
    return text[:200]


class Transport:
    """Narrow GitHub surface used by contributor publication."""

    def find_pull_requests(
        self, repo: str, *, head_branch: str, deadline: Deadline, head_owner: str | None = None
    ) -> list[dict]:
        raise NotImplementedError


    def get_pull_request(self, repo: str, number: int, deadline: Deadline) -> dict:
        raise NotImplementedError

    def create_pull_request(
        self, repo: str, *, title: str, body: str, head: str, base: str, deadline: Deadline
    ) -> dict:
        raise NotImplementedError

    def update_pull_request(
        self, repo: str, number: int, *, title: str, body: str, deadline: Deadline
    ) -> dict:
        raise NotImplementedError








# --------------------------------------------------------------------------- #
# Real transport: the maintainer-owned GitHub CLI
# --------------------------------------------------------------------------- #


class GhTransport(Transport):
    """Bounded ``gh api`` argv calls. The token stays in gh's own env var."""

    def __init__(self, settings: Mapping[str, Any]):
        self.settings = settings
        self.token_env = settings.get("token_env", "GH_TOKEN")
        self.op_seconds = min(DEFAULT_API_OP_SECONDS, settings.get("transaction_seconds", 120))

    def _api(
        self, method: str, path: str, deadline: Deadline, body: Mapping[str, Any] | None = None
    ) -> Any:
        remaining = deadline.step(f"gh {method} {path.split('?')[0]}")
        argv = ["gh", "api", "--include", "--method", method, "-H", "Accept: application/vnd.github+json"]
        tmp = None
        try:
            if body is not None:
                tmp = tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", suffix=".json", delete=False
                )
                json.dump(body, tmp, ensure_ascii=False)
                tmp.close()
                argv += ["--input", tmp.name]
            argv.append(path)
            env = None
            token = os.environ.get(self.token_env)
            if token:
                env = {"GH_TOKEN": token}
            result = run_argv(
                argv,
                timeout=min(self.op_seconds, remaining),
                max_output=MAX_API_OUTPUT,
                env=env,
            )
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
        header, body_text = _split_included(result.out_text)
        payload: Any = None
        if body_text.strip():
            try:
                payload = json.loads(body_text)
            except json.JSONDecodeError:
                payload = None
        evidence = "\n".join((header, result.err_text))
        status = _http_status(evidence)
        if result.timed_out:
            _read_or_unknown(
                method == "GET",
                f"gh api {method} {path} timed out",
                unknown_detail=f"gh api {method} {path} timed out; outcome unknown",
            )
        if result.code != 0:
            message = ""
            if isinstance(payload, Mapping):
                message = str(payload.get("message") or "")
            if not message:
                # gh prints HTTP failures to stderr; keep detail bounded and
                # never include argv or the token.
                message = result.err_text.strip().splitlines()[0] if result.err_text.strip() else "gh api failed"
            rate = _rate_limited(status, message, result.err_text)
            retry_at = _retry_epoch(header) if rate or (status is not None and status >= 500) else None
            if rate:
                detail = (
                    f"GitHub rate limit on {method} {path}: {message}"
                    + (f" (HTTP {status})" if status else "")
                )
                # A rate-limited write may have landed. Stay unknown and only
                # delay the next read-only reconciliation.
                _read_or_unknown(
                    method == "GET",
                    detail,
                    unknown_detail=f"gh api {method} {path}: rate limited; outcome unknown",
                    retry_at=retry_at,
                )
            if status in (401, 403, 404):
                raise CommunityError(f"GitHub refused {method} {path}: {message} (HTTP {status})")
            if status is None:
                _read_or_unknown(
                    method == "GET",
                    f"GitHub unreachable on {method} {path}: {message}",
                    unknown_detail=f"gh api {method} {path}: no HTTP response; outcome unknown",
                )
            if status >= 500:
                _read_or_unknown(
                    method == "GET",
                    f"GitHub temporarily unavailable on {method} {path}: {message} (HTTP {status})",
                    unknown_detail=f"gh api {method} {path}: HTTP {status}; outcome unknown",
                    retry_at=retry_at,
                )
            raise CommunityError(f"GitHub error on {method} {path}: {message} (HTTP {status})")
        return payload

    @staticmethod
    def _query(path: str, **params: str) -> str:
        from urllib.parse import quote

        return path + "?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items())

    def find_pull_requests(
        self, repo: str, *, head_branch: str, deadline: Deadline, head_owner: str | None = None
    ) -> list[dict]:
        check_repository(repo)
        owner = head_owner or _configured_head_owner(self.settings, repo)
        payload = self._api(
            "GET", self._query(f"/repos/{repo}/pulls", head=f"{owner}:{head_branch}", state="all"), deadline
        )
        # A completed lookup is a list of selectable PRs. An unreadable 2xx
        # body is an unavailable read — not "no PR" and not a content rejection.
        # Only a valid empty list proves absence.
        if not isinstance(payload, list):
            raise TransientError("PR lookup returned an unreadable list")
        return [_normalize_pr(item) for item in payload]


    def get_pull_request(self, repo: str, number: int, deadline: Deadline) -> dict:
        payload = self._api("GET", f"/repos/{repo}/pulls/{int(number)}", deadline)
        if not isinstance(payload, Mapping):
            raise TransientError("PR lookup returned an unreadable PR")
        return _normalize_pr(payload, number=int(number))

    def create_pull_request(self, repo, *, title, body, head, base, deadline) -> dict:
        payload = self._api(
            "POST",
            f"/repos/{repo}/pulls",
            deadline,
            {"title": title, "body": body, "head": head, "base": base, "maintainer_can_modify": True},
        )
        if not isinstance(payload, Mapping) or "number" not in payload:
            raise UnknownOutcome("PR creation returned an unreadable response")
        return dict(payload)

    def update_pull_request(self, repo, number, *, title, body, deadline) -> dict:
        payload = self._api(
            "PATCH", f"/repos/{repo}/pulls/{int(number)}", deadline, {"title": title, "body": body}
        )
        return dict(payload) if isinstance(payload, Mapping) else {}








_HTTP_STATUS = re.compile(
    r"(?:\bHTTP/\d+(?:\.\d+)?|\bHTTP|\(\s*HTTP)\s+(\d{3})\b",
    re.IGNORECASE,
)
_RETRY_AFTER = re.compile(r"(?im)^retry-after:\s*(.+?)\s*$")


def _http_status(text: str) -> int | None:
    """Parse gh's ``(HTTP 403)`` and a real status line. Not every token."""
    match = _HTTP_STATUS.search(text or "")
    if not match:
        return None
    try:
        code = int(match.group(1))
    except ValueError:
        return None
    return code if 100 <= code <= 599 else None


def _split_included(text: str) -> tuple[str, str]:
    """Split ``gh api --include`` headers from the body. Bare JSON stays a body.

    Real responses use LF or CRLF. The blank line is the separator; a payload
    that does not start with a status line is left intact.
    """
    raw = text or ""
    if not raw.lstrip().startswith("HTTP/"):
        return "", raw.strip()
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    header, sep, body = normalized.partition("\n\n")
    if not sep:
        return "", raw.strip()
    return header, body.strip()


def _rate_limited(status: int | None, *parts: str) -> bool:
    if status == 429:
        return True
    blob = "\n".join(parts).lower()
    return "rate limit" in blob


def _retry_epoch(header: str) -> float | None:
    """Retry-After as a finite Unix epoch. Relative seconds or an HTTP date."""
    match = _RETRY_AFTER.search(header or "")
    if not match:
        return None
    raw = match.group(1).strip().strip('"')
    if raw.lower() in {"nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity", "true", "false"}:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        seconds = None
    else:
        if not math.isfinite(seconds) or seconds < 0:
            return None
        return time.time() + seconds
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        from datetime import timezone
        parsed = parsed.replace(tzinfo=timezone.utc)
    epoch = parsed.timestamp()
    if not math.isfinite(epoch):
        return None
    return epoch


def _read_or_unknown(is_read: bool, read_detail: str, *, unknown_detail: str, retry_at: float | None = None) -> None:
    if is_read:
        raise TransientError(read_detail, retry_at=retry_at)
    raise UnknownOutcome(unknown_detail, retry_at=retry_at)


def _selectable_pr(item: Any) -> bool:
    """Number, head ref, and state open/closed. Not a GitHub schema."""
    if not isinstance(item, Mapping):
        return False
    number = item.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return False
    if item.get("state") not in ("open", "closed"):
        return False
    head = item.get("head")
    if not isinstance(head, Mapping):
        return False
    ref = head.get("ref")
    return isinstance(ref, str) and bool(ref)


def _merged_flag(item: Mapping) -> bool:
    """List omits ``merged``. A nonempty ``merged_at`` is the merge.

    An explicit boolean wins. ``null`` or a missing key is not a boolean,
    so the timestamp is used. No timestamp means not merged.
    """
    merged = item.get("merged")
    if isinstance(merged, bool):
        return merged
    merged_at = item.get("merged_at")
    return isinstance(merged_at, str) and bool(merged_at.strip())


def _normalize_pr(item: Mapping, *, number: int | None = None) -> dict:
    """Copy one REST PR into the dict downstream already reads.

    ``number`` is the detail request. A body for a different PR is unread.
    """
    if not _selectable_pr(item) or (number is not None and item.get("number") != number):
        raise TransientError("PR lookup returned an unreadable PR")
    out = dict(item)
    out["merged"] = _merged_flag(item)
    return out


def _configured_head_owner(settings: Mapping[str, Any], repo: str) -> str:
    """PR head owner is the fork account when publication pushes to a fork."""
    fork = settings.get("fork")
    if isinstance(fork, str) and "/" in fork:
        return fork.split("/", 1)[0]
    return repo.split("/", 1)[0]


# --------------------------------------------------------------------------- #
# File-backed development transport (mechanism tests only)
# --------------------------------------------------------------------------- #

_EMPTY = {"repos": {}}


class FileTransport(Transport):
    """Deterministic dev double over a JSON document plus real local Git.

    ``remotes`` maps ``owner/repo`` to a Git URL (a local bare repo in tests);
    branch tips are resolved with real ``git ls-remote`` so PR head metadata
    always reflects actual pushed state. Test doubles seeding this file are
    not evidence of GitHub behaviour.
    """

    def __init__(self, path: Path, remotes: Mapping[str, str] | None = None):
        self.path = Path(path)
        self.remotes = dict(remotes or {})
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(_EMPTY)

    # -- state file plumbing ------------------------------------------------ #

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"repos": {}}
        return data if isinstance(data, dict) else {"repos": {}}

    def _write(self, data: Mapping[str, Any]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def _repo(self, data: dict, repo: str) -> dict:
        return data.setdefault("repos", {}).setdefault(
            repo,
            {"pulls": {}, "next_pr": 1, "comments": [], "checks": {}, "permissions": True},
        )

    def seed(self, repo: str, **fields: Any) -> None:
        """Test hook: set repo-level state such as checks or permissions."""
        with self.lock:
            data = self._read()
            self._repo(data, repo).update(fields)
            self._write(data)

    def _tip(self, repo: str, branch: str, deadline: Deadline) -> str | None:
        url = self.remotes.get(repo)
        if not url:
            return None
        remaining = deadline.step("git ls-remote")
        result = run_argv(
            ["git", "ls-remote", url, f"refs/heads/{branch}"],
            timeout=min(30, remaining),
            max_output=64 * 1024,
        )
        if result.code != 0 or not result.out_text.strip():
            return None
        return result.out_text.split()[0]

    # -- transport surface --------------------------------------------------- #

    def find_pull_requests(self, repo, *, head_branch, deadline, head_owner=None) -> list[dict]:
        deadline.step("find pull requests")
        with self.lock:
            repo_state = self._repo(self._read(), repo)
            found = []
            for pr in repo_state["pulls"].values():
                head = pr.get("head") or {}
                if head.get("ref") != head_branch:
                    continue
                owner = ((head.get("repo") or {}).get("full_name") or "").split("/", 1)[0]
                if head_owner and owner and owner != head_owner:
                    continue
                found.append(_normalize_pr(pr) if _selectable_pr(pr) else dict(pr))
            return found

    def list_open_pull_requests(self, repo, *, deadline) -> list[dict]:
        deadline.step("list open pull requests")
        with self.lock:
            repo_state = self._repo(self._read(), repo)
            return [
                dict(pr)
                for pr in sorted(repo_state["pulls"].values(), key=lambda p: p.get("number", 0))
                if pr.get("state") == "open"
            ]

    def get_pull_request(self, repo, number, deadline) -> dict:
        deadline.step("get pull request")
        with self.lock:
            repo_state = self._repo(self._read(), repo)
            pr = repo_state["pulls"].get(str(int(number)))
            if pr is None:
                raise CommunityError(f"PR {number} not found")
            pr = dict(pr)
        # The head sha always reflects the actual remote branch tip.
        tip = self._tip(repo, pr.get("head", {}).get("ref", ""), deadline)
        if tip and pr.get("state") == "open":
            pr.setdefault("head", {})["sha"] = tip
        if _selectable_pr(pr):
            pr = _normalize_pr(pr)
        elif not isinstance(pr.get("merged"), bool):
            pr["merged"] = _merged_flag(pr)
        return pr

    def create_pull_request(self, repo, *, title, body, head, base, deadline) -> dict:
        deadline.step("create pull request")
        with self.lock:
            data = self._read()
            repo_state = self._repo(data, repo)
            if not repo_state.get("permissions", True):
                raise CommunityError("token lacks authority to create pull requests (HTTP 403)")
            offered = head.split(":", 1)[1] if isinstance(head, str) and ":" in head else head
            for pr in repo_state["pulls"].values():
                existing = (pr.get("head") or {}).get("ref")
                if pr.get("state") == "open" and existing in {head, offered}:
                    raise CommunityError("an open PR already exists for this branch (HTTP 422)")
            number = repo_state["next_pr"]
            repo_state["next_pr"] = number + 1
            ref = head
            full_name = repo
            if isinstance(head, str) and ":" in head:
                owner, ref = head.split(":", 1)
                repo_name = repo.split("/", 1)[1] if "/" in str(repo) else ""
                full_name = f"{owner}/{repo_name}" if repo_name else str(repo)
            sha = self._tip(repo, ref, deadline) or ""
            pr = {
                "number": number,
                "title": title,
                "body": body,
                "state": "open",
                "merged": False,
                "head": {"ref": ref, "sha": sha, "repo": {"full_name": full_name}},
                "base": {"ref": base, "repo": {"full_name": repo}},
                "user": {"login": "dev-transport"},
                "html_url": f"https://example.invalid/{repo}/pull/{number}",
            }
            repo_state["pulls"][str(number)] = pr
            self._write(data)
        if sha:
            url = self.remotes.get(repo)
            if url:
                # The object is already on the dev remote (the branch push).
                # Record the same commit as refs/pull/N/head, which is what
                # GitHub exposes for that PR. No second host and no force.
                remaining = deadline.step("record PR head ref")
                result = run_argv(
                    ["git", "--git-dir", url, "update-ref", f"refs/pull/{number}/head", sha],
                    timeout=min(30, remaining),
                    max_output=64 * 1024,
                )
                if result.timed_out:
                    raise TransientError("recording the PR head ref timed out")
                if result.code != 0:
                    raise CommunityError(
                        f"cannot record PR head ref: {result.err_text.strip()[:200]}"
                    )
        return dict(pr)

    def update_pull_request(self, repo, number, *, title, body, deadline) -> dict:
        deadline.step("update pull request")
        with self.lock:
            data = self._read()
            repo_state = self._repo(data, repo)
            pr = repo_state["pulls"].get(str(int(number)))
            if pr is None:
                raise CommunityError(f"PR {number} not found")
            pr["title"] = title
            pr["body"] = body
            pr["head"]["sha"] = self._tip(repo, pr["head"]["ref"], deadline) or pr["head"]["sha"]
            self._write(data)
            return dict(pr)


    def _ensure_mirror(self, repo: str, url: str, deadline: Deadline) -> Path:
        mirror = self.path.parent / "mirrors" / repo.replace("/", "_")
        remaining = deadline.step("git mirror sync")
        if mirror.is_dir():
            argv = ["git", "-C", str(mirror), "fetch", "origin", "+refs/heads/*:refs/remotes/origin/*"]
        else:
            mirror.parent.mkdir(parents=True, exist_ok=True)
            argv = ["git", "clone", "--mirror", url, str(mirror)]
        result = run_argv(argv, timeout=min(60, remaining), max_output=128 * 1024)
        if result.code != 0:
            raise CommunityError(f"cannot sync dev remote mirror: {result.err_text[:200]}")
        return mirror


    def merge_pull_request(self, repo, number, *, sha, method, deadline) -> dict:
        deadline.step("merge pull request")
        with self.lock:
            data = self._read()
            repo_state = self._repo(data, repo)
            if not repo_state.get("permissions", True):
                raise CommunityError("token lacks merge authority (HTTP 403)")
            pr = repo_state["pulls"].get(str(int(number)))
            if pr is None:
                raise CommunityError(f"PR {number} not found")
            if pr.get("state") != "open":
                raise CommunityError(f"PR {number} is not open")
            tip = self._tip(repo, pr["head"].get("ref", ""), deadline) or pr["head"].get("sha")
            if tip != sha:
                raise CommunityError("head moved since review (HTTP 409)")
            pr["head"]["sha"] = tip
            pr["state"] = "closed"
            pr["merged"] = True
            merge_sha = self._merge_into_remote(repo, pr, deadline)
            pr["merge_commit_sha"] = merge_sha
            self._write(data)
            return {"merged": True, "sha": merge_sha}

    def _merge_into_remote(self, repo: str, pr: Mapping[str, Any], deadline: Deadline) -> str:
        """Perform a real squash merge into the local dev remote's base branch."""
        url = self.remotes.get(repo)
        if not url:
            return ""
        self._ensure_mirror(repo, url, deadline)
        work = self.path.parent / "merge-work" / repo.replace("/", "_")
        deadline.step("dev merge worktree")
        if (work / ".git").is_dir():
            run_argv(["git", "-C", str(work), "fetch", "origin", "--prune"],
                     timeout=min(60, deadline.remaining()), max_output=128 * 1024)
        else:
            work.parent.mkdir(parents=True, exist_ok=True)
            result = run_argv(["git", "clone", "--quiet", url, str(work)],
                              timeout=min(60, deadline.remaining()), max_output=128 * 1024)
            if result.code != 0:
                raise CommunityError(f"dev merge clone failed: {result.err_text[:200]}")
        base = pr["base"]["ref"]
        head = pr["head"]["ref"]
        sequence = [
            ["git", "-C", str(work), "checkout", "--quiet", "-B", base, f"origin/{base}"],
            ["git", "-C", str(work), "merge", "--squash", "--quiet", f"origin/{head}"],
            [
                "git", "-C", str(work),
                "-c", "user.name=mindie-dev-transport",
                "-c", "user.email=dev-transport@example.invalid",
                "commit", "--quiet", "-m", f"merge {head} into {base}",
            ],
            ["git", "-C", str(work), "push", "origin", f"HEAD:refs/heads/{base}"],
        ]
        for argv in sequence:
            result = run_argv(argv, timeout=min(60, deadline.remaining()), max_output=128 * 1024)
            if result.code != 0:
                raise CommunityError(f"dev merge failed at {argv[2]}: {result.err_text[:200]}")
        result = run_argv(
            ["git", "-C", str(work), "rev-parse", "HEAD"],
            timeout=10,
            max_output=4096,
        )
        return result.out_text.strip()





def transport_from_settings(settings: Mapping[str, Any], state_dir: Path) -> Transport:
    kind = settings.get("transport", "gh")
    if kind == "gh":
        return GhTransport(settings)
    if kind == "file":
        path = Path(state_dir) / "dev-github.json"
        remotes = settings.get("dev_remotes") or {}
        return FileTransport(path, remotes)
    raise CommunityError(f"unknown transport {kind!r}")
