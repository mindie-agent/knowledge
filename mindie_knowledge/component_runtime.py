"""[Role] Build an immutable component runtime beside the active native runtime.

[Invariants] Published adapter inputs stay unchanged. A separate uv project
locks the selected component combination; only a verified candidate is returned.
Native adapters retain ownership of their existing idle switch and rollback.
[Timeouts & Locks] The caller supplies one enclosing build budget. A nonblocking
OS lock coalesces the same generation; process cleanup has an extra 10s allowance.
[Side-Effect Policy] Writes only under the caller's runtime-generation directory.
No knowledge service, corpus, model, transcript or native configuration is used.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sys
import sysconfig
import time
import tomllib

from .component_releases import runtime_identity
from .runtime_inventory import validate_targets
from .loop.locks import lock_file, unlock_file
from .loop.process import bounded_run


def source_files(source):
    """Native source only; generated host bindings are mutable installation data."""
    source = Path(source)
    excluded = {".git", ".venv", "__pycache__", "node_modules", "dist", "build",
                "config", "host-package", ".pytest_cache"}
    result = []
    for directory, children, names in os.walk(source):
        children[:] = [name for name in children
                       if name not in excluded and not name.endswith(".egg-info")]
        for name in names:
            path = Path(directory) / name
            if path.suffix != ".pyc" and path.is_file():
                result.append(path.relative_to(source))
    return sorted(result, key=str)


def source_identity(source):
    digest, size = hashlib.sha256(), 0
    for relative in source_files(source):
        with (Path(source) / relative).open("rb") as stream:
            raw = stream.read(16 * 1024 * 1024 - size + 1)
        size += len(raw)
        if size > 16 * 1024 * 1024:
            raise ValueError("adapter runtime source exceeds 16 MiB")
        name = relative.as_posix().encode()
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(len(raw).to_bytes(8, "big") + raw)
    return digest.hexdigest()


def source_baseline(source):
    """For native adapters without a product receipt, retain their exact pins."""
    from .runtime_inventory import COMPONENTS
    requirements = Path(source) / "runtime-requirements.txt"
    if requirements.is_file():
        pins = {}
        for line in requirements.read_text(encoding="utf-8").splitlines():
            match = re.fullmatch(r"([\w-]+)\s*@\s*git\+https://github.com/([\w-]+/[\w-]+)@([0-9a-f]{40})", line)
            if match and match[1] in COMPONENTS:
                if COMPONENTS[match[1]] != match[2]:
                    raise ValueError("adapter declares an unofficial component source")
                pins[match[1]] = match[3]
    else:
        project = tomllib.loads((Path(source) / "pyproject.toml").read_text(encoding="utf-8"))
        pins = {name: row["rev"] for name, row in project["tool"]["uv"]["sources"].items()
                if name in COMPONENTS}
    if not pins:
        raise ValueError("adapter declares no component source pins")
    return dict(source_sha256=source_identity(source), runtime=pins)


def discover(python, minimum, *, force=False, catalog=None):
    from .component_releases import Catalog, select
    current = read_inventory(python)
    catalog = catalog or Catalog()
    floor = {name: catalog.source(name, revision) for name, revision in minimum.items()}
    targets = select(current, lambda name: catalog.latest(name, force=force), baseline=floor)
    return dict(installed=current, runtime=targets, changed=targets != current)


def project_text(source, targets, *, adapter_distribution=None):
    validate_targets(targets)
    requirements = [f"{name} @ git+https://github.com/{row['repository']}@{row['revision']}"
                    for name, row in sorted(targets.items())]
    dependencies = list(requirements)
    if adapter_distribution:
        dependencies.append(f"{adapter_distribution} @ {Path(source).resolve().as_uri()}")
    return ("[project]\nname = \"mindie-component-runtime\"\nversion = \"0.0.0\"\n"
            "requires-python = \">=3.11\"\ndependencies = " + json.dumps(dependencies) + "\n\n"
            "[tool.uv]\npackage = false\noverride-dependencies = " + json.dumps(requirements) + "\n")


def generation_key(baseline, targets):
    value = dict(source=baseline["source_sha256"], runtime=targets,
                 abi=sys.implementation.cache_tag, platform=sysconfig.get_platform())
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def read_inventory(python, *, timeout=15):
    script = Path(__file__).with_name("runtime_inventory.py")
    return json.loads(bounded_run([str(python), "-I", str(script)], "", timeout=timeout,
                                  max_output=64 * 1024))


def build(source, root, baseline, targets, *, python=sys.executable,
          adapter_distribution=None, timeout=420, env=None, files=None):
    """Prepare and verify; publication of a native binding stays with its owner."""
    source, root = Path(source).resolve(), Path(root).resolve()
    files = source_files(source) if files is None else [Path(item) for item in files]
    validate_targets(targets)
    if not set(baseline["runtime"]) <= set(targets):
        raise ValueError("component selection omits an adapter dependency")
    generation = root / generation_key(baseline, targets)
    if generation.is_relative_to(source):
        raise ValueError("component generations must be outside the adapter source")
    generation.mkdir(parents=True, exist_ok=True)
    manifest_file = generation / "components.json"
    descriptor = os.open(generation / "prepare.lock", os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        lock_file(descriptor, exclusive=True)
        locked = True
        started = time.monotonic()
        runtime = generation / "runtime"
        selected_python = runtime / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if manifest_file.is_file():
            components = json.loads(manifest_file.read_text(encoding="utf-8"))
            if (components["adapter_source_sha256"] != baseline["source_sha256"]
                    or components["runtime"] != targets):
                raise ValueError("completed component runtime differs from its generation")
        else:
            retained = generation / "source"
            if retained.exists():
                shutil.rmtree(retained)
            size = 0
            for relative in files:
                relative = Path(relative)
                size += (source / relative).stat().st_size
                if size > 16 * 1024 * 1024:
                    raise ValueError("adapter runtime source exceeds 16 MiB")
                destination = retained / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / relative, destination)
            runtime.mkdir(exist_ok=True)
            (runtime / "pyproject.toml").write_text(
                project_text(retained, targets, adapter_distribution=adapter_distribution),
                encoding="utf-8", newline="\n")
            uv = shutil.which("uv")
            if uv is None:
                raise FileNotFoundError("uv is required to prepare a component runtime")
            environment = dict(os.environ if env is None else env)
            environment.pop("PYTHONPATH", None)
            environment.update(UV_PROJECT_ENVIRONMENT=str(runtime / ".venv"), UV_NO_PROGRESS="1",
                               GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
            bounded_run([uv, "sync", "--project", str(runtime), "--no-editable", "--no-dev",
                         "--python", str(python)], "", timeout=timeout - 20,
                        max_output=256 * 1024, child_env=environment)
            components = dict(schema="mindie-component-runtime/1",
                              adapter_source_sha256=baseline["source_sha256"], runtime=targets,
                              lock_sha256=hashlib.sha256((runtime / "uv.lock").read_bytes()).hexdigest())
            temporary = manifest_file.with_suffix(".pending.json")
            temporary.write_text(json.dumps(components, sort_keys=True), encoding="utf-8", newline="\n")
        # A completed marker is not proof that the retained adapter stayed
        # intact. Compare only source inputs; native generated config is outside
        # this selection and belongs to its existing binding transaction.
        retained = generation / "source"
        for relative in files:
            if (source / relative).read_bytes() != (retained / relative).read_bytes():
                raise ValueError("retained component adapter source changed")
        remaining = timeout - (time.monotonic() - started) - 10
        if remaining <= 0:
            raise TimeoutError("component runtime verification budget exhausted")
        script = Path(__file__).with_name("runtime_inventory.py")
        candidate_manifest = manifest_file if manifest_file.exists() else temporary
        actual = json.loads(bounded_run([str(selected_python), "-I", str(script), str(candidate_manifest)], "",
                                       timeout=min(15, remaining), max_output=64 * 1024))
        if candidate_manifest != manifest_file:
            os.replace(candidate_manifest, manifest_file)
        return dict(generation=str(generation), source=str(generation / "source"),
                    python=str(selected_python), components=components,
                    identity=runtime_identity(baseline, components), installed=actual)
    finally:
        # Closing also releases a lock when acquisition itself failed.
        try:
            if locked:
                unlock_file(descriptor)
        finally:
            os.close(descriptor)


def main():
    """Native bootstraps can delegate to the already-installed shared runtime."""
    raw = sys.stdin.buffer.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise ValueError("component update request exceeds its byte limit")
    request = json.loads(raw)
    if request["operation"] == "discover":
        result = discover(request["python"], request.get("minimum", {}), force=request.get("force", False))
    elif request["operation"] == "build":
        result = build(request["source"], request["root"], request["baseline"], request["runtime"],
                       python=request["python"], adapter_distribution=request.get("adapter_distribution"),
                       timeout=request.get("timeout", 420), files=request.get("files"))
    else:
        raise ValueError("unsupported component update operation")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
