"""[Role] Read installed first-party runtime identities without importing engines.

[Invariants] Versions and VCS receipts describe the selected interpreter, not
an adapter's original dependency lock. Only fixed first-party repositories count.
[Timeouts & Locks] Local package metadata only; no waits, network or database.
[Side-Effect Policy] Read-only. Direct execution prints JSON for native updaters.
"""
from __future__ import annotations

import importlib.metadata
import hashlib
import json
from pathlib import Path
import re
import sys

COMPONENTS = {
    "mindie-knowledge": "mindie-agent/knowledge",
    "remote-dev": "mindie-agent/remote-dev",
    "mindie-diagnostics": "mindie-agent/diagnostics",
    "mindie-profiler": "mindie-agent/profiler",
    "mindie-sim": "mindie-agent/sim",
    "npu-top": "mindie-agent/npu-top",
    "mindie-coordinator": "mindie-agent/coordinator",
    "mindie-bench": "mindie-agent/mindie-bench",
}
SHA = re.compile(r"[0-9a-f]{40}\Z")


def inventory():
    result = {}
    for name, repository in COMPONENTS.items():
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        raw = distribution.read_text("direct_url.json")
        direct = json.loads(raw) if raw else {}
        revision = (direct.get("vcs_info") or {}).get("commit_id")
        if (direct.get("url", "").removesuffix(".git") != "https://github.com/" + repository
                or not isinstance(revision, str) or SHA.fullmatch(revision) is None):
            raise ValueError(f"{name} has no official immutable installation receipt")
        result[name] = dict(repository=repository, version=distribution.version, revision=revision)
    return result


def validate_targets(targets):
    if not isinstance(targets, dict) or not targets:
        raise ValueError("component runtime has no selected components")
    for name, target in targets.items():
        if name not in COMPONENTS or not isinstance(target, dict):
            raise ValueError("component runtime contains an unsupported component")
        if (set(target) != {"repository", "version", "revision"}
                or target["repository"] != COMPONENTS[name]
                or not isinstance(target["version"], str)
                or not re.fullmatch(r"\d+\.\d+\.\d+", target["version"])
                or not isinstance(target["revision"], str) or not SHA.fullmatch(target["revision"])):
            raise ValueError(f"invalid selected component identity: {name}")
    return targets


def verify(targets):
    validate_targets(targets)
    actual = inventory()
    for name, target in targets.items():
        if actual.get(name) != target:
            raise ValueError(f"installed component differs from its selected release: {name}")
    return actual


def verify_lock(components, root=None):
    root = Path(root) if root is not None else Path(sys.prefix).parent
    actual = hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest()
    if actual != components["lock_sha256"]:
        raise ValueError("component runtime lock differs from its validated receipt")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        verify_lock(manifest)
        value = verify(manifest["runtime"])
    else:
        value = inventory()
    print(json.dumps(value, sort_keys=True))
