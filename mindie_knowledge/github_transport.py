"""GitHub CLI transport, reusing the user's existing authentication.

Credentials stay in the environment or the gh credential store. Repository
names and asset paths are data passed as separate arguments, never shell code.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping


def repository_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("expected a GitHub owner/repository name")
    return value


def gh(args: list[str], *, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, encoding="utf-8",
        timeout=timeout, env={**os.environ, "GH_PROMPT_DISABLED": "1"},
    )
    if check and result.returncode:
        raise RuntimeError(redact_credentials((result.stderr or "GitHub operation failed").strip())[:1200])
    return result


def github_token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        try:
            result = gh(["auth", "token"], check=False, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("GitHub authentication unavailable; set GH_TOKEN/GITHUB_TOKEN or sign in with gh auth login") from exc
        if result.returncode or not result.stdout.strip():
            raise RuntimeError("GitHub authentication unavailable; set GH_TOKEN/GITHUB_TOKEN or sign in with gh auth login")
        token = result.stdout.strip()
    if any(character.isspace() or ord(character) < 32 for character in token):
        raise RuntimeError("GitHub token contains invalid whitespace or control characters")
    return token


def api(path: str) -> Any:
    """Read JSON for the separate maintainer release publisher."""
    return json.loads(gh(["api", path]).stdout)


def redact_credentials(text: str, *, env: Mapping[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    secrets = [values.get(name, "") for name in ("GH_TOKEN", "GITHUB_TOKEN")]
    for key, value in values.items():
        if key.startswith("GIT_CONFIG_VALUE_") and value.startswith("Authorization: "):
            secrets.extend([value, value.removeprefix("Authorization: "), value.rsplit(" ", 1)[-1]])
            if value.startswith("Authorization: Basic "):
                try:
                    secrets.append(base64.b64decode(value.rsplit(" ", 1)[-1], validate=True).decode().split(":", 1)[1])
                except (ValueError, IndexError, UnicodeError):
                    pass
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def git_environment(token: str) -> dict[str, str]:
    """Authenticate HTTPS in one child process, without URL/argv/config secrets."""
    env = dict(os.environ)
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    if not 0 <= count <= 100:
        raise ValueError("invalid inherited Git environment configuration")
    authorization = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    options = [
        ("http.https://github.com/.extraHeader", ""),
        ("http.https://github.com/.extraHeader", f"Authorization: Basic {authorization}"),
        ("credential.https://github.com.helper", ""),
        ("http.followRedirects", "false"),
        ("url.https://github.com/.insteadOf", "git@github.com:"),
        ("url.https://github.com/.insteadOf", "ssh://git@github.com/"),
    ]
    for index, (key, value) in enumerate(options, count):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(count + len(options))
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Curl/Git debugging can dump request headers to inherited trace files.
    for key in list(env):
        if key.startswith("GIT_TRACE") or key == "GIT_CURL_VERBOSE":
            env.pop(key)
    return env


def ensure_fork(upstream: str, directory: Path, *, github_user: str | None = None,
                authorize: Callable[[], bool] | None = None) -> dict[str, str]:
    """Create/reuse the user's fork and one package-owned contribution clone."""
    from mindie_knowledge.contribution.consent import ContributionPaused
    from mindie_knowledge.contribution.github import GitHubError, UrllibContributionGitHub
    from mindie_knowledge.contribution.gitops import run_git

    def require_consent():
        if authorize is not None and not authorize():
            raise ContributionPaused("community participation changed during contribution setup")

    require_consent()
    upstream = repository_name(upstream)
    token = github_token()
    github = UrllibContributionGitHub(token)
    identity = github.get("/user")
    owner = identity["login"]
    if github_user is not None and github_user.lower() != owner.lower():
        raise ValueError("authenticated GitHub user differs from the confirmed personal user")
    fork = repository_name(f"{owner}/{upstream.split('/')[1]}")
    if fork != upstream:
        try:
            metadata = github.get(f"/repos/{fork}")
        except GitHubError as exc:
            if exc.status != 404:
                raise
            require_consent()
            metadata = github.post(f"/repos/{upstream}/forks", {})
        if metadata.get("parent", {}).get("full_name", "").lower() != upstream.lower():
            raise RuntimeError("the matching personal repository is not a fork of the configured corpus")
    metadata = github.get(f"/repos/{upstream}")
    branch = metadata["default_branch"]
    directory = Path(directory).resolve()
    directory.parent.mkdir(parents=True, exist_ok=True)
    if not directory.exists():
        require_consent()
        run_git(directory.parent, ["clone", f"https://github.com/{fork}.git", str(directory)],
                env=git_environment(token))

    origin = run_git(directory, ["remote", "get-url", "origin"]).stdout.strip()
    normalized = origin.removesuffix(".git").replace("git@github.com:", "https://github.com/")
    if normalized.lower() != f"https://github.com/{fork}".lower():
        raise RuntimeError("contribution checkout origin differs from the configured fork")
    remote = run_git(directory, ["remote", "get-url", "upstream"], check=False)
    wanted = f"https://github.com/{upstream}.git"
    if remote.returncode:
        run_git(directory, ["remote", "add", "upstream", wanted])
    elif remote.stdout.strip().removesuffix(".git").replace("git@github.com:", "https://github.com/").lower() != wanted.removesuffix(".git").lower():
        raise RuntimeError("contribution checkout upstream differs from the configured corpus")
    if isinstance(identity.get("id"), int) and not isinstance(identity["id"], bool):
        for key, value in (("user.name", owner),
                           ("user.email", f"{identity['id']}+{owner}@users.noreply.github.com")):
            if run_git(directory, ["config", "--get", key], check=False).returncode:
                run_git(directory, ["config", "--local", key, value])
    return {"upstream": upstream, "fork": fork, "default_branch": branch, "git_repo": str(directory)}
