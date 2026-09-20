"""Fixtures for community mechanism tests.

Git is real everywhere: remotes are local bare repositories, clones/commits/
pushes run as actual subprocesses. The GitHub API is the file-backed dev
transport — its successes are mechanism evidence, not GitHub acceptance.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def _install_core_documents():
    """Mount the in-flight core documents module exactly as root's integration will.

    The community package delegates to ``mindie_knowledge.loop.documents``. In
    this worktree core has not landed yet, so tests load the sibling core
    worktree's real file under that module name. If it is absent, document-
    dependent tests skip with an explicit integration-dependency reason.
    """
    try:
        import mindie_knowledge.loop.documents  # noqa: F401

        return True
    except ImportError:
        pass
    import importlib.util
    import os

    sibling = os.environ.get(
        "MINDIE_CORE_WORKTREE", "/Users/maoxx241/code/mindie-sharing-20260920/core-kimi"
    )
    candidate = Path(sibling) / "mindie_knowledge/loop/documents.py"
    if not candidate.is_file():
        return False
    spec = importlib.util.spec_from_file_location("mindie_knowledge.loop.documents", candidate)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["mindie_knowledge.loop.documents"] = module
    import mindie_knowledge.loop as _loop

    _loop.documents = module
    return True


CORE_DOCUMENTS = _install_core_documents()
requires_core_documents = pytest.mark.skipif(
    not CORE_DOCUMENTS,
    reason="integration dependency: core mindie_knowledge.loop.documents not available",
)

from mindie_knowledge.community import entrydoc  # noqa: F401  (delegates to core documents)
from mindie_knowledge.community.batch import batch_revision
from mindie_knowledge.community.common import sha256_text
from mindie_knowledge.community.transport import FileTransport

REPO = "acme/npu-knowledge"
PLUGIN_REPO = "acme/mindie-plugin"


def git(argv, cwd=None):
    result = subprocess.run(
        ["git", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(cwd) if cwd else "/tmp",
            "GIT_CONFIG_NOSYSTEM": "1",
        },
    )
    assert result.returncode == 0, f"git {argv}: {result.stderr}"
    return result.stdout.strip()


def make_remote(tmp_path: Path, name: str) -> str:
    """A real bare remote with one initial commit on main."""
    bare = tmp_path / f"{name}.git"
    git(["init", "--bare", "-b", "main", str(bare)])
    work = tmp_path / f"{name}-seed"
    git(["clone", str(bare), str(work)])
    (work / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    git(["add", "README.md"], cwd=work)
    git(["-c", "user.name=seed", "-c", "user.email=seed@example.invalid",
         "commit", "-m", "init"], cwd=work)
    git(["push", "origin", "main"], cwd=work)
    return str(bare)


def make_entry(entry_id="entry-1", domain="npu", title="Container device numbering",
               content="Map the physical device, then number logically from zero.",
               status="active", retirement_reason="", producers=None, revision=None,
               kind="experience", conditions=None, summary="How device numbering works"):
    import hashlib
    import re

    if not re.fullmatch(r"[0-9a-f]{64}", entry_id):
        entry_id = hashlib.sha256(entry_id.encode("utf-8")).hexdigest()
    doc = {
        "schema": "mindie-entry/1",
        "entry_id": entry_id,
        "domain": domain,
        "kind": kind,
        "status": status,
        "title": title,
        "summary": summary,
        "conditions": conditions or {"driver": "cann 8.0"},
        "sources": [],
        "producers": sorted(producers or []),
        "retirement_reason": retirement_reason,
        # Core validates canonical form: no surrounding whitespace on content.
        "content": content.strip(),
    }
    doc["revision"] = revision or entrydoc.revision_of(doc)
    return doc


def entry_file(doc, path=None):
    path = path or f"cases/{doc['entry_id']}.md"
    content = entrydoc.render_entry(doc)
    return {"path": path, "content": content, "sha256": sha256_text(content), "base_sha256": None}


def feedback_file(votes, path="feedback/fb-1.json"):
    content = json.dumps({"schema": "mindie-feedback/1", "votes": votes},
                         ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return {"path": path, "content": content, "sha256": sha256_text(content), "base_sha256": None}


def vote(vote_id, entry_id, revision, rating="up", root_id=None, reason=""):
    return {
        "vote_id": vote_id,
        "root_id": root_id or f"root-{vote_id}",
        "entry_id": entry_id,
        "revision": revision,
        "rating": rating,
        "reason": reason,
    }


def make_batch(batch_id, files, domain="npu", base_commit=None, entry_refs=None,
               summary="device numbering experience", schema="mindie-contribution/1"):
    batch = {
        "schema": schema,
        "batch_id": batch_id,
        "domain": domain,
        "base_commit": base_commit,
        "entry_refs": entry_refs or [],
        "files": files,
        "summary": summary,
    }
    batch["revision"] = batch_revision(files, domain, base_commit, batch["entry_refs"])
    return batch


@pytest.fixture
def remote_url(tmp_path):
    return make_remote(tmp_path, "content")


@pytest.fixture
def settings(tmp_path, remote_url):
    return {
        "schema": "mindie-community-config/1",
        "enabled": True,
        "generation": "g1",
        "enabled_at": 1_700_000_000,
        "repository": REPO,
        "branch": "main",
        "project_roots": [str(tmp_path)],
        "idle_seconds": 300,
        "transport": "file",
        "dev_remotes": {REPO: remote_url},
        "transaction_seconds": 120,
        "operation_limit": 60,
        "token_env": "GH_TOKEN",
        "bot": {},
    }


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "state"


@pytest.fixture
def transport(settings, state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    return FileTransport(state_dir / "dev-github.json", settings["dev_remotes"])


def grok_script(tmp_path: Path, payload: dict, counter: str = "grok-calls") -> list[str]:
    """A stand-in for the maintainer-configured review CLI (argv contract)."""
    script = tmp_path / "fake_grok.py"
    counter_path = tmp_path / counter
    script.write_text(
        "import json, sys, pathlib\n"
        "sys.stdin.read()\n"
        f"count = pathlib.Path({str(counter_path)!r})\n"
        "count.write_text(str(int(count.read_text() or '0') + 1) if count.exists() else '1')\n"
        f"print(json.dumps({json.dumps(payload)}))\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


def grok_calls(tmp_path: Path, counter: str = "grok-calls") -> int:
    path = tmp_path / counter
    return int(path.read_text()) if path.exists() else 0
