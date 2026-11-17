"""[Role] Read installed first-party runtime identities without importing engines.

[Invariants] Versions and VCS receipts describe the selected interpreter, not
an adapter's original dependency lock. Default v1 calls expose the exact 9-entry
`COMPONENTS` set for deployed plugins; `include_substrate=True` (`COMPONENTS_V2`)
adds `"mindie-harness": "mindie-agent/harness"`, and `allow_wheel_provenance=True`
reports wheel archive digests honestly (`provenance="wheel"`) without fabricating
Git commit SHAs.
[Timeouts & Locks] Local package metadata only; no waits, network or database.
[Side-Effect Policy] Read-only. Direct execution prints JSON for native updaters.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import sys
from pathlib import Path

COMPONENTS = {
    "mindie-knowledge": "mindie-agent/knowledge",
    "remote-dev": "mindie-agent/remote-dev",
    "mindie-diagnostics": "mindie-agent/diagnostics",
    "mindie-debugger": "mindie-agent/debugger",
    "mindie-profiler": "mindie-agent/profiler",
    "mindie-sim": "mindie-agent/sim",
    "npu-top": "mindie-agent/npu-top",
    "mindie-coordinator": "mindie-agent/coordinator",
    "mindie-bench": "mindie-agent/mindie-bench",
}
SUBSTRATE_COMPONENTS = {
    "mindie-harness": "mindie-agent/harness",
}
COMPONENTS_V2 = {
    **COMPONENTS,
    **SUBSTRATE_COMPONENTS,
}
SHA = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def component_registry(*, include_substrate: bool = False) -> dict[str, str]:
    """Return the v1 component map by default, or v2 including `mindie-harness`."""
    return dict(COMPONENTS_V2 if include_substrate else COMPONENTS)


def _wheel_provenance(name: str, repository: str, distribution) -> dict[str, str]:
    raw = distribution.read_text("direct_url.json")
    direct = json.loads(raw) if raw else {}
    hashes = (direct.get("archive_info") or {}).get("hashes") or {}
    archive_sha256 = hashes.get("sha256")
    if isinstance(archive_sha256, str) and SHA256.fullmatch(archive_sha256):
        return {
            "repository": repository,
            "version": distribution.version,
            "provenance": "wheel",
            "archive_sha256": archive_sha256,
        }
    record_raw = distribution.read_text("RECORD")
    if record_raw:
        return {
            "repository": repository,
            "version": distribution.version,
            "provenance": "wheel",
            "record_sha256": hashlib.sha256(record_raw.encode("utf-8")).hexdigest(),
        }
    raise ValueError(f"{name} has no official immutable installation receipt")


def inventory(*, include_substrate: bool = False, allow_wheel_provenance: bool = False):
    registry = COMPONENTS_V2 if include_substrate else COMPONENTS
    result = {}
    for name, repository in registry.items():
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        raw = distribution.read_text("direct_url.json")
        direct = json.loads(raw) if raw else {}
        revision = (direct.get("vcs_info") or {}).get("commit_id")
        official_vcs = (
            direct.get("url", "").removesuffix(".git") == "https://github.com/" + repository
            and isinstance(revision, str)
            and SHA.fullmatch(revision) is not None
        )
        if official_vcs:
            entry = dict(repository=repository, version=distribution.version, revision=revision)
            if allow_wheel_provenance:
                entry["provenance"] = "vcs"
            result[name] = entry
            continue
        if allow_wheel_provenance and not (direct.get("dir_info") or {}).get("editable"):
            result[name] = _wheel_provenance(name, repository, distribution)
            continue
        raise ValueError(f"{name} has no official immutable installation receipt")
    return result


def versions(names, *, include_substrate: bool = False):
    """Read version floors only; local wheels do not establish source identity."""
    registry = COMPONENTS_V2 if include_substrate else COMPONENTS
    result = {}
    for name in names:
        if name not in registry:
            raise ValueError(f"unsupported component version request: {name}")
        result[name] = importlib.metadata.version(name)
    return result


def validate_targets(targets, *, include_substrate: bool = False):
    registry = COMPONENTS_V2 if include_substrate else COMPONENTS
    if not isinstance(targets, dict) or not targets:
        raise ValueError("component runtime has no selected components")
    for name, target in targets.items():
        if name not in registry or not isinstance(target, dict):
            raise ValueError("component runtime contains an unsupported component")
        if (set(target) != {"repository", "version", "revision"}
                or target["repository"] != registry[name]
                or not isinstance(target["version"], str)
                or not re.fullmatch(r"\d+\.\d+\.\d+", target["version"])
                or not isinstance(target["revision"], str) or not SHA.fullmatch(target["revision"])):
            raise ValueError(f"invalid selected component identity: {name}")
    return targets


def verify(targets, *, include_substrate: bool = False):
    validate_targets(targets, include_substrate=include_substrate)
    actual = inventory(include_substrate=include_substrate)
    for name, target in targets.items():
        if actual.get(name) != target:
            raise ValueError(f"installed component differs from its selected release: {name}")
    return actual


def verify_lock(components, root=None):
    root = Path(root) if root is not None else Path(sys.prefix).parent
    actual = hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest()
    if actual != components["lock_sha256"]:
        raise ValueError("component runtime lock differs from its validated receipt")


def _main(argv: list[str] | None = None) -> dict:
    args = list(sys.argv[1:] if argv is None else argv)
    include_substrate = False
    allow_wheel = False
    filtered: list[str] = []
    for item in args:
        if item == "--include-substrate":
            include_substrate = True
        elif item == "--allow-wheel-provenance":
            allow_wheel = True
        else:
            filtered.append(item)
    if filtered and filtered[0] == "--versions":
        return versions(filtered[1:], include_substrate=include_substrate)
    if filtered:
        manifest = json.loads(Path(filtered[0]).read_text(encoding="utf-8"))
        verify_lock(manifest)
        use_v2 = include_substrate or manifest.get("schema") == "mindie-component-runtime/2"
        return verify(manifest["runtime"], include_substrate=use_v2)
    return inventory(include_substrate=include_substrate, allow_wheel_provenance=allow_wheel)


if __name__ == "__main__":
    print(json.dumps(_main(), sort_keys=True))
