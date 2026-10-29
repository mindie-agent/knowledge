"""Native command ownership, external floors, and bounded metadata discovery."""
from copy import deepcopy
from pathlib import Path

import pytest

from mindie_knowledge import component_bindings as bindings


def test_rebinding_moves_standard_modules_and_preserves_explicit_wrappers():
    adapter = {"component_commands": {
        "profiler": ["/old/bin/python", "-I", "-m", "mindie_profiler"],
        "sim": ["/old/bin/python", "-m", "mindie_sim"],
        "coordinator": ["/custom/bin/coordinator-wrapper", "--project", "chosen"],
        "diagnostics": ["/old/bin/mindie-diagnostics"],
    }}
    before = deepcopy(adapter)
    components = {"mindie-profiler": {"version": "0.5.0"},
                  "mindie-sim": {"version": "0.7.1"}}

    candidate_python = Path("/managed/bin/python")
    result = bindings.rebind_commands(adapter, candidate_python, components)

    assert result["profiler"] == [str(candidate_python), "-I", "-X", "utf8", "-m", "mindie_profiler"]
    assert result["sim"] == [str(candidate_python), "-I", "-X", "utf8", "-m", "mindie_sim"]
    assert result["coordinator"] == adapter["component_commands"]["coordinator"]
    assert result["diagnostics"] == adapter["component_commands"]["diagnostics"]
    assert adapter == before
    with pytest.raises(ValueError, match="omits bound component: mindie-sim"):
        bindings.rebind_commands(adapter, "/candidate/bin/python", {"mindie-profiler": components["mindie-profiler"]})


def test_metadata_reads_share_interpreter_and_total_budget(monkeypatch):
    adapter = {"component_commands": {
        "profiler": ["/shared/python", "-m", "mindie_profiler"],
        "sim": ["/shared/python", "-I", "-X", "utf8", "-m", "mindie_sim"],
        "diagnostics": ["/other/python", "-I", "-B", "-X", "utf8", "-m", "mindie_diagnostics.cli"],
        "coordinator": ["/custom/wrapper"],
    }}
    clock = [0]
    calls = []
    versions = {"mindie-profiler": "0.5.0", "mindie-sim": "0.7.1", "mindie-diagnostics": "0.5.4"}
    monkeypatch.setattr(bindings.time, "monotonic", lambda: clock[0])

    def metadata(python, names, *, timeout):
        calls.append((python, names, timeout))
        clock[0] += 6
        return {name: versions[name] for name in names}

    monkeypatch.setattr(bindings, "read_versions", metadata)
    assert bindings.required_components(adapter) == versions
    assert calls == [
        ("/shared/python", ["mindie-profiler", "mindie-sim"], 15),
        ("/other/python", ["mindie-diagnostics"], 9),
    ]

    clock[0] = 0
    calls.clear()
    with pytest.raises(TimeoutError, match="metadata budget exhausted"):
        bindings.required_components(adapter, timeout=5)
    assert calls == [("/shared/python", ["mindie-profiler", "mindie-sim"], 5)]


def test_activation_rechecks_version_floor_and_does_not_turn_failure_into_empty_inventory(monkeypatch):
    adapter = {"component_commands": {"profiler": ["/external/python", "-m", "mindie_profiler"]}}
    version = ["0.5.0"]

    def metadata(python, names, *, timeout):
        assert python == "/external/python" and names == ["mindie-profiler"]
        return {"mindie-profiler": version[0]}

    monkeypatch.setattr(bindings, "read_versions", metadata)
    candidate = {"mindie-profiler": {"version": "0.5.0"}}
    assert bindings.required_components(adapter) == {"mindie-profiler": "0.5.0"}
    bindings.validate_command_floor(adapter, candidate)
    version[0] = "0.5.1"
    with pytest.raises(ValueError, match="below the current bound component: mindie-profiler"):
        bindings.validate_command_floor(adapter, candidate)

    def missing(*args, **kwargs):
        raise FileNotFoundError("external interpreter is unavailable")

    monkeypatch.setattr(bindings, "read_versions", missing)
    with pytest.raises(FileNotFoundError, match="external interpreter is unavailable"):
        bindings.required_components(adapter)
