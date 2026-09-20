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
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from .common import (
    DEFAULT_API_OP_SECONDS,
    CommunityError,
    Deadline,
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

    def find_pull_requests(self, repo: str, *, head_branch: str, deadline: Deadline) -> list[dict]:
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
        self.token_env = settings.get("token_env", "GH_TOKEN")
        self.op_seconds = min(DEFAULT_API_OP_SECONDS, settings.get("transaction_seconds", 120))

    def _api(
        self, method: str, path: str, deadline: Deadline, body: Mapping[str, Any] | None = None
    ) -> Any:
        remaining = deadline.step(f"gh {method} {path.split('?')[0]}")
        argv = ["gh", "api", "--method", method, "-H", "Accept: application/vnd.github+json"]
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
        if result.timed_out:
            raise UnknownOutcome(f"gh api {method} {path} timed out; outcome unknown")
        text = result.out_text.strip()
        payload: Any = None
        if text:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
        if result.code != 0:
            message = ""
            if isinstance(payload, Mapping):
                message = str(payload.get("message") or "")
            if not message:
                # gh prints HTTP failures to stderr; keep detail bounded and
                # never include argv, headers or environment.
                message = result.err_text.strip().splitlines()[0] if result.err_text.strip() else "gh api failed"
            status = _http_status(result.err_text)
            if status in (401, 403, 404):
                raise CommunityError(f"GitHub refused {method} {path}: {message} (HTTP {status})")
            if status is None:
                raise UnknownOutcome(f"gh api {method} {path}: no HTTP response; outcome unknown")
            raise CommunityError(f"GitHub error on {method} {path}: {message} (HTTP {status})")
        return payload

    @staticmethod
    def _query(path: str, **params: str) -> str:
        from urllib.parse import quote

        return path + "?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items())

    def find_pull_requests(self, repo: str, *, head_branch: str, deadline: Deadline) -> list[dict]:
        check_repository(repo)
        owner = repo.split("/", 1)[0]
        payload = self._api(
            "GET", self._query(f"/repos/{repo}/pulls", head=f"{owner}:{head_branch}", state="all"), deadline
        )
        return list(payload) if isinstance(payload, list) else []


    def get_pull_request(self, repo: str, number: int, deadline: Deadline) -> dict:
        payload = self._api("GET", f"/repos/{repo}/pulls/{int(number)}", deadline)
        if not isinstance(payload, Mapping):
            raise CommunityError("unexpected PR payload")
        return dict(payload)

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








def _http_status(stderr_text: str) -> int | None:
    for token in stderr_text.split():
        if token.startswith("HTTP"):
            try:
                return int(token.removeprefix("HTTP").strip(": "))
            except ValueError:
                return None
    return None


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

    def find_pull_requests(self, repo, *, head_branch, deadline) -> list[dict]:
        deadline.step("find pull requests")
        with self.lock:
            repo_state = self._repo(self._read(), repo)
            return [
                dict(pr)
                for pr in repo_state["pulls"].values()
                if pr.get("head", {}).get("ref") == head_branch
            ]

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
        return pr

    def create_pull_request(self, repo, *, title, body, head, base, deadline) -> dict:
        deadline.step("create pull request")
        with self.lock:
            data = self._read()
            repo_state = self._repo(data, repo)
            if not repo_state.get("permissions", True):
                raise CommunityError("token lacks authority to create pull requests (HTTP 403)")
            for pr in repo_state["pulls"].values():
                if pr.get("head", {}).get("ref") == head and pr.get("state") == "open":
                    raise CommunityError("an open PR already exists for this branch (HTTP 422)")
            number = repo_state["next_pr"]
            repo_state["next_pr"] = number + 1
            sha = self._tip(repo, head, deadline)
            pr = {
                "number": number,
                "title": title,
                "body": body,
                "state": "open",
                "merged": False,
                "head": {"ref": head, "sha": sha or ""},
                "base": {"ref": base},
                "user": {"login": "dev-transport"},
                "html_url": f"https://example.invalid/{repo}/pull/{number}",
            }
            repo_state["pulls"][str(number)] = pr
            self._write(data)
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
