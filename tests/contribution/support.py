"""Fakes and local git helpers for contribution tests. No live network."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from mindie_knowledge.contribution.documents import MarkdownDocument
from mindie_knowledge.contribution.github import GitHubError

OWNER_REPO = "owner/mindie-knowledge-corpus"
DEFAULT_BRANCH = "main"


def ordinary_md(title: str, body: str) -> str:
    return MarkdownDocument.from_text(f"# {title}\n\n{body}\n").render()


def init_git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", DEFAULT_BRANCH], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "contrib-test@example.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Contribution Test"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("# fixture corpus\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return sha


class FakeContributionGitHub:
    def __init__(self):
        self.calls = []
        self.pulls = {}
        self.next_pr = 1
        self.network_down = False
        self.auth_fail = False

    def _fail(self, path):
        if self.network_down:
            raise GitHubError(0, path, "network down")
        if self.auth_fail:
            raise GitHubError(401, path, "unauthorized")

    def get(self, path):
        self.calls.append(("GET", path, None))
        self._fail(path)
        parsed = urlparse(path)
        if parsed.path.endswith("/pulls"):
            query = parse_qs(parsed.query)
            branch = (query.get("head") or [""])[0].split(":")[-1]
            state = (query.get("state") or ["open"])[0]
            return [dict(p) for p in self.pulls.values()
                    if p["head"]["ref"] == branch and p["state"] == state]
        if "/pulls/" in parsed.path:
            return dict(self.pulls[int(parsed.path.rsplit("/", 1)[-1])])
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, body):
        self.calls.append(("POST", path, dict(body)))
        self._fail(path)
        if not path.endswith("/pulls"):
            raise AssertionError(f"unexpected POST {path}")
        number = self.next_pr
        self.next_pr += 1
        branch = body["head"].split(":")[-1]
        pull = {"number": number, "state": "open", "title": body["title"],
                "head": {"ref": branch, "sha": hashlib.sha1(branch.encode()).hexdigest()},
                "html_url": f"https://github.com/{OWNER_REPO}/pull/{number}"}
        self.pulls[number] = pull
        return dict(pull)
