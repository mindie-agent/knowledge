"""Fixtures for community mechanism tests.

Git is real everywhere: remotes are local bare repositories, clones/commits/
pushes run as actual subprocesses. The GitHub API is the file-backed dev
transport — its successes are mechanism evidence, not GitHub acceptance.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


from mindie_knowledge.publication_contract import make_contract, render_contract
from mindie_knowledge.loop import documents  # integrated package is required

from mindie_knowledge.community import entrydoc  # noqa: F401  (delegates to core documents)
from mindie_knowledge.community.batch import batch_revision
from mindie_knowledge.community.common import sha256_text
from mindie_knowledge.community.transport import FileTransport

REPO = "acme/npu-knowledge"
PLUGIN_REPO = "acme/mindie-plugin"


def git(argv, cwd=None):
    result = subprocess.run(
        ["git", "-c", "core.longpaths=true", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        },
    )
    assert result.returncode == 0, f"git {argv}: {result.stderr}"
    return result.stdout.strip()


def commit_tree_file(repository, parent, path, content, *, mode="100644"):
    """Add a Git path without asking the host filesystem to represent it."""
    def object_git(*args, data=None):
        result = subprocess.run(
            ["git", *args], cwd=repository, input=data, capture_output=True,
            env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                 "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"},
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        return result.stdout

    blob = object_git("hash-object", "-w", "--stdin", data=content).strip()

    def replace(tree, parts):
        records = [record for record in object_git("ls-tree", "-z", tree).split(b"\0") if record]
        entries = {record.partition(b"\t")[2]: record for record in records}
        name = parts[0]
        if len(parts) == 1:
            entries[name] = mode.encode("ascii") + b" blob " + blob + b"\t" + name
        else:
            child = entries[name].partition(b"\t")[0].split()[2].decode("ascii")
            updated = replace(child, parts[1:])
            entries[name] = b"040000 tree " + updated + b"\t" + name
        return object_git("mktree", "-z", data=b"\0".join(entries.values()) + b"\0").strip()

    tree = replace(parent + "^{tree}", path.encode("utf-8").split(b"/"))
    return object_git("commit-tree", tree.decode("ascii"), "-p", parent,
                      data=b"Add unsupported task path\n").strip().decode("ascii")


def make_remote(tmp_path: Path, name: str) -> str:
    """A real bare remote with one initial commit on main."""
    bare = tmp_path / f"{name}.git"
    git(["init", "--bare", "-b", "main", str(bare)])
    work = tmp_path / f"{name}-seed"
    git(["clone", str(bare), str(work)])
    (work / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    (work / "publication-contract.json").write_text(render_contract(make_contract("npu", "a" * 40)), encoding="utf-8")
    git(["add", "README.md", "publication-contract.json"], cwd=work)
    git(["-c", "user.name=seed", "-c", "user.email=seed@example.invalid",
         "commit", "-m", "init"], cwd=work)
    git(["push", "origin", "main"], cwd=work)
    return str(bare)


# Test fixtures use real task packages produced by the file authority. The
# registry is test-local convenience for expanding an index descriptor into
# its complete package; none of these bodies enter a product database.
_PACKAGES = {}
_INDEXES = {}


def make_entry(entry_id="entry-1", domain="npu", title="Container device numbering",
               content="Map the physical device, then number logically from zero.",
               revision=None, kind="experience", conditions=None,
               summary="How device numbering works"):
    import hashlib
    import re
    import tempfile
    from mindie_knowledge.materials import MaterialStore

    if not re.fullmatch(r"[0-9a-f]{64}", entry_id):
        entry_id = hashlib.sha256(entry_id.encode("utf-8")).hexdigest()
    doc = documents.make_entry(entry_id=entry_id, domain=domain, kind=kind,
                              title=title, summary=summary, content=content,
                              conditions=conditions or {"driver": "cann 8.0"})
    with tempfile.TemporaryDirectory(prefix="mindie-package-fixture-") as directory:
        materials = MaterialStore(Path(directory), domain)
        doc = materials.put_document(doc)
        package = materials.export_task(entry_id, revision=doc["revision"])
    _PACKAGES[doc["revision"]] = package
    _INDEXES[sha256_text(package["files"]["index.md"])] = package
    if revision is not None:
        doc["revision"] = revision
    return doc


def entry_file(doc, path=None):
    package = _PACKAGES[doc["revision"]]
    path = path or f"tasks/{doc['entry_id']}/index.md"
    content = package["files"]["index.md"]
    return {"path": path, "content": content, "sha256": sha256_text(content), "base_sha256": None}


def package_files(doc):
    package = _PACKAGES[doc["revision"]]
    return [dict(path=f"tasks/{doc['entry_id']}/{path}", content=content,
                 sha256=sha256_text(content), base_sha256=None)
            for path, content in package["files"].items()]


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
    expanded = []
    refs = list(entry_refs or [])
    for item in files:
        package = _INDEXES.get(sha256_text(item.get("content", "")))
        if package is None or not item["path"].endswith("/index.md"):
            expanded.append(item)
            continue
        prefix = f"tasks/{package['task_id']}/"
        prior = _INDEXES.get(item.get("base_sha256"))
        prior_files = prior["files"] if prior else {}
        expanded.append(item)
        for path, content in package["files"].items():
            if path == "index.md":
                continue
            expanded.append(dict(path=prefix + path, content=content,
                                 sha256=sha256_text(content),
                                 base_sha256=sha256_text(prior_files[path]) if path in prior_files else None))
        for path, content in prior_files.items():
            if path not in package["files"]:
                expanded.append(dict(path=prefix + path, content=None, sha256=None,
                                     base_sha256=sha256_text(content), delete=True))
        ref = f"mindie://{domain}/{package['task_id']}@{package['revision']}"
        if ref not in refs:
            refs.append(ref)
    files = expanded
    entry_refs = refs
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
    # The live gate requires a real, validated shared config file on disk.
    data = {
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
    config = tmp_path / "community.json"
    config.write_text(json.dumps(data), encoding="utf-8")
    data["config_path"] = str(config.resolve())
    return data


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "state"


@pytest.fixture
def transport(settings, state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    return FileTransport(state_dir / "dev-github.json", settings["dev_remotes"])
