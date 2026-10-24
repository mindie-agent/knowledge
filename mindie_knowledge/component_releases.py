"""[Role] Discover independent formal component releases for native updaters.

[Invariants] Plugin installation is independent of knowledge activation. Only
official formal tags with matching source package versions can become targets;
selection never downgrades or silently accepts an incomplete catalog.
[Timeouts & Locks] Caller bounds each HTTP read; public ETag cache writes use
atomic replace. Concurrent readers may repeat a read but cannot mix cache bytes.
[Side-Effect Policy] Public release reads and a bounded metadata cache only;
no engine, corpus, transcript, consent or model is accessed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import tomllib
import time
import urllib.parse

from semantic_version import Version

from .runtime_inventory import COMPONENTS, SHA, validate_targets
from .releases_io import _OfficialRedirects
from .release_http import read as http_read

MAX_RESPONSE = 2 * 1024 * 1024


class Catalog:
    def __init__(self, *, cache=None, timeout=10, token=None, budget=120):
        self.cache, self.timeout, self.token = cache, timeout, token
        self.deadline = time.monotonic() + budget

    def remaining(self):
        available = self.deadline - time.monotonic()
        if available <= 0:
            raise TimeoutError("component release discovery budget exhausted")
        return min(self.timeout, available)

    def read(self, path, *, force=False):
        if not path.startswith("/repos/mindie-agent/") or ".." in path:
            raise ValueError("component catalog path is not official")
        raw, _ = http_read("https://api.github.com" + path, maximum=MAX_RESPONSE,
                           timeout=self.remaining(), accept="application/vnd.github+json",
                           redirects=_OfficialRedirects(), directory=self.cache,
                           force=force, credential=self.token)
        return json.loads(raw.decode("utf-8"))

    def latest(self, name, *, force=False):
        repository = COMPONENTS[name]
        prefix = "/repos/" + repository
        release = self.read(prefix + "/releases/latest", force=force)
        if release.get("draft") is not False or release.get("prerelease") is not False:
            raise ValueError(f"component release is not formal: {name}")
        tag = release.get("tag_name", "")
        version = Version(tag.removeprefix("v"))
        if version.prerelease or version.build:
            raise ValueError(f"component release is not stable: {name}")
        assets = {row["name"]: row for row in release.get("assets", [])}
        wheel = next((file for file in assets if file.startswith(name.replace("-", "_") + "-" + str(version) + "-")
                      and file.endswith(".whl")), None)
        sdist = name.replace("-", "_") + "-" + str(version) + ".tar.gz"
        required = {"SHA256SUMS", "benchmark-results.json", "benchmark-results.md", "benchmark-SHA256SUMS", sdist}
        if wheel is None or not required <= set(assets) or any(assets[file].get("size", 0) <= 0 for file in required | {wheel}):
            raise ValueError(f"component release artifacts are incomplete: {name}")
        reference = self.read(prefix + "/git/ref/tags/" + urllib.parse.quote(tag, safe=""))
        target = reference["object"]
        # Published tags may be annotated. Resolve their immutable object,
        # rather than trusting Release.target_commitish (often a branch name).
        for _ in range(4):
            if target.get("type") == "commit":
                break
            if target.get("type") != "tag" or not SHA.fullmatch(target.get("sha", "")):
                raise ValueError(f"component tag does not resolve to source: {name}")
            target = self.read(prefix + "/git/tags/" + target["sha"])["object"]
        revision = target.get("sha", "")
        if target.get("type") != "commit" or not SHA.fullmatch(revision):
            raise ValueError(f"component tag does not resolve to source: {name}")
        selected = self.source(name, revision)
        if Version(selected["version"]) != version:
            raise ValueError(f"component tag and package version differ: {name}")
        sums = self.asset(name, tag, "SHA256SUMS").decode("utf-8")
        checksums = {}
        for line in sums.splitlines():
            digest, filename = line.split(maxsplit=1)
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"component release checksum is invalid: {name}")
            checksums[filename.lstrip("*")] = digest
        expected_files = (required - {"SHA256SUMS"}) | {wheel}
        if not expected_files <= set(checksums):
            raise ValueError(f"component release checksums are incomplete: {name}")
        raw = self.asset(name, tag, "benchmark-results.json")
        if hashlib.sha256(raw).hexdigest() != checksums["benchmark-results.json"]:
            raise ValueError(f"component release benchmark digest differs: {name}")
        benchmark = json.loads(raw.decode("utf-8"))
        valid_source = all(isinstance(benchmark.get(field), dict)
                           and benchmark[field].get("revision") == revision
                           and benchmark[field].get("dirty") is False
                           for field in ("source", "source_after"))
        if (benchmark.get("release_gate") is not True or benchmark.get("version") != str(version)
                or not valid_source):
            raise ValueError(f"component release benchmark does not validate this source: {name}")
        return selected

    def asset(self, name, tag, filename):
        url = f"https://github.com/{COMPONENTS[name]}/releases/download/{urllib.parse.quote(tag, safe='')}/{filename}"
        raw, _ = http_read(url, maximum=MAX_RESPONSE, timeout=self.remaining(),
                           accept="application/octet-stream", redirects=_OfficialRedirects(),
                           directory=self.cache, credential="")
        return raw

    def source(self, name, revision):
        if not SHA.fullmatch(revision):
            raise ValueError("component source must be an immutable commit")
        repository = COMPONENTS[name]
        source = self.read("/repos/" + repository + "/contents/pyproject.toml?ref=" + revision)
        if source.get("encoding") != "base64":
            raise ValueError(f"component source declaration is unavailable: {name}")
        raw = base64.b64decode(source["content"].replace("\n", ""), validate=True)
        digest = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if len(raw) != source["size"] or digest != source["sha"]:
            raise ValueError(f"component source declaration digest differs: {name}")
        project = tomllib.loads(raw.decode("utf-8"))["project"]
        if project["name"] != name:
            raise ValueError(f"component source package differs: {name}")
        return dict(repository=repository, version=str(Version(project["version"])), revision=revision)


def select(installed, latest, *, baseline=None):
    """Keep every installed component and advance only to newer formal releases."""
    validate_targets(installed)
    minimum = dict(installed)
    if baseline:
        validate_targets(baseline)
        for name, target in baseline.items():
            if (name in minimum and target["version"] == minimum[name]["version"]
                    and target["revision"] != minimum[name]["revision"]):
                raise ValueError(f"adapter baseline source changed without a version bump: {name}")
            if name not in minimum or Version(target["version"]) > Version(minimum[name]["version"]):
                minimum[name] = target
    result = {}
    for name, current in minimum.items():
        target = latest(name)
        validate_targets({name: target})
        if (Version(target["version"]) == Version(current["version"])
                and target["revision"] != current["revision"]):
            raise ValueError(f"published component source changed without a version bump: {name}")
        newer = Version(target["version"]) > Version(current["version"])
        result[name] = dict(target if newer else current)
    return result


def runtime_identity(baseline, components):
    """Bind an adapter source receipt to an independently verified runtime plan."""
    if components is None:
        return baseline
    if (set(components) != {"schema", "adapter_source_sha256", "runtime", "lock_sha256"}
            or components.get("schema") != "mindie-component-runtime/1"
            or components.get("adapter_source_sha256") != baseline["source_sha256"]):
        raise ValueError("component runtime belongs to a different adapter source")
    targets = validate_targets(components["runtime"])
    if not set(baseline["runtime"]) <= set(targets):
        raise ValueError("component runtime omits an adapter dependency")
    return dict(baseline, runtime={name: row["revision"] for name, row in targets.items()},
                component_updates=components)
