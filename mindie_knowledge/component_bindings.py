"""[Role] Transfer standard component module commands to managed generations.

[Invariants] Only known module entrypoints transfer ownership. Custom wrappers
stay unchanged; external package metadata supplies version floors, never SHAs.
[Timeouts & Locks] Metadata reads share one 15s budget across interpreters.
No locks are taken; native owners retain their existing idle switch and CAS.
[Side-Effect Policy] Read-only metadata subprocesses and pure candidate mappings.
Never modifies an external environment or publishes an installation binding.
"""
from __future__ import annotations

import time

from semantic_version import Version

from .component_runtime import read_versions

COMPONENT_MODULES = {
    "profiler": ("mindie-profiler", "mindie_profiler"),
    "sim": ("mindie-sim", "mindie_sim"),
    "coordinator": ("mindie-coordinator", "mindie_coordinator.cli"),
    "diagnostics": ("mindie-diagnostics", "mindie_diagnostics.cli"),
}


def managed_commands(adapter):
    """Return standard module bindings; executable filenames prove no ownership."""
    result = {}
    for name, (distribution, module) in COMPONENT_MODULES.items():
        argv = adapter.get("component_commands", {}).get(name)
        if (not isinstance(argv, list) or len(argv) < 3
                or not all(isinstance(value, str) and value for value in argv)
                or argv[-2:] != ["-m", module]):
            continue
        flags = argv[1:-2]
        if flags not in ([], ["-I"], ["-I", "-X", "utf8"], ["-I", "-B", "-X", "utf8"]):
            continue
        result[name] = (distribution, argv[0], module)
    return result


def required_components(adapter, *, timeout=15):
    """Read the external version floor once per interpreter within one budget."""
    deadline = time.monotonic() + timeout
    interpreters = {}
    for distribution, python, _module in managed_commands(adapter).values():
        interpreters.setdefault(python, []).append(distribution)
    versions = {}
    for python, names in interpreters.items():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("bound component metadata budget exhausted")
        versions.update(read_versions(python, names, timeout=remaining))
    return versions


def validate_command_floor(adapter, components, *, timeout=15):
    """Recheck the current binding before a native owner prepares its idle switch."""
    for name, version in required_components(adapter, timeout=timeout).items():
        if name not in components or Version(components[name]["version"]) < Version(version):
            raise ValueError(f"candidate runtime is below the current bound component: {name}")


def rebind_commands(adapter, python, components):
    """Build candidate commands; publication belongs to the native idle transaction."""
    commands = dict(adapter.get("component_commands", {}))
    for name, (distribution, _old_python, module) in managed_commands(adapter).items():
        if distribution not in components:
            raise ValueError(f"candidate runtime omits bound component: {distribution}")
        commands[name] = [str(python), "-I", "-X", "utf8", "-m", module]
    return commands
