"""GitHub transport for opening a contribution PR and reading its status."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import urllib.parse
from typing import Any, Callable, Mapping, Optional, Protocol

from mindie_knowledge.contribution.errors import TransportError

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # Authenticated API requests may never forward a token to another URL.
        return None


class GitHubError(Exception):
    """A GitHub API call failed."""

    def __init__(self, status: int, path: str, detail: str = "") -> None:
        self.status = status
        self.path = path
        super().__init__(f"GitHub API {status} for {path}: {detail}".rstrip())


class GitHubTransport(Protocol):
    def get(self, path: str) -> Any: ...
    def post(self, path: str, body: Mapping[str, Any]) -> Any: ...


def transport_message(exc: GitHubError) -> str:
    if exc.status in (401, 403):
        return f"GitHub permission error ({exc.status}) for {exc.path}"
    if exc.status == 0:
        return f"GitHub network error for {exc.path}: {exc}"
    return f"GitHub API {exc.status} for {exc.path}"

def raise_transport(exc: GitHubError) -> None:
    raise TransportError(transport_message(exc), status=exc.status) from exc


class UrllibContributionGitHub:
    """Authenticated GitHub REST transport. Tests can inject the same interface."""

    def __init__(self, token: str) -> None:
        if not token:
            raise TransportError("missing GITHUB_TOKEN", status=401)
        if any(character.isspace() or ord(character) < 32 for character in token):
            raise TransportError("invalid GitHub token whitespace or control characters", status=401)
        self._token = token

    def _request(self, method: str, path: str, body: Optional[Mapping[str, Any]] = None) -> tuple[Any, dict[str, str]]:
        url = path if path.startswith("http") else API_ROOT + path
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "api.github.com":
            raise TransportError("GitHub API requests must stay on api.github.com")
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "mindie-knowledge-contribution",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=60) as response:
                raw = response.read()
                header_map = {key: value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", "replace").replace(self._token, "[redacted]")
            raise GitHubError(exc.code, path, detail) from exc
        except urllib.error.URLError as exc:
            raise GitHubError(0, path, str(exc)) from exc
        payload: Any = None
        if raw:
            payload = json.loads(raw.decode("utf-8"))
        return payload, header_map

    def get(self, path: str) -> Any:
        payload, _headers = self._request("GET", path)
        return payload

    def post(self, path: str, body: Mapping[str, Any]) -> Any:
        payload, _headers = self._request("POST", path, body)
        return payload


def pull_head_ref(fork: str, upstream: str, branch: str) -> str:
    if fork == upstream:
        return branch
    owner = fork.split("/", 1)[0]
    return f"{owner}:{branch}"

def find_open_pull(
    api: GitHubTransport,
    *,
    upstream: str,
    fork: str,
    branch: str,
) -> dict[str, Any] | None:
    head = pull_head_ref(fork, upstream, branch)
    try:
        payload = api.get(f"/repos/{upstream}/pulls?head={head}&state=open")
    except GitHubError as exc:
        raise_transport(exc)
        raise
    if not isinstance(payload, list):
        raise TransportError("malformed pull listing")
    for item in payload:
        if isinstance(item, Mapping) and isinstance(item.get("number"), int):
            return dict(item)
    return None

def create_pull(
    api: GitHubTransport,
    *,
    upstream: str,
    fork: str,
    branch: str,
    base: str,
    title: str,
    body: str,
    authorize: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    existing = find_open_pull(api, upstream=upstream, fork=fork, branch=branch)
    if existing is not None:
        return existing
    if authorize is not None and not authorize():
        from mindie_knowledge.contribution.consent import ContributionPaused

        raise ContributionPaused("contribution authorization changed before PR creation")
    try:
        created = api.post(
            f"/repos/{upstream}/pulls",
            {
                "title": title,
                "head": pull_head_ref(fork, upstream, branch),
                "base": base,
                "body": body,
            },
        )
    except GitHubError as exc:
        raise_transport(exc)
        raise
    if not isinstance(created, Mapping) or not isinstance(created.get("number"), int):
        raise TransportError("malformed create-pull response")
    return dict(created)
