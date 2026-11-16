"""Real-directory Tier 0/1 coverage for shared storage lifecycle and reclamation.

Every case runs under an isolated HOME/config/data/TMPDIR; no host installation
is read or deleted. Subprocess and lock cases use real OS processes, never a
mocked process table, so the liveness claims in the module headers are tested.
"""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time

import pytest

from mindie_knowledge import _host_exec, component_runtime, release_http
from mindie_knowledge import filesystem
from mindie_knowledge.loop import process, service_host, summary_scratch
from mindie_knowledge.loop.locks import lock_file, unlock_file
from mindie_knowledge.storage import leases, legacy, lifecycle, uv_cache
from mindie_knowledge.storage.lifecycle import (
    configure_same_volume_uv_cache, prune_component_runtimes, retire_directory,
    sweep_trash,
)


@pytest.fixture(autouse=True)
def isolated_machine_home(tmp_path, monkeypatch):
    """Pin HOME and every MindIE root so hidden-pin scanners see only this case."""
    home = tmp_path / "home"
    home.mkdir()
    scratch = tmp_path / "tmp"
    scratch.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("MINDIE_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("MINDIE_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_ROOT", str(tmp_path / "diagnostics"))
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    return home


def _key(harness: str, index: int) -> str:
    return hashlib.sha256(f"{harness}-{index}".encode()).hexdigest()


def _runtime(root: Path, harness: str, index: int, *, verified=True, mtime=None):
    entry = root / _key(harness, index)
    (entry / "runtime" / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (entry / "runtime" / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    if verified:
        (entry / "components.json").write_text("{}", encoding="utf-8")
    if mtime is not None:
        os.utime(entry, (mtime, mtime))
        if verified:
            os.utime(entry / "components.json", (mtime, mtime))
    return entry


def _bad_staging(root: Path, name: str, *, age: float):
    entry = root / name
    (entry / "runtime" / ".venv" / "Lib" / "site-packages").mkdir(parents=True)
    (entry / "runtime" / ".venv" / "Lib" / "site-packages" / "big.py").write_text(
        "x" * 64, encoding="utf-8")
    stamp = time.time() - age
    os.utime(entry, (stamp, stamp))
    return entry


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _kimi_fixture(base: Path):
    config, data, home = base / "config", base / "data", base / "home"
    state = data / "kimi" / "adapter-state"
    update = state / "update"
    generations, launch, receipts = update / "generations", update / "launch", update / "receipts"
    for directory in (generations, launch, receipts, state / "component-runtimes"):
        directory.mkdir(parents=True, exist_ok=True)
    shas = [hashlib.sha256(f"gen-{i}".encode()).hexdigest() for i in range(10)]
    for index in range(6):
        entry = generations / shas[index]
        (entry / ".venv" / "bin").mkdir(parents=True)
        (entry / "config").mkdir()
        os.utime(entry, (1000 + index, 1000 + index))
    (generations / "bootstrap").mkdir()
    (generations / "bootstrap" / "package").mkdir()
    for index in range(10):
        entry = launch / shas[index]
        entry.mkdir()
        (entry / "mindie_launch.py").write_text("pass\n", encoding="utf-8")
        os.utime(entry, (1000 + index, 1000 + index))
    (launch / "bootstrap").mkdir()
    for index in range(10):
        entry = receipts / (shas[index] + ".json")
        entry.write_text(json.dumps({"sha": shas[index]}), encoding="utf-8")
        os.utime(entry, (1000 + index, 1000 + index))
    runtimes = [_runtime(state / "component-runtimes", "kimi", i, mtime=1000 + i)
                for i in range(10)]
    broken = _bad_staging(state / "component-runtimes", "f" * 64, age=7200)
    staging = _bad_staging(state / "component-runtimes", "e" * 64, age=10)
    current = {
        "schema": "mindie-kimi-update/1", "sha": shas[5],
        "generation": str(generations / shas[5]),
        "python": str(runtimes[5] / "runtime" / ".venv" / "bin" / "python"),
        "adapter_config": str(config / "kimi.json"),
    }
    (update / "current.json").write_text(json.dumps(current), encoding="utf-8")
    previous_receipt = {
        "sha": shas[5],
        "previous": {"sha": shas[8], "generation": str(runtimes[8]),
                     "python": str(runtimes[8] / "runtime" / ".venv" / "bin" / "python")},
    }
    (receipts / (shas[5] + ".json")).write_text(json.dumps(previous_receipt), encoding="utf-8")
    front = home / ".kimi-code" / "plugins" / "managed" / "mindie-agent"
    front.mkdir(parents=True)
    (front / "mindie-front").write_text(
        "#!/bin/sh\nexec " + str(runtimes[3] / "runtime" / ".venv" / "bin" / "python") + " \"$@\"\n",
        encoding="utf-8")
    plist = home / "Library" / "LaunchAgents" / "agent.mindie.kimi-update.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text(
        "<plist><dict><key>ProgramArguments</key><array><string>"
        + str(launch / shas[2] / "mindie_launch.py") + "</string></array></dict></plist>",
        encoding="utf-8")
    return {
        "state": state, "update": update, "generations": generations,
        "launch": launch, "receipts": receipts, "shas": shas,
        "runtimes": runtimes, "broken": broken, "staging": staging,
    }


def _codex_fixture(base: Path):
    codex = base / "codex"
    plugin = codex / "plugins" / "data" / "mindie-agent-mindie-agent"
    runtimes_root = plugin / "component-runtimes"
    runtimes = [_runtime(runtimes_root, "codex", i, mtime=1000 + i) for i in range(10)]
    broken = _bad_staging(runtimes_root, "d" * 64, age=7200)
    staging = _bad_staging(runtimes_root, "c" * 64, age=10)
    for index in range(3):
        entry = plugin / "runtimes" / f"runtime-{index}"
        (entry / "venv").mkdir(parents=True)
        (entry / "runtime.json").write_text("{}", encoding="utf-8")
        os.utime(entry, (1000 + index, 1000 + index))
    releases = plugin / "releases"
    for version in ("0.1.20", "0.1.21", "0.1.22"):
        entry = releases / version
        entry.mkdir(parents=True)
        (entry / "runtime.json").write_text("{}", encoding="utf-8")
    old_log = releases / "prepare-old.log"
    old_log.write_text("log\n", encoding="utf-8")
    os.utime(old_log, (time.time() - 7200, time.time() - 7200))
    fresh_log = releases / "prepare-new.log"
    fresh_log.write_text("log\n", encoding="utf-8")
    updates = base / "config" / "codex.updates"
    updates.mkdir(parents=True)
    (updates / "native.json").write_text(json.dumps({"native_version": "0.1.22"}),
                                         encoding="utf-8")
    for index in range(10):
        entry = updates / f"superseded-{index:02d}.json"
        entry.write_text("{}", encoding="utf-8")
        os.utime(entry, (1000 + index, 1000 + index))
    return {
        "plugin": plugin, "runtimes": runtimes, "broken": broken,
        "staging": staging, "releases": releases,
        "old_log": old_log, "fresh_log": fresh_log, "updates": updates,
    }


def _cc_fixture(base: Path, home: Path):
    data = base / "data"
    for index in range(4):
        _runtime(data / "cc" / "adapter-state" / "component-runtimes", "cc-state", index,
                 mtime=1000 + index)
    _bad_staging(data / "cc" / "adapter-state" / "component-runtimes", "9" * 64, age=7200)
    _bad_staging(data / "cc" / "adapter-state" / "component-runtimes", "8" * 64, age=10)
    cc_runtimes = data / "runtimes" / "cc"
    runtimes = []
    for index in range(5):
        entry = cc_runtimes / _key("cc", index)
        (entry / "bin").mkdir(parents=True)
        (entry / "bin" / "python").write_text("", encoding="utf-8")
        ready = entry.with_name(entry.name + ".ready.json")
        ready.write_text("{}", encoding="utf-8")
        os.utime(entry, (1000 + index, 1000 + index))
        os.utime(ready, (1000 + index, 1000 + index))
        runtimes.append(entry)
    installations = data / "cc" / "adapter-state" / "installations"
    for index in range(5):
        entry = installations / f"installation-{index}"
        entry.mkdir(parents=True)
        (entry / "cc.adapter.json").write_text("{}", encoding="utf-8")
        os.utime(entry, (1000 + index, 1000 + index))
    claude = base / "claude-data" / "releases" / "0.1.23"
    claude.mkdir(parents=True)
    (claude / "entry.py").write_text(
        "PYTHON = " + json.dumps(str(runtimes[1] / "bin" / "python")) + "\n", encoding="utf-8")
    return {"runtimes": runtimes, "installations": installations,
            "claude_data": base / "claude-data"}


def _wb_fixture(base: Path, home: Path, codex_runtimes):
    config, data = base / "config", base / "data"
    (config / "wb.json").write_text(json.dumps({
        "state_dir": str(data / "wb-state"),
        "python": str(codex_runtimes[4] / "runtime" / ".venv" / "bin" / "python"),
    }), encoding="utf-8")
    runtimes = [_runtime(data / "wb-state" / "component-runtimes", "wb", i, mtime=1000 + i)
                for i in range(4)]
    broken = _bad_staging(data / "wb-state" / "component-runtimes", "b" * 64, age=7200)
    staging = _bad_staging(data / "wb-state" / "component-runtimes", "a" * 64, age=10)
    sources = data / "wb-state" / "wb-source"
    source_entries = []
    for index in range(4):
        entry = sources / f"0.1.{index}-{index:08x}"
        (entry / "bin").mkdir(parents=True)
        (entry / "bin" / "python").write_text("", encoding="utf-8")
        os.utime(entry, (1000 + index, 1000 + index))
        source_entries.append(entry)
    mcp = home / ".workbuddy" / "plugins" / "cache" / "market" / "mindie-agent" / "plugin"
    mcp.mkdir(parents=True)
    (mcp / "mcp.json").write_text(json.dumps({
        "command": str(source_entries[0] / "bin" / "python"),
        "env": {"MINDIE_WB_VENV": str(runtimes[1])},
    }), encoding="utf-8")
    return {"runtimes": runtimes, "broken": broken, "staging": staging,
            "sources": source_entries}


def _agy_oc_fixture(base: Path):
    data = base / "data"
    created = {}
    for harness in ("agy", "oc"):
        root = data / f"{harness}-state" / "component-runtimes"
        for index in range(4):
            _runtime(root, harness, index, mtime=1000 + index)
        _bad_staging(root, (harness[0] * 64), age=7200)
        _bad_staging(root, (harness[1] * 64), age=10)
        sources = data / f"{harness}-state" / f"{harness}-source"
        entries = []
        for index in range(4):
            entry = sources / f"0.1.{index}-{index:08x}"
            (entry / "bin").mkdir(parents=True)
            os.utime(entry, (1000 + index, 1000 + index))
            entries.append(entry)
        created[harness] = entries
    return created


def _dsh_fixture(base: Path, codex_runtimes):
    config, data = base / "config", base / "data"
    for index in range(4):
        _runtime(data / "update" / "component-runtimes", "dsh-update", index,
                 mtime=1000 + index)
    _bad_staging(data / "update" / "component-runtimes", "7" * 64, age=7200)
    _bad_staging(data / "update" / "component-runtimes", "6" * 64, age=10)
    dsh = data / "dsh"
    (dsh / "venv" / "bin").mkdir(parents=True)
    (dsh / "venv" / "bin" / "python").write_text("", encoding="utf-8")
    os.utime(dsh / "venv", (1000, 1000))
    runtimes_root = dsh / "runtimes"
    runtimes = []
    for index in range(4):
        entry = runtimes_root / _key("dsh", index)
        (entry / "venv" / "bin").mkdir(parents=True)
        (entry / "python").write_text("", encoding="utf-8")
        os.utime(entry, (1000 + index, 1000 + index))
        runtimes.append(entry)
    generations = dsh / "update" / "generations"
    tags = []
    for index in range(3):
        entry = generations / f"v0.1.{index}"
        (entry / "package").mkdir(parents=True)
        (entry / "asset.tgz").write_text("tarball", encoding="utf-8")
        os.utime(entry, (1000 + index, 1000 + index))
        tags.append(entry)
    (dsh / "update" / "current.json").write_text(json.dumps({
        "generation": str(runtimes[1]),
        "previous_generation": str(codex_runtimes[1]),
    }), encoding="utf-8")
    engines = config / "dsh" / "engines"
    engines.mkdir(parents=True)
    for index, name in enumerate((runtimes[1].name, "engine-human", "engine-old-1", "engine-old-2")):
        entry = engines / f"{name}.json"
        entry.write_text("{}", encoding="utf-8")
        os.utime(entry, (1000 + index, 1000 + index))
    dsh_profiles = base / "home" / ".dsh" / "profiles" / "default"
    dsh_profiles.mkdir(parents=True)
    (dsh_profiles / "package.json").write_text(json.dumps({
        "name": "dsh", "mindie_package_dir": str(dsh)}), encoding="utf-8")
    package = base / "data" / "mindie-agent" / "dsh"
    package.mkdir(parents=True)
    links = dsh_profiles / "node_modules" / "@mindie-agent"
    links.mkdir(parents=True)
    os.symlink(package, links / "dsh", target_is_directory=True)
    return {"dsh": dsh, "runtimes": runtimes, "tags": tags, "engines": engines,
            "venv": dsh / "venv"}


def _build_full_fake_machine(base: Path, home: Path):
    for directory in (base / "config", base / "data", base / "codex"):
        directory.mkdir(parents=True, exist_ok=True)
    kimi = _kimi_fixture(base)
    codex = _codex_fixture(base)
    cc = _cc_fixture(base, home)
    wb = _wb_fixture(base, home, codex["runtimes"])
    agy_oc = _agy_oc_fixture(base)
    dsh = _dsh_fixture(base, codex["runtimes"])
    # Kimi's current generation points at a config file the scanner also reads.
    (base / "config" / "kimi.json").write_text(json.dumps({
        "state_dir": str(kimi["state"])}), encoding="utf-8")
    return {"kimi": kimi, "codex": codex, "cc": cc, "wb": wb, "agy_oc": agy_oc, "dsh": dsh}


def test_owning_dir_maps_every_managed_generation_shape(tmp_path):
    root = tmp_path / "user" / "project"
    assert legacy.owning_dir(str(root / "component-runtimes" / ("a" * 64)
                                  / "runtime/.venv/bin/python")) \
        == root / "component-runtimes" / ("a" * 64)
    assert legacy.owning_dir(str(root / "update/generations" / ("b" * 40) / ".venv/bin/python")) \
        == root / "update/generations" / ("b" * 40)
    assert legacy.owning_dir(str(root / "runtimes/cc" / ("c" * 64) / "bin/python")) \
        == root / "runtimes/cc" / ("c" * 64)
    assert legacy.owning_dir(str(root / "wb-source/0.1.2-deadbeef/bin/python")) \
        == root / "wb-source" / "0.1.2-deadbeef"
    assert legacy.owning_dir(str(root / "bootstrap-venv/bin/python")) == root / "bootstrap-venv"
    assert legacy.owning_dir(str(root / "adapter-state/installations/key/cc.adapter.json")) \
        == root / "adapter-state" / "installations" / "key"
    assert legacy.owning_dir(str(root / "dsh/venv/bin/python")) == root / "dsh" / "venv"
    assert legacy.owning_dir(str(root / "nested/notes.txt")) is None


def test_multi_harness_retroactive_sweep_protects_every_live_root(tmp_path, monkeypatch):
    home = tmp_path / "home"
    fixture = _build_full_fake_machine(tmp_path, home)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(fixture["cc"]["claude_data"]))
    kimi, codex, cc, wb = fixture["kimi"], fixture["codex"], fixture["cc"], fixture["wb"]
    dsh, agy_oc = fixture["dsh"], fixture["agy_oc"]
    config, data = tmp_path / "config", tmp_path / "data"

    result = legacy.sweep_legacy_installations(
        config_home=config, data_home=data, codex_home=tmp_path / "codex",
        budget_seconds=60.0, force=True)

    assert result["status"] == "completed"
    assert result["inspection_complete"] is True
    assert result["reclaimed_dirs"] > 20
    assert result["reclaimed_files"] >= 3  # asset.tgz and stale prepare log
    assert result["warnings"] == []

    # Kimi: current, immediate rollback, launcher pin, borrowed python survive.
    assert (kimi["generations"] / kimi["shas"][5]).is_dir()
    assert (kimi["generations"] / "bootstrap").is_dir()
    assert (kimi["launch"] / kimi["shas"][2]).is_dir()   # launchd plist pin
    assert (kimi["launch"] / kimi["shas"][8]).is_dir()   # immediate previous receipt
    assert (kimi["launch"] / kimi["shas"][5]).is_dir()   # current generation launcher
    assert not (kimi["launch"] / kimi["shas"][0]).is_dir()
    assert not (kimi["launch"] / kimi["shas"][9]).is_dir()
    assert not (kimi["generations"] / kimi["shas"][0]).is_dir()
    assert not (kimi["generations"] / kimi["shas"][4]).is_dir()
    assert not (kimi["broken"]).exists()
    assert (kimi["staging"]).is_dir()
    for index in (3, 5, 7, 8, 9):
        assert kimi["runtimes"][index].is_dir(), index
    for index in (0, 1, 2, 4, 6):
        assert not kimi["runtimes"][index].exists(), index
    assert (kimi["receipts"] / (kimi["shas"][9] + ".json")).is_file()
    assert not (kimi["receipts"] / (kimi["shas"][0] + ".json")).exists()
    assert not (kimi["receipts"] / (kimi["shas"][1] + ".json")).exists()

    # Codex: WB borrowed #4 and DSH previous #1 keep their generations; newest 2
    # verified plus fresh staging remain, the 2-hour-old half-build is gone.
    for index in (1, 4, 8, 9):
        assert codex["runtimes"][index].is_dir(), index
    for index in (0, 2, 3, 5, 6, 7):
        assert not codex["runtimes"][index].exists(), index
    assert not codex["broken"].exists()
    assert codex["staging"].is_dir()
    assert (codex["releases"] / "0.1.22").is_dir()
    assert (codex["releases"] / "0.1.21").is_dir()
    assert not (codex["releases"] / "0.1.20").exists()
    assert not codex["old_log"].exists()
    assert codex["fresh_log"].is_file()
    assert len(list(codex["updates"].glob("superseded-*.json"))) == 8
    assert (codex["plugin"] / "runtimes" / "runtime-2").is_dir()
    assert not (codex["plugin"] / "runtimes" / "runtime-0").exists()

    # Claude Code entry.py pin and the newest two ready runtimes survive.
    assert cc["runtimes"][1].is_dir()
    assert cc["runtimes"][4].is_dir() and cc["runtimes"][3].is_dir()
    assert not cc["runtimes"][0].exists() and not cc["runtimes"][2].exists()
    assert len(list(cc["installations"].iterdir())) == 2
    cc_state = tmp_path / "data" / "cc" / "adapter-state" / "component-runtimes"
    assert (cc_state / _key("cc-state", 3)).is_dir()
    assert (cc_state / _key("cc-state", 2)).is_dir()
    assert not (cc_state / _key("cc-state", 0)).exists()
    assert (cc_state / ("8" * 64)).is_dir()

    # WorkBuddy: the mcp.json source pin and the borrowed runtime survive.
    assert wb["runtimes"][1].is_dir()
    assert not wb["broken"].exists()
    assert wb["staging"].is_dir()
    assert wb["sources"][0].is_dir()
    assert wb["sources"][3].is_dir() and wb["sources"][2].is_dir()
    assert not wb["sources"][1].exists()

    # AGY/OC sources keep the pinned newest two and drop the rest.
    for harness in ("agy", "oc"):
        assert agy_oc[harness][3].is_dir() and agy_oc[harness][2].is_dir()
        assert not agy_oc[harness][0].exists()
        assert not agy_oc[harness][1].exists()
        harness_state = tmp_path / "data" / f"{harness}-state" / "component-runtimes"
        assert (harness_state / _key(harness, 3)).is_dir()
        assert not (harness_state / _key(harness, 0)).exists()
        assert (harness_state / (harness[1] * 64)).is_dir()
    wb_state = tmp_path / "data" / "wb-state" / "component-runtimes"
    assert wb_state.joinpath(_key("wb", 3)).is_dir()
    assert wb_state.joinpath(_key("wb", 1)).is_dir()
    assert not wb_state.joinpath(_key("wb", 0)).exists()

    # DSH: current runtime, newest verified generation, extracted asset.tgz gone.
    assert dsh["runtimes"][1].is_dir()
    assert dsh["runtimes"][3].is_dir()
    assert not dsh["runtimes"][0].exists() and not dsh["runtimes"][2].exists()
    assert not dsh["venv"].exists()
    assert dsh["tags"][2].is_dir() and dsh["tags"][1].is_dir()
    assert not dsh["tags"][0].exists()
    assert not (dsh["tags"][2] / "asset.tgz").exists()
    assert not (dsh["tags"][1] / "asset.tgz").exists()
    assert (dsh["engines"] / f"{dsh['runtimes'][1].name}.json").is_file()
    assert len(list(dsh["engines"].glob("*.json"))) == 3
    dsh_update = tmp_path / "data" / "update" / "component-runtimes"
    assert (dsh_update / _key("dsh-update", 3)).is_dir()
    assert not (dsh_update / _key("dsh-update", 0)).exists()
    assert (dsh_update / ("6" * 64)).is_dir()

    # A second forced pass is a no-op: no double-delete, no trash remainder.
    second = legacy.sweep_legacy_installations(
        config_home=config, data_home=data, codex_home=tmp_path / "codex",
        budget_seconds=60.0, force=True)
    assert second["reclaimed_dirs"] == 0
    assert second["reclaimed_files"] == 0
    assert second["trash_remaining"] == 0
    cooled = legacy.sweep_legacy_installations(
        config_home=config, data_home=data, codex_home=tmp_path / "codex",
        budget_seconds=60.0)
    assert cooled["status"] == "skipped_cooldown"
    assert cooled["reclaimed_dirs"] == 0


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX ps argv inspection; Windows uses image path and runtime lease")
def test_unreferenced_component_runtime_is_kept_while_subprocess_runs(tmp_path):
    root = tmp_path / "component-runtimes"
    generation = _runtime(root, "live", 0, mtime=1000)
    script = generation / "runtime" / "serve.py"
    script.write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
    process_handle = subprocess.Popen([sys.executable, "-I", str(script)])
    try:
        deadline = time.monotonic() + 10
        live, complete = leases.live_process_prefixes()
        while time.monotonic() < deadline:
            live, complete = leases.live_process_prefixes()
            if complete and any(str(generation) in value for value in live):
                break
            time.sleep(0.1)
        else:
            pytest.fail("live process command line was not observable through ps")
        protected = prune_component_runtimes(
            root, keep_paths=[], keep_recent_verified=0,
            budget_seconds=5.0, live_paths=live, inspection_complete=complete)
        assert protected["reclaimed_dirs"] == 0
        assert generation.is_dir()
    finally:
        process_handle.terminate()
        process_handle.wait(timeout=10)
    live, complete = leases.live_process_prefixes()
    reclaimed = prune_component_runtimes(
        root, keep_paths=[], keep_recent_verified=0,
        budget_seconds=5.0, live_paths=live, inspection_complete=complete)
    assert reclaimed["reclaimed_dirs"] == 1
    assert not generation.exists()


def test_cross_process_shared_lease_blocks_exclusive_retirement(tmp_path):
    generation = _runtime(tmp_path / "component-runtimes", "lease", 0)
    holder = tmp_path / "holder.py"
    holder.write_text(
        "import time\n"
        "from mindie_knowledge.storage.leases import acquire_runtime_lease\n"
        f"with acquire_runtime_lease({str(generation)!r}):\n"
        "    print('held', flush=True)\n"
        "    time.sleep(60)\n", encoding="utf-8")
    repository = Path(__file__).resolve().parents[1]
    child = subprocess.Popen([sys.executable, str(holder)], stdout=subprocess.PIPE,
                             text=True,
                             env={**os.environ, "PYTHONPATH": str(repository)})
    try:
        assert child.stdout.readline().strip() == "held"
        assert leases.is_lease_held(generation) is True
        result = prune_component_runtimes(
            tmp_path / "component-runtimes", keep_paths=[], keep_recent_verified=0,
            budget_seconds=5.0)
        assert result["reclaimed_dirs"] == 0
        assert generation.is_dir()
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert leases.is_lease_held(generation) is False


def test_windows_permission_error_defers_without_deleting_a_single_file(tmp_path, monkeypatch):
    root = tmp_path / "component-runtimes"
    generation = _runtime(root, "locked", 0, mtime=1000)
    (generation / "runtime" / "data.bin").write_text("payload", encoding="utf-8")
    real_replace = lifecycle.os.replace

    def deny(source, destination):
        # On Windows ``source`` carries the ``\\?\`` extended prefix.
        if filesystem.path_identity(source) == filesystem.path_identity(generation):
            raise PermissionError(13, "WinError 5: in use")
        return real_replace(source, destination)

    monkeypatch.setattr(lifecycle.os, "replace", deny)
    outcome = retire_directory(generation)
    assert outcome["status"] == "deferred_in_use"
    assert outcome["error"] == "PermissionError"
    assert (generation / "runtime" / "data.bin").is_file()
    assert (generation / "components.json").is_file()
    assert not list(root.glob(".trash-*"))

    pruned = prune_component_runtimes(
        root, keep_paths=[], keep_recent_verified=0, budget_seconds=5.0)
    assert pruned["reclaimed_dirs"] == 0
    assert pruned["deferred_dirs"] == 1
    assert generation.is_dir()


def test_partial_deadline_leaves_resumable_trash_and_next_sweep_finishes(tmp_path):
    generation = _runtime(tmp_path / "component-runtimes", "slow", 0, mtime=1000)
    (generation / "runtime" / "deep").mkdir()
    (generation / "runtime" / "deep" / "file.bin").write_text("x", encoding="utf-8")
    outcome = retire_directory(generation, deadline=time.monotonic() - 1)
    assert outcome["status"] == "trashed_partial"
    trash = Path(outcome["trash"])
    assert trash.is_dir()
    swept = sweep_trash(generation.parent, deadline=time.monotonic() + 5)
    assert swept["removed"] == 1
    assert not trash.exists()


def test_failed_fresh_build_removes_unverified_venv_and_pending_manifest(tmp_path, monkeypatch):
    source = tmp_path / "adapter"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[project]\nname = "adapter"\nversion = "0.1.2"\n', encoding="utf-8")
    targets = {"mindie-knowledge": {
        "repository": "mindie-agent/knowledge", "version": "0.13.21",
        "revision": "a" * 40,
    }}
    baseline = {"source_sha256": component_runtime.source_identity(source), "runtime": {}}
    root = (tmp_path / "component-runtimes").resolve()
    key = component_runtime.generation_key(baseline, targets)
    generation = component_runtime.generation_dir(root, key)
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.setattr(component_runtime.shutil, "which",
                        lambda name: str(tmp_path / "tools" / "uv"))
    foreign_cache = tmp_path / "foreign-volume-uv-cache"
    monkeypatch.setattr(uv_cache, "_uv_default_cache", lambda env=None: foreign_cache)
    monkeypatch.setattr(
        uv_cache, "_device",
        lambda path: 1 if "foreign-volume-uv-cache" in str(path) else 2)
    observed = {}

    def failing_download(command, payload, **kwargs):
        observed.update(kwargs.get("child_env") or {})
        venv = generation / "runtime" / ".venv" / "Lib" / "site-packages"
        venv.mkdir(parents=True, exist_ok=True)
        (venv / "half-built.bin").write_text("x" * 1024, encoding="utf-8")
        raise TimeoutError("uv sync deadline")

    monkeypatch.setattr(component_runtime, "bounded_run", failing_download)
    with pytest.raises(TimeoutError):
        component_runtime.build(source, root, baseline, targets, timeout=120)
    assert not (generation / "runtime").exists()
    assert not (generation / "source").exists()
    assert not (generation / "components.json").exists()
    assert not list(generation.glob("*.pending.json"))
    assert "UV_PROJECT_ENVIRONMENT" in observed
    assert observed["UV_CACHE_DIR"] == str(root.parent / ".uv-cache")


def test_cross_volume_uv_cache_selection_and_explicit_override(tmp_path, monkeypatch):
    target = tmp_path / "component-runtimes"
    target.mkdir()
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    foreign_cache = tmp_path / "foreign-volume-uv-cache"
    monkeypatch.setattr(uv_cache, "_uv_default_cache", lambda env=None: foreign_cache)
    monkeypatch.setattr(
        uv_cache, "_device",
        lambda path: 1 if "foreign-volume-uv-cache" in str(path) else 2)
    env = {}
    configured = configure_same_volume_uv_cache(target, env)
    assert configured["UV_CACHE_DIR"] == str(target.parent / ".uv-cache")

    same_volume = {"UV_CACHE_DIR": ""}
    monkeypatch.setattr(uv_cache, "_device", lambda path: 7)
    assert configure_same_volume_uv_cache(target, same_volume) == {"UV_CACHE_DIR": ""}

    explicit_path = str(tmp_path / "shared" / "uv")
    explicit = {"UV_CACHE_DIR": explicit_path}
    monkeypatch.setattr(uv_cache, "_device",
                        lambda path: (_ for _ in ()).throw(AssertionError("must not inspect")))
    assert configure_same_volume_uv_cache(target, explicit) == {
        "UV_CACHE_DIR": explicit_path}


def test_windows_short_generation_directory_reuse_and_collision_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(component_runtime, "_IS_WINDOWS", True)
    root = tmp_path / "component-runtimes"
    root.mkdir()
    key = "ab" * 32
    short = root / key[:24]
    assert component_runtime.generation_dir(root, key) == short
    short.mkdir()
    (short / "components.json").write_text("{}", encoding="utf-8")
    assert component_runtime.generation_dir(root, key) == short

    collided = tmp_path / "collided"
    collided.mkdir()
    (collided / key[:24]).mkdir()
    (collided / key[:24] / "runtime").mkdir()
    assert component_runtime.generation_dir(collided, key) == collided / key[:32]

    existing = tmp_path / "legacy-full"
    existing.mkdir()
    (existing / key).mkdir()
    assert component_runtime.generation_dir(existing, key) == existing / key


def test_release_http_prunes_legacy_entries_and_stale_temporaries(tmp_path):
    cache = tmp_path / "release-cache"
    cache.mkdir()
    now = time.time()
    for index in range(130):
        entry = cache / f"{index:064x}.json"
        entry.write_text("{}", encoding="utf-8")
        os.utime(entry, (1000 + index, 1000 + index))
    http_entry = cache / ("http-" + "f" * 64 + ".json")
    http_entry.write_text("{}", encoding="utf-8")
    os.utime(http_entry, (now, now))
    stale = cache / "abandoned.tmp"
    stale.write_text("partial", encoding="utf-8")
    os.utime(stale, (now - 7200, now - 7200))
    fresh = cache / "inflight.tmp"
    fresh.write_text("partial", encoding="utf-8")

    result = release_http._prune_http_cache(cache)
    assert result["stale"] == 1
    assert not stale.exists()
    assert fresh.exists()
    assert len(list(cache.glob("*.json"))) == 128
    assert (cache / f"{129:064x}.json").is_file()
    assert not (cache / f"{0:064x}.json").exists()


def test_bounded_run_removes_scratch_when_spawn_times_out(tmp_path, monkeypatch):
    before = {entry.name for entry in Path(tempfile.gettempdir()).glob("mindie-summary-owner-*")}

    def timing_out(command, stdin, scratch=None, child_env=None):
        raise TimeoutError("spawn deadline")

    monkeypatch.setattr(process, "_spawn", timing_out)
    with pytest.raises(TimeoutError):
        process.bounded_run(["unused"], "", timeout=1, max_output=1024,
                            on_result=lambda _output: None)
    after = {entry.name for entry in Path(tempfile.gettempdir()).glob("mindie-summary-owner-*")}
    assert after == before


def test_scratch_owner_marker_and_stale_sweep_are_explicit(tmp_path, monkeypatch):
    scratch = tmp_path / "tmp"
    scratch.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    stale = scratch / "mindie-summary-owner-stale"
    stale.mkdir()
    (stale / summary_scratch.OWNER_MARKER).write_text(
        json.dumps({"pid": 999999, "created_at": time.time() - 7200}), encoding="utf-8")
    unowned = scratch / "mindie-summary-owner-unowned"
    unowned.mkdir()
    created = summary_scratch.create_root()
    try:
        marker = _read_json(created / summary_scratch.OWNER_MARKER)
        assert marker["pid"] == os.getpid()
        assert isinstance(marker["created_at"], (int, float))
        assert not stale.exists()
        assert unowned.exists()
    finally:
        shutil.rmtree(created, ignore_errors=True)


def test_ntfs_junction_and_symlink_are_never_traversed(tmp_path, monkeypatch):
    external = tmp_path / "external-target"
    (external / "nested").mkdir(parents=True)
    (external / "nested" / "precious.txt").write_text("do not delete", encoding="utf-8")

    tree = tmp_path / "tree"
    (tree / "keep").mkdir(parents=True)
    (tree / "keep" / "file.txt").write_text("local", encoding="utf-8")
    (tree / "keep" / "link").symlink_to(external, target_is_directory=True)
    assert lifecycle._bounded_rmtree(tree, None) is True
    assert not tree.exists()
    assert (external / "nested" / "precious.txt").read_text(encoding="utf-8") == "do not delete"

    root = tmp_path / "component-runtimes"
    root.mkdir()
    junction = root / "junction-name"
    junction.symlink_to(external, target_is_directory=True)
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path, "is_symlink",
        lambda self: False if self == junction else real_is_symlink(self))
    junction_io = filesystem.filesystem_path(junction)
    real_lstat = os.lstat

    def lstat(value):
        if Path(value) == junction_io:
            # Ordinary directory mode: the 3.12+ os.path.isjunction fallback
            # below is what identifies this simulated reparse point.
            return type("Stat", (), {"st_mode": stat.S_IFDIR | 0o755})()
        return real_lstat(value)

    monkeypatch.setattr(lifecycle.os, "lstat", lstat)
    monkeypatch.setattr(os.path, "isjunction",
                        lambda value: Path(value) == junction, raising=False)
    outcome = retire_directory(junction)
    assert outcome["status"] == "retired"
    assert not junction.exists()
    assert (external / "nested" / "precious.txt").is_file()
    assert not (external / ".inuse.lock").exists()


def test_junction_detection_uses_lstat_without_python312_os_path_helper(tmp_path, monkeypatch):
    external = tmp_path / "external-target"
    (external / "nested").mkdir(parents=True)
    (external / "nested" / "precious.txt").write_text("do not delete", encoding="utf-8")
    junction = tmp_path / "junction-name"
    junction.mkdir()
    junction_io = filesystem.filesystem_path(junction)

    class FakeStat:
        def __init__(self, **attributes):
            self.st_mode = stat.S_IFDIR | 0o755
            self.__dict__.update(attributes)

    fake = {"value": FakeStat(st_file_attributes=0x410)}
    real_lstat = os.lstat

    def lstat(value):
        if Path(value) == junction_io:
            return fake["value"]
        return real_lstat(value)

    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path, "is_symlink",
        lambda self: False if self == junction else real_is_symlink(self))
    monkeypatch.delattr(os.path, "isjunction", raising=False)
    monkeypatch.setattr(lifecycle.os, "lstat", lstat)

    # FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT on Python 3.11.
    assert lifecycle.is_junction(junction) is True
    # IO_REPARSE_TAG_MOUNT_POINT alone is equally decisive.
    fake["value"] = FakeStat(st_reparse_tag=0xA0000003)
    assert lifecycle.is_junction(junction) is True

    real_scandir = os.scandir

    def scandir(value):
        if Path(value) == junction_io:
            raise AssertionError("descended into a junction target")
        return real_scandir(value)

    removed = []
    real_rmdir = os.rmdir

    def rmdir(value):
        removed.append(Path(value))
        return real_rmdir(value)

    monkeypatch.setattr(lifecycle.os, "scandir", scandir)
    monkeypatch.setattr(lifecycle.os, "rmdir", rmdir)

    assert lifecycle._bounded_rmtree(junction, None) is True
    assert removed == [junction_io]
    assert not junction.exists()
    assert (external / "nested" / "precious.txt").is_file()


def test_external_git_source_root_is_never_a_deletion_scan_root(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    project = tmp_path / "user" / "code" / "project"
    (project / ".git").mkdir(parents=True)
    victim = project / "runtimes" / "old"
    victim.mkdir(parents=True)
    (victim / "precious.txt").write_text("keep", encoding="utf-8")
    (config / "wb.json").write_text(json.dumps({"source_root": str(project)}),
                                    encoding="utf-8")

    # Even a candidate inside a managed-looking directory is protected when
    # any ancestor is a Git checkout.
    managed = project / "state" / "runtimes" / "old"
    (managed / "bin").mkdir(parents=True)
    (managed / "bin" / "python").write_text("", encoding="utf-8")
    os.utime(managed, (1000, 1000))
    newer = project / "state" / "runtimes" / "newer"
    (newer / "bin").mkdir(parents=True)
    os.utime(newer, (2000, 2000))

    result = legacy.sweep_legacy_installations(
        config_home=config, data_home=project / "state",
        codex_home=tmp_path / "codex", budget_seconds=60.0, force=True)
    assert result["status"] == "completed"
    assert victim.is_dir() and (victim / "precious.txt").is_file()
    assert managed.is_dir()
    assert any("protected source root" in warning for warning in result["warnings"])


def test_paths_with_spaces_and_chinese_survive_ps_and_host_pins(tmp_path, monkeypatch):
    base = tmp_path / "Application Support" / "中文目录"
    config, data = base / "config", base / "data"
    home = tmp_path / "home"

    common = data / "runtimes"
    runtimes = []
    for index in range(4):
        entry = common / _key("cjk-live", index)
        (entry / "bin").mkdir(parents=True)
        (entry / "bin" / "python").write_text("", encoding="utf-8")
        os.utime(entry, (1000 + index, 1000 + index))
        runtimes.append(entry)

    cc_root = data / "runtimes" / "cc"
    cc_runtimes = []
    for index in range(4):
        entry = cc_root / _key("cjk-cc", index)
        (entry / "bin").mkdir(parents=True)
        (entry / "bin" / "python").write_text("", encoding="utf-8")
        ready = entry.with_name(entry.name + ".ready.json")
        ready.write_text("{}", encoding="utf-8")
        os.utime(entry, (1000 + index, 1000 + index))
        os.utime(ready, (1000 + index, 1000 + index))
        cc_runtimes.append(entry)

    wb_sources = data / "wb-state" / "wb-source"
    sources = []
    for index in range(4):
        entry = wb_sources / f"0.1.{index}-{index:08x}"
        (entry / "bin").mkdir(parents=True)
        (entry / "bin" / "python").write_text("", encoding="utf-8")
        os.utime(entry, (1000 + index, 1000 + index))
        sources.append(entry)

    claude = base / "claude data" / "releases" / "0.1.23"
    claude.mkdir(parents=True)
    (claude / "entry.py").write_text(
        "PYTHON = " + json.dumps(str(cc_runtimes[0] / "bin" / "python")) + "\n",
        encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(base / "claude data"))

    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "agent.mindie.kimi-update.plist").write_bytes(plistlib.dumps(
        {"ProgramArguments": [str(runtimes[0] / "bin" / "python")]}))

    update = data / "update"
    update.mkdir(parents=True)
    (update / "MindIEKimiUpdate.pyw").write_text(
        "PYTHON = " + json.dumps(str(runtimes[1] / "bin" / "python")) + "\n",
        encoding="utf-8")

    front = home / ".kimi-code" / "plugins" / "managed" / "mindie-agent"
    front.mkdir(parents=True)
    (front / "mindie-front").write_text(
        "#!/bin/sh\nexec \"" + str(runtimes[2] / "bin" / "python") + "\" \"$@\"\n",
        encoding="utf-8")

    mcp = home / ".workbuddy" / "plugins" / "cache" / "market" / "mindie-agent" / "plugin"
    mcp.mkdir(parents=True)
    (mcp / "mcp.json").write_text(json.dumps({
        "command": str(sources[0] / "bin" / "python"),
        "env": {"MINDIE_WB_VENV": str(sources[1])},
    }), encoding="utf-8")

    result = legacy.sweep_legacy_installations(
        config_home=config, data_home=data, codex_home=base / "codex",
        budget_seconds=60.0, force=True)

    assert result["status"] == "completed"
    for index in (0, 1, 2, 3):
        assert runtimes[index].is_dir(), index
    assert cc_runtimes[0].is_dir()
    assert not cc_runtimes[1].exists()
    assert sources[0].is_dir() and sources[1].is_dir()
    assert sources[2].is_dir() and sources[3].is_dir()

    if os.name == "nt":
        return  # Windows live inspection exposes image paths, not ps command lines.
    generation = base / "live runner"
    generation.mkdir()
    script = generation / "serve.py"
    script.write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
    child = subprocess.Popen([sys.executable, "-I", str(script)])
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            live, complete = leases.live_process_prefixes()
            if complete and leases.is_path_used_by_live_process(generation, live):
                break
            time.sleep(0.1)
        else:
            pytest.fail("a live command line with spaces/CJK was not observable")
    finally:
        child.terminate()
        child.wait(timeout=10)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ps command-line inspection")
def test_posix_ps_uses_ww_and_strips_columns_for_long_paths(tmp_path, monkeypatch):
    long_path = (tmp_path / "Application Support" / ("a" * 96)
                 / "component-runtimes" / ("b" * 64) / ".venv" / "bin" / "python")
    assert len(str(long_path)) > 240
    record = tmp_path / "ps-record.txt"
    executable = tmp_path / "fake-ps"
    executable.write_text(
        "#!/bin/sh\n"
        "{\n"
        '  printf \'COLUMNS=%s\\n\' "${COLUMNS-__unset__}"\n'
        '  for arg in "$@"; do printf \'ARG=%s\\n\' "$arg"; done\n'
        '} > "$FAKE_PS_RECORD"\n'
        'printf \'%s\\n\' "$FAKE_PS_PATH"\n',
        encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("COLUMNS", "40")
    monkeypatch.setenv("FAKE_PS_RECORD", str(record))
    monkeypatch.setenv("FAKE_PS_PATH", str(long_path))
    monkeypatch.setattr(_host_exec, "which_native", lambda name: str(executable))

    live, complete = leases._posix_live_paths(timeout=10)

    assert complete is True
    assert live.commands and str(long_path) in live.commands[0]
    assert leases.is_path_used_by_live_process(long_path, live) is True
    details = record.read_text(encoding="utf-8")
    assert "COLUMNS=__unset__" in details
    assert "ARG=-ww" in details
    assert "ARG=-axo" in details
    assert "ARG=command=" in details


@pytest.mark.skipif(os.name == "nt", reason="POSIX ps command-line inspection")
def test_posix_ps_capped_or_failed_output_is_incomplete(tmp_path, monkeypatch):
    executable = tmp_path / "fake-ps"
    executable.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$(printf '%*s' 4096 '')\"\n",
        encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(_host_exec, "which_native", lambda name: str(executable))
    monkeypatch.setattr(leases, "_MAX_COMMAND_OUTPUT", 1024)

    live, complete = leases._posix_live_paths(timeout=10)
    assert complete is False
    assert not live

    executable.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    live, complete = leases._posix_live_paths(timeout=10)
    assert complete is False
    assert not live


def test_unverified_staging_obeys_lease_builder_lock_and_grace(tmp_path):
    root = tmp_path / "component-runtimes"
    staging = _bad_staging(root, "e" * 64, age=7200)

    descriptor = os.open(staging / "prepare.lock", os.O_CREAT | os.O_RDWR, 0o600)
    lock_file(descriptor, exclusive=True)
    os.utime(staging, (1000, 1000))
    try:
        held = prune_component_runtimes(
            root, keep_paths=[], keep_recent_verified=0, budget_seconds=5.0,
            include_global_roots=False)
        assert held["reclaimed_dirs"] == 0
        assert staging.is_dir()
    finally:
        unlock_file(descriptor)
        os.close(descriptor)

    fresh = _bad_staging(root, "f" * 64, age=5)
    pruned = prune_component_runtimes(
        root, keep_paths=[], keep_recent_verified=0, budget_seconds=5.0,
        include_global_roots=False)
    assert pruned["reclaimed_dirs"] == 1
    assert not staging.exists()
    assert fresh.is_dir()


def test_retirement_holds_exclusive_locks_across_rename(tmp_path, monkeypatch):
    generation = _runtime(tmp_path / "component-runtimes", "guard", 0, mtime=1000)
    observed = {}
    real_replace = lifecycle.os.replace

    def probe(source, destination):
        if filesystem.path_identity(source) == filesystem.path_identity(generation):
            if os.name == "nt":
                # Windows holds one sibling lock and no handle inside the
                # generation, because NTFS refuses to rename an open directory.
                sibling = generation.parent / leases.RETIRE_LOCK_NAME
                descriptor = os.open(sibling, os.O_RDWR)
                try:
                    with pytest.raises((BlockingIOError, OSError)):
                        lock_file(descriptor, exclusive=True)
                    observed["retire_lock_held"] = True
                finally:
                    os.close(descriptor)
            else:
                observed["lease_held"] = leases.is_lease_held(generation)
        return real_replace(source, destination)

    monkeypatch.setattr(lifecycle.os, "replace", probe)
    outcome = retire_directory(generation)
    assert outcome["status"] == "retired"
    if os.name == "nt":
        assert observed["retire_lock_held"] is True
    else:
        assert observed["lease_held"] is True

    trash_named = tmp_path / ".trash-already-retired"
    trash_named.mkdir()
    with pytest.raises(RuntimeError):
        leases.acquire_runtime_lease(trash_named)


def test_prune_inspects_live_processes_and_unions_global_roots(tmp_path, monkeypatch):
    from mindie_knowledge.storage import legacy_roots

    root = tmp_path / "component-runtimes"
    generation = _runtime(root, "global-pin", 0, mtime=1000)
    monkeypatch.setattr(
        legacy_roots, "collect_global_protected_roots",
        lambda **kwargs: {generation})
    monkeypatch.setattr(
        leases, "live_process_prefixes",
        lambda **kwargs: (leases.LivePaths(), False))
    result = prune_component_runtimes(
        root, keep_paths=[], keep_recent_verified=0, budget_seconds=5.0)
    assert result["kept_dirs"] >= 1
    assert generation.is_dir()
    assert result["inspection_complete"] is False


def _build_fixture(tmp_path, monkeypatch):
    source = tmp_path / "adapter"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[project]\nname = "adapter"\nversion = "0.1.2"\n', encoding="utf-8")
    targets = {"mindie-knowledge": {
        "repository": "mindie-agent/knowledge", "version": "0.13.21",
        "revision": "a" * 40,
    }}
    baseline = {"source_sha256": component_runtime.source_identity(source), "runtime": {}}
    root = (tmp_path / "component-runtimes").resolve()
    key = component_runtime.generation_key(baseline, targets)
    generation = component_runtime.generation_dir(root, key)
    monkeypatch.setattr(component_runtime.shutil, "which",
                        lambda name: str(tmp_path / "tools" / "uv"))

    def successful(command, payload, **kwargs):
        if Path(command[0]).name == "uv":
            venv = generation / "runtime" / ".venv" / (
                "Scripts" if os.name == "nt" else "bin")
            venv.mkdir(parents=True, exist_ok=True)
            (venv / ("python.exe" if os.name == "nt" else "python")).write_text(
                "", encoding="utf-8")
            (generation / "runtime" / "uv.lock").write_text("{}", encoding="utf-8")
            return ""
        return json.dumps({"installed": {}})

    monkeypatch.setattr(component_runtime, "bounded_run", successful)
    return source, root, baseline, targets, generation


def test_published_manifest_survives_post_publish_interrupt(tmp_path, monkeypatch):
    source, root, baseline, targets, generation = _build_fixture(tmp_path, monkeypatch)

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(component_runtime, "_post_build_sweep", interrupted)
    with pytest.raises(KeyboardInterrupt):
        component_runtime.build(source, root, baseline, targets, timeout=120)
    assert (generation / "components.json").is_file()
    manifest = json.loads((generation / "components.json").read_text(encoding="utf-8"))
    # Windows reuses a 24-hex directory alias; the manifest keeps the full key.
    assert manifest["generation_key"] == component_runtime.generation_key(
        baseline, targets)
    selected = generation / "runtime" / ".venv" / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python")
    assert selected.is_file()
    assert (generation / "source" / "pyproject.toml").is_file()
    assert not list(generation.glob("*.pending.json"))


def test_post_build_sweep_warning_does_not_abort_verified_build(tmp_path, monkeypatch):
    source, root, baseline, targets, generation = _build_fixture(tmp_path, monkeypatch)

    def failing(*_args, **_kwargs):
        raise RuntimeError("cleanup warning")

    monkeypatch.setattr(component_runtime, "_post_build_sweep", failing)
    result = component_runtime.build(source, root, baseline, targets, timeout=120)
    assert result["generation"] == str(generation)
    assert (generation / "components.json").is_file()


def test_prune_attempt_receipts_protects_active_and_pending_attempts(tmp_path):
    root = tmp_path / "service"
    root.mkdir()
    active, pending = "a" * 32, "b" * 32
    (root / "launch.json").write_text(json.dumps({"attempt": active}), encoding="utf-8")
    (root / "operation.json").write_text(
        json.dumps({"attempt": active, "phase": "started"}), encoding="utf-8")
    for attempt in (active, pending):
        (root / (attempt + ".claimed")).write_text("", encoding="utf-8")
        (root / (attempt + ".result.json")).write_text(json.dumps({
            "attempt": attempt, "status": "starting", "pid": 123}),
            encoding="utf-8")
    old = time.time() - 3 * 24 * 3600
    for index in range(40):
        attempt = f"{index:032x}"
        for suffix, value in ((".claimed", ""),
                              (".result.json", json.dumps({
                                  "attempt": attempt, "status": "finished",
                                  "exit_code": 0}))):
            path = root / (attempt + suffix)
            path.write_text(value, encoding="utf-8")
            os.utime(path, (old, old))
    result = service_host.prune_attempt_receipts(root)
    assert result["removed"] > 0
    assert (root / (active + ".claimed")).is_file()
    assert (root / (active + ".result.json")).is_file()
    assert (root / (pending + ".claimed")).is_file()
    assert (root / (pending + ".result.json")).is_file()
    assert (root / "launch.json").is_file()


def test_windows_process_error_classification_is_honest():
    classify = leases._windows_process_error_is_incomplete
    assert classify(87, "python.exe") is False   # exited mid-scan
    assert classify(5, "svchost.exe") is False   # SYSTEM process is normal
    assert classify(5, "csrss.exe") is False
    assert classify(5, "python.exe") is True     # a denied host hides liveness
    assert classify(5, "node.exe") is True
    assert classify(5, "chrome.exe") is False
    assert classify(2, "anything.exe") is True   # unknown error is incomplete


def test_path_identity_is_reused_across_storage_modules(tmp_path):
    candidate = tmp_path / "Application Support" / "中文目录" / "generation"
    candidate.mkdir(parents=True)
    identity = filesystem.path_identity(candidate)
    assert leases._normalize(candidate) == identity
    assert lifecycle._identity(candidate) == identity


def test_generation_key_collision_falls_back_instead_of_crashing(tmp_path, monkeypatch):
    monkeypatch.setattr(component_runtime, "_IS_WINDOWS", True)
    root = tmp_path / "collided-key"
    root.mkdir()
    key = "cd" * 32
    short = root / key[:24]
    short.mkdir()
    (short / "components.json").write_text(
        json.dumps({"generation_key": "ef" * 32}), encoding="utf-8")
    assert component_runtime.generation_dir(root, key) == root / key[:32]
    (root / key[:32]).mkdir()
    (root / key[:32] / "components.json").write_text(
        json.dumps({"generation_key": key}), encoding="utf-8")
    assert component_runtime.generation_dir(root, key) == root / key[:32]

    manifest_less = tmp_path / "aborted-key"
    manifest_less.mkdir()
    (manifest_less / key[:24]).mkdir()
    assert component_runtime.generation_dir(manifest_less, key) == manifest_less / key[:32]

    monkeypatch.setattr(component_runtime, "_IS_WINDOWS", False)
    posix = tmp_path / "posix-key"
    (posix / key[:24]).mkdir(parents=True)
    assert component_runtime.generation_dir(posix, key) == posix / key


def test_uv_cache_defaults_and_mountpoint_fallback(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    target = tmp_path / "mountpoint"
    target.mkdir()

    monkeypatch.setattr(uv_cache, "_device", lambda path: 2)
    xdg = tmp_path / "xdg-cache"
    assert uv_cache._uv_default_cache({"XDG_CACHE_HOME": str(xdg)}) == xdg / "uv"
    assert configure_same_volume_uv_cache(target, {"XDG_CACHE_HOME": str(xdg)}) \
        == {"XDG_CACHE_HOME": str(xdg)}

    monkeypatch.setattr(
        uv_cache, "_device",
        lambda path: 2 if str(path).startswith(str(target)) else 1)
    configured = configure_same_volume_uv_cache(target, {})
    assert configured["UV_CACHE_DIR"] == str(target / ".uv-cache")


def test_sweep_trash_runs_on_every_managed_target(tmp_path):
    data = tmp_path / "data"
    sources = data / "wb-state" / "wb-source"
    stale = sources / ".trash-old"
    (stale / "nested").mkdir(parents=True)
    (stale / "nested" / "file.txt").write_text("x", encoding="utf-8")
    live = sources / "0.1.2-deadbeef"
    (live / "bin").mkdir(parents=True)
    os.utime(live, (1000, 1000))

    result = legacy.sweep_legacy_installations(
        config_home=tmp_path / "config", data_home=data,
        codex_home=tmp_path / "codex", budget_seconds=30.0, force=True)
    assert result["reclaimed_trash"] >= 1
    assert not stale.exists()
    assert live.is_dir()


# --------------------------------------------------------------------------- #
# R4 surgical regressions: link safety, source-root ancestry, Windows handles,
# truncated ps, scoped-home scans and deadline-bounded enumeration.
# --------------------------------------------------------------------------- #

def test_symlinked_generation_never_deletes_asset_in_external_target(tmp_path):
    data = tmp_path / "data"
    generations = data / "dsh" / "update" / "generations"
    generations.mkdir(parents=True)
    external = tmp_path / "external-generation"
    (external / "package").mkdir(parents=True)
    asset = external / "asset.tgz"
    asset.write_text("precious", encoding="utf-8")
    os.utime(external, (1000, 1000))
    link = generations / "v0.1.0"
    link.symlink_to(external, target_is_directory=True)

    assert legacy._verified_generation(link) is True
    result = legacy.sweep_legacy_installations(
        config_home=tmp_path / "config", data_home=data,
        codex_home=tmp_path / "codex", budget_seconds=30.0, force=True)

    assert result["status"] == "completed"
    assert result["kept_dirs"] >= 1
    assert asset.is_file()
    assert asset.read_text(encoding="utf-8") == "precious"
    assert link.is_symlink()


def test_junction_asset_is_never_unlinked(tmp_path, monkeypatch):
    data = tmp_path / "data"
    entry = data / "dsh" / "update" / "generations" / "v0.1.1"
    (entry / "package").mkdir(parents=True)
    asset = entry / "asset.tgz"
    asset.write_text("keep", encoding="utf-8")
    os.utime(entry, (1000, 1000))
    monkeypatch.setattr(legacy, "is_junction",
                        lambda path: Path(path) == asset)

    legacy.sweep_legacy_installations(
        config_home=tmp_path / "config", data_home=data,
        codex_home=tmp_path / "codex", budget_seconds=30.0, force=True)

    assert asset.is_file()
    assert (entry / "package").is_dir()


def test_trash_sweep_and_prune_refuse_a_symlinked_root(tmp_path):
    external = tmp_path / "external"
    trash = external / ".trash-old"
    (trash / "nested").mkdir(parents=True)
    victim = trash / "nested" / "file.txt"
    victim.write_text("do not delete", encoding="utf-8")
    parent = tmp_path / "component-runtimes"
    parent.symlink_to(external, target_is_directory=True)

    swept = sweep_trash(parent)
    assert swept["removed"] == 0
    assert victim.is_file()

    pruned = prune_component_runtimes(
        parent, keep_paths=[], keep_recent_verified=0, budget_seconds=5.0,
        live_paths=leases.LivePaths(), inspection_complete=True)
    assert pruned["status"] == "refused_link"
    assert pruned["reclaimed_dirs"] == 0
    assert victim.is_file()
    assert trash.is_dir()


def test_component_prune_protects_exported_source_root_without_git(
        tmp_path, isolated_machine_home):
    source = tmp_path / "exported-source"          # deliberately no .git
    protected_root = source / "component-runtimes"
    protected = [_runtime(protected_root, "exported", index, mtime=1000 + index)
                 for index in range(2)]
    config_home = isolated_machine_home / ".config" / "mindie-agent"
    config_home.mkdir(parents=True)
    (config_home / "codex.json").write_text(
        json.dumps({"source_root": str(source)}), encoding="utf-8")

    result = prune_component_runtimes(
        protected_root, keep_paths=[], keep_recent_verified=0, budget_seconds=5.0,
        live_paths=leases.LivePaths(), inspection_complete=True)
    assert result["reclaimed_dirs"] == 0
    assert all(entry.is_dir() for entry in protected)

    control_root = tmp_path / "unprotected" / "component-runtimes"
    control = [_runtime(control_root, "control", index, mtime=1000 + index)
               for index in range(2)]
    control_result = prune_component_runtimes(
        control_root, keep_paths=[], keep_recent_verified=0, budget_seconds=5.0,
        live_paths=leases.LivePaths(), inspection_complete=True)
    assert control_result["reclaimed_dirs"] == 2
    assert not any(entry.exists() for entry in control)


def test_legacy_component_prune_protects_exported_source_root_without_git(tmp_path):
    project = tmp_path / "exported-source"          # deliberately no .git
    runtimes = project / "component-runtimes"
    generations = [_runtime(runtimes, "exported", index, mtime=1000 + index)
                   for index in range(4)]
    (project / "codex.json").write_text(
        json.dumps({"source_root": str(project)}), encoding="utf-8")

    result = legacy.sweep_legacy_installations(
        config_home=project, data_home=project / "data",
        codex_home=tmp_path / "codex", budget_seconds=60.0, force=True)

    assert result["reclaimed_dirs"] == 0
    assert all(entry.is_dir() for entry in generations)


def test_windows_retirement_holds_no_handle_inside_entry(tmp_path, monkeypatch):
    root = tmp_path / "component-runtimes"
    generation = _runtime(root, "windows", 0, mtime=1000)
    open_paths = {}
    real_open, real_close, real_replace = os.open, os.close, os.replace

    def tracking_open(path, flags, mode=0o777, *, dir_fd=None):
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        open_paths[descriptor] = Path(os.fsdecode(path))
        return descriptor

    def tracking_close(descriptor):
        open_paths.pop(descriptor, None)
        real_close(descriptor)

    def guarded_replace(source, destination):
        target = Path(os.fsdecode(source))
        if any(path == target or target in path.parents
               for path in open_paths.values()):
            # NTFS rejects a directory rename while any handle inside is open,
            # even when the holder is this very process.
            raise PermissionError(13, "WinError 32: the directory is in use")
        return real_replace(source, destination)

    observed = {}
    real_acquire = leases.acquire_retirement_locks

    def tracking_acquire(directory):
        locks = real_acquire(directory)
        if locks is not None:
            observed["sibling_only"] = locks.sibling_only
            observed["inside"] = locks.descriptors_inside_entry
            observed["sibling"] = (
                Path(directory).parent / leases.RETIRE_LOCK_NAME).is_file()
        return locks

    monkeypatch.setattr(leases, "_IS_WINDOWS", True)
    monkeypatch.setattr(lifecycle, "acquire_retirement_locks", tracking_acquire)
    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "close", tracking_close)
    monkeypatch.setattr(os, "replace", guarded_replace)

    outcome = retire_directory(generation)

    assert outcome["status"] == "retired"
    assert outcome["trash_removed"] is True
    assert not generation.exists()
    assert not list(root.glob(".trash-*"))
    assert not open_paths
    assert observed == {"sibling_only": True, "inside": (), "sibling": True}


def test_windows_retirement_defers_on_builder_lock_without_handles(tmp_path, monkeypatch):
    root = tmp_path / "component-runtimes"
    generation = _runtime(root, "windows-lock", 0, mtime=1000)
    descriptor = os.open(generation / "prepare.lock", os.O_CREAT | os.O_RDWR, 0o600)
    lock_file(descriptor, exclusive=True)
    try:
        monkeypatch.setattr(leases, "_IS_WINDOWS", True)
        outcome = retire_directory(generation)
    finally:
        unlock_file(descriptor)
        os.close(descriptor)

    assert outcome["status"] == "deferred_in_use"
    assert outcome["error"] == "LeaseHeld"
    assert generation.is_dir()
    assert not list(root.glob(".trash-*"))
    assert (root / leases.RETIRE_LOCK_NAME).is_file()


def test_builder_rejects_generation_moved_after_prepare_lock(tmp_path, monkeypatch):
    source, root, baseline, targets, generation = _build_fixture(tmp_path, monkeypatch)
    trash = root / (".trash-" + generation.name[:16])
    if os.name == "nt":
        # NTFS refuses to rename a directory while prepare.lock is open inside
        # it. The equivalent Windows race window is between the pre-lock stat
        # and os.open; move the generation exactly there, then let the real
        # lock acquire the replacement directory's prepare.lock.
        real_open = component_runtime.os.open
        moved = False

        def moving_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal moved
            if (not moved and filesystem.path_identity(path) == filesystem.path_identity(
                    generation / "prepare.lock")):
                moved = True
                os.replace(generation, trash)
                generation.mkdir()
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(component_runtime.os, "open", moving_open)
    else:
        real_lock_file = component_runtime.lock_file

        def swapping_lock(descriptor, *, exclusive, blocking=False):
            real_lock_file(descriptor, exclusive=exclusive, blocking=blocking)
            os.replace(generation, trash)
            generation.mkdir()

        monkeypatch.setattr(component_runtime, "lock_file", swapping_lock)

    with pytest.raises(RuntimeError, match="moved while its builder lock"):
        component_runtime.build(source, root, baseline, targets, timeout=120)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ps command-line inspection")
def test_posix_ps_never_falls_back_to_a_truncated_table(tmp_path, monkeypatch):
    executable = tmp_path / "fake-ps"
    record = tmp_path / "ps-calls.txt"
    executable.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_PS_RECORD\"\n"
        "case \" $* \" in\n"
        "  *\" -ww \"*) exit 1 ;;\n"
        "esac\n"
        "printf '%s\\n' '/tmp/truncated/component-runtimes/old'\n",
        encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("FAKE_PS_RECORD", str(record))
    monkeypatch.setattr(_host_exec, "which_native", lambda name: str(executable))

    live, complete = leases._posix_live_paths(timeout=10)

    assert complete is False
    assert not live
    calls = record.read_text(encoding="utf-8").splitlines()
    assert calls == ["-ww -axo command="]


def test_explicit_scoped_roots_never_scan_real_home_uv_cache(
        tmp_path, isolated_machine_home):
    cache = isolated_machine_home / ".cache" / "mindie-agent-dsh" / "uv-cache"
    cache.mkdir(parents=True)
    payload = cache / "blob.bin"
    payload.write_text("keep", encoding="utf-8")
    os.utime(cache, (1000, 1000))

    result = legacy.sweep_legacy_installations(
        config_home=tmp_path / "config", data_home=tmp_path / "data",
        codex_home=tmp_path / "codex", budget_seconds=60.0, force=True)

    assert result["status"] == "completed"
    assert cache.is_dir()
    assert payload.is_file()
    discovered = legacy._discover_targets(
        [tmp_path / "config"], [tmp_path / "data"], tmp_path / "codex",
        include_home_roots=False)
    assert all(target["path"] != cache for target in discovered)


def test_default_production_mode_still_reclaims_home_uv_cache(
        isolated_machine_home):
    cache = isolated_machine_home / ".cache" / "mindie-agent-dsh" / "uv-cache"
    (cache / "vendor").mkdir(parents=True)
    (cache / "vendor" / "blob.bin").write_text("x", encoding="utf-8")
    os.utime(cache, (1000, 1000))

    result = legacy.sweep_legacy_installations(budget_seconds=60.0, force=True)

    assert result["status"] in {"completed", "partial", "inspection_incomplete"}
    assert not cache.exists()


def test_deadline_expiry_during_snapshot_is_partial_not_success(tmp_path, monkeypatch):
    root = tmp_path / "trash-tree"
    (root / "nested").mkdir(parents=True)
    victim = root / "nested" / "file.txt"
    victim.write_text("x", encoding="utf-8")

    assert lifecycle._snapshot(root, deadline=time.monotonic() - 1) \
        is lifecycle._SNAPSHOT_EXPIRED
    real_snapshot = lifecycle._snapshot

    def expiring(path, deadline=None):
        if Path(path).name == "nested":
            return lifecycle._SNAPSHOT_EXPIRED
        return real_snapshot(path, deadline)

    monkeypatch.setattr(lifecycle, "_snapshot", expiring)
    assert lifecycle._bounded_rmtree(root, time.monotonic() + 60) is False
    assert victim.is_file()

    monkeypatch.setattr(lifecycle, "_snapshot",
                        lambda path, deadline=None: lifecycle._SNAPSHOT_UNREADABLE)
    assert lifecycle._bounded_rmtree(root, None) is False
    assert root.is_dir()


def test_workbuddy_scan_checks_deadline_on_every_entry(tmp_path, isolated_machine_home):
    from mindie_knowledge.storage import legacy_roots

    cache = isolated_machine_home / ".workbuddy" / "plugins" / "cache"
    for index in range(50):
        plugin = cache / f"filler-{index}" / "mindie-agent" / "plugin"
        plugin.mkdir(parents=True)
        (plugin / "notes.txt").write_text("x", encoding="utf-8")

    # No matching filename exists yet: an expired budget must still stop the
    # enumeration instead of walking every directory and returning a clean scan.
    with pytest.raises(TimeoutError):
        legacy_roots._scan_workbuddy(set(), deadline=time.monotonic() - 1)

    tool_paths = [(tmp_path / f"opt/tool-{index}/bin/python").resolve()
                  for index in range(2)]
    for index in range(2):
        plugin = cache / f"filler-{index}" / "mindie-agent" / "plugin"
        (plugin / "mcp.json").write_text(
            json.dumps({"command": str(tool_paths[index])}),
            encoding="utf-8")
    protected = set()
    legacy_roots._scan_workbuddy(protected, deadline=time.monotonic() + 60)
    for tool_path in tool_paths:
        assert Path(os.path.normpath(str(tool_path))) in protected


def _zero_identity(info):
    class ZeroIdentity:
        st_dev = 0
        st_ino = 0

        def __getattr__(self, name):
            return getattr(info, name)

    return ZeroIdentity()


def test_scandir_walk_traverses_when_direntry_stat_has_no_identity(
        tmp_path, monkeypatch):
    """Windows ``DirEntry.stat`` reports ``(0, 0)`` for every directory."""
    from mindie_knowledge.storage import legacy_roots

    root = tmp_path / "plugins"
    target = root / "releases" / "0.1.23" / "entry.py"
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")

    class ZeroStatEntry:
        def __init__(self, entry):
            self._entry = entry

        def __getattr__(self, name):
            return getattr(self._entry, name)

        def stat(self, *, follow_symlinks=True):
            return _zero_identity(
                self._entry.stat(follow_symlinks=follow_symlinks))

    class ZeroStatScandir:
        def __init__(self, iterator):
            self._iterator = iterator

        def __iter__(self):
            return self

        def __next__(self):
            return ZeroStatEntry(next(self._iterator))

        def __enter__(self):
            self._iterator.__enter__()
            return self

        def __exit__(self, *exc_info):
            return self._iterator.__exit__(*exc_info)

    real_scandir = os.scandir
    monkeypatch.setattr(
        legacy_roots.os, "scandir",
        lambda path=".": ZeroStatScandir(real_scandir(path)))

    entries = list(legacy_roots._scandir_walk(root, max_depth=6))

    assert (root / "releases" / "0.1.23", True) in entries
    assert (target, False) in entries


def test_scandir_walk_never_skips_directories_on_a_zero_inode_filesystem(
        tmp_path, monkeypatch):
    """FAT/exFAT ``os.stat`` returns ``(0, 0)``; depth is the only bound."""
    from mindie_knowledge.storage import legacy_roots

    root = tmp_path / "plugins"
    target = root / "releases" / "0.1.23" / "entry.py"
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")
    real_stat = legacy_roots.os.stat
    monkeypatch.setattr(
        legacy_roots.os, "stat",
        lambda path, *args, **kwargs: _zero_identity(
            real_stat(path, *args, **kwargs)))

    entries = list(legacy_roots._scandir_walk(root, max_depth=6))

    assert (root / "releases" / "0.1.23", True) in entries
    assert (target, False) in entries


def test_legacy_entry_scan_honours_deadline(tmp_path):
    root = tmp_path / "runtimes"
    for index in range(10):
        (root / f"generation-{index}").mkdir(parents=True)

    with pytest.raises(TimeoutError):
        legacy._entries(root, deadline=time.monotonic() - 1)
    assert len(legacy._entries(root, deadline=time.monotonic() + 60)) == 10


# --------------------------------------------------------------------------- #
# R5 surgical regressions: linked target roots, forbidden trash parents and
# end-of-sweep deadline accounting.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("link_kind", ["symlink", "intermediate", "junction"])
def test_linked_generations_root_is_refused_before_any_sweep(
        tmp_path, monkeypatch, link_kind):
    data = tmp_path / "data"
    generations = data / "dsh" / "update" / "generations"
    if link_kind == "intermediate":
        (data / "dsh").mkdir(parents=True)
        external = tmp_path / "external-update"
        (data / "dsh" / "update").symlink_to(external, target_is_directory=True)
        target_root = external / "generations"
    else:
        generations.parent.mkdir(parents=True)
        if link_kind == "symlink":
            external = tmp_path / "external-generations"
            generations.symlink_to(external, target_is_directory=True)
            target_root = external
        else:
            # NTFS junctions are directory reparse points: keep the real
            # directory and expose only the junction metadata to the sweep.
            generations.mkdir()
            target_root = generations
            monkeypatch.setattr(legacy, "is_junction",
                                lambda path: Path(path) == generations)
    kept = target_root / "v0.1.0"
    (kept / "package").mkdir(parents=True)
    asset = kept / "asset.tgz"
    asset.write_text("precious", encoding="utf-8")
    os.utime(kept, (1000, 1000))
    trash = target_root / ".trash-kept"
    trash.mkdir()
    sentinel = trash / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    result = legacy.sweep_legacy_installations(
        config_home=tmp_path / "config", data_home=data,
        codex_home=tmp_path / "codex", budget_seconds=30.0, force=True)

    assert result["reclaimed_files"] == 0
    assert asset.is_file()
    assert asset.read_text(encoding="utf-8") == "precious"
    assert sentinel.is_file()
    assert any("protected target" in warning for warning in result["warnings"])


def test_component_prune_refuses_protected_trash_before_sweeping(
        tmp_path, isolated_machine_home):
    source = tmp_path / "exported-source"          # deliberately no .git
    root = source / "component-runtimes"
    trash = root / ".trash-user-data"
    trash.mkdir(parents=True)
    keep = trash / "keep.txt"
    keep.write_text("keep", encoding="utf-8")

    explicit = prune_component_runtimes(
        root, keep_paths=[], keep_recent_verified=0, budget_seconds=5.0,
        live_paths=leases.LivePaths(), inspection_complete=True,
        include_global_roots=False, forbidden_roots=[source])
    assert explicit["reclaimed_trash"] == 0
    assert keep.is_file()
    assert sweep_trash(root, forbidden_roots=[source])["removed"] == 0
    assert keep.is_file()

    config_home = isolated_machine_home / ".config" / "mindie-agent"
    config_home.mkdir(parents=True)
    (config_home / "codex.json").write_text(
        json.dumps({"source_root": str(source)}), encoding="utf-8")
    configured = prune_component_runtimes(
        root, keep_paths=[], keep_recent_verified=0, budget_seconds=5.0,
        live_paths=leases.LivePaths(), inspection_complete=True)
    assert configured["reclaimed_trash"] == 0
    assert keep.is_file()

    git_source = tmp_path / "git-source"
    (git_source / ".git").mkdir(parents=True)
    git_root = git_source / "component-runtimes"
    git_trash = git_root / ".trash-user-data"
    git_trash.mkdir(parents=True)
    git_keep = git_trash / "keep.txt"
    git_keep.write_text("keep", encoding="utf-8")
    git_result = prune_component_runtimes(
        git_root, keep_paths=[], keep_recent_verified=0, budget_seconds=5.0,
        live_paths=leases.LivePaths(), inspection_complete=True,
        include_global_roots=False)
    assert git_result["status"] == "protected_root"
    assert git_keep.is_file()


class _SteppedClock:
    def __init__(self, start=1000.0, step=0.1):
        self.now = start
        self.step = step

    def monotonic(self):
        value = self.now
        self.now += self.step
        return value

    def time(self):
        return time.time()


def test_stepped_clock_expiry_in_discovery_and_leftover_is_partial(
        tmp_path, monkeypatch):
    from mindie_knowledge.storage import legacy_roots

    def install(clock):
        monkeypatch.setattr(legacy, "time", clock)
        monkeypatch.setattr(lifecycle, "time", clock)
        monkeypatch.setattr(legacy_roots, "time", clock)

    discover_clock = _SteppedClock(start=1000.0, step=0.1)
    install(discover_clock)
    discovered = legacy.sweep_legacy_installations(
        config_home=tmp_path / "config", data_home=tmp_path / "data",
        codex_home=tmp_path / "codex", budget_seconds=0.35, force=True)
    assert discovered["partial"] is True
    assert discovered["status"] == "partial"
    assert discover_clock.now - 1000.0 <= 1.0

    target = tmp_path / "data" / "runtimes"
    target.mkdir(parents=True)
    for index in range(5):
        (target / f"entry-{index}").mkdir()
    monkeypatch.setattr(
        legacy, "_discover_targets",
        lambda *args, **kwargs: [{"kind": "runtimes", "path": target,
                                  "harness": "dsh", "root": tmp_path / "data"}])
    marker = tmp_path / "data" / legacy.STATE_MARKER
    marker.write_text(json.dumps({"schema": legacy.MARKER_SCHEMA,
                                  "at": time.time()}), encoding="utf-8")
    leftover_clock = _SteppedClock(start=2000.0, step=0.1)
    install(leftover_clock)
    leftover = legacy.sweep_legacy_installations(
        config_home=tmp_path / "config", data_home=tmp_path / "data",
        codex_home=tmp_path / "codex", budget_seconds=0.25, force=False)
    assert leftover["partial"] is True
    assert leftover["status"] == "partial"
    assert leftover_clock.now - 2000.0 <= 1.0


# --------------------------------------------------------------------------- #
# R6 surgical regressions: per-generation source-root/.git protection before
# asset unlinking, and per-candidate trash / bounded-rmtree Git exclusion.
# --------------------------------------------------------------------------- #

def _kept_dsh_generation(data: Path, name: str = "v0.1.0"):
    entry = data / "dsh" / "update" / "generations" / name
    (entry / "package").mkdir(parents=True)
    (entry / "asset.tgz").write_text("tarball", encoding="utf-8")
    os.utime(entry, (1000, 1000))
    return entry, entry / "asset.tgz"


def test_kept_dsh_generation_in_source_root_keeps_asset(tmp_path):
    data = tmp_path / "data"
    entry, asset = _kept_dsh_generation(data)
    config = tmp_path / "config"
    config.mkdir()
    (config / "codex.json").write_text(
        json.dumps({"source_root": str(entry)}), encoding="utf-8")

    result = legacy.sweep_legacy_installations(
        config_home=config, data_home=data, codex_home=tmp_path / "codex",
        budget_seconds=30.0, force=True)

    assert result["reclaimed_files"] == 0
    assert result["reclaimed_dirs"] == 0
    assert entry.is_dir()
    assert asset.is_file()
    assert asset.read_text(encoding="utf-8") == "tarball"


def test_kept_dsh_generation_containing_git_keeps_asset(tmp_path):
    data = tmp_path / "data"
    entry, asset = _kept_dsh_generation(data, "v0.1.1")
    (entry / ".git" / "objects").mkdir(parents=True)
    head = entry / ".git" / "HEAD"
    head.write_text("ref: refs/heads/main\n", encoding="utf-8")

    result = legacy.sweep_legacy_installations(
        config_home=tmp_path / "config", data_home=data,
        codex_home=tmp_path / "codex", budget_seconds=30.0, force=True)

    assert result["reclaimed_files"] == 0
    assert result["reclaimed_dirs"] == 0
    assert entry.is_dir()
    assert asset.is_file()
    assert head.is_file()


def _trash_candidates(root: Path):
    user_trash = root / ".trash-user-data"
    (user_trash / "nested").mkdir(parents=True)
    user_keep = user_trash / "nested" / "keep.txt"
    user_keep.write_text("keep", encoding="utf-8")

    git_trash = root / ".trash-git"
    (git_trash / ".git" / "objects").mkdir(parents=True)
    (git_trash / ".git" / "HEAD").write_text(
        "ref: refs/heads/main\n", encoding="utf-8")
    git_keep = git_trash / ".git" / "objects" / "pack.idx"
    git_keep.write_text("keep", encoding="utf-8")
    worktree = git_trash / "worktree.txt"
    worktree.write_text("keep", encoding="utf-8")
    return user_trash, user_keep, git_trash, git_keep, worktree


def _assert_trash_untouched(user_trash, user_keep, git_trash, git_keep,
                            worktree):
    assert user_trash.is_dir() and user_keep.is_file()
    assert git_trash.is_dir() and git_keep.is_file() and worktree.is_file()


def test_trash_and_prune_protect_candidate_root_and_git_marker(tmp_path):
    root = tmp_path / "component-runtimes"
    root.mkdir()
    user_trash, user_keep, git_trash, git_keep, worktree = \
        _trash_candidates(root)
    forbidden = [user_trash, git_trash / ".git"]

    swept = sweep_trash(root, forbidden_roots=forbidden)
    assert swept["removed"] == 0
    assert swept["remaining"] == 2
    assert swept["partial"] is False
    assert any("trash protected" in warning for warning in swept["warnings"])
    _assert_trash_untouched(user_trash, user_keep, git_trash, git_keep,
                            worktree)

    pruned = prune_component_runtimes(
        root, keep_paths=[], keep_recent_verified=0, budget_seconds=5.0,
        live_paths=leases.LivePaths(), inspection_complete=True,
        include_global_roots=False, forbidden_roots=forbidden)
    assert pruned["reclaimed_trash"] == 0
    assert pruned["reclaimed_dirs"] == 0
    _assert_trash_untouched(user_trash, user_keep, git_trash, git_keep,
                            worktree)


def test_trash_git_marker_is_protected_without_forbidden_root(tmp_path):
    root = tmp_path / "component-runtimes"
    root.mkdir()
    git_trash = root / ".trash-git"
    (git_trash / ".git" / "objects").mkdir(parents=True)
    keep = git_trash / ".git" / "objects" / "pack.idx"
    keep.write_text("keep", encoding="utf-8")

    swept = sweep_trash(root)
    assert swept["removed"] == 0
    assert swept["remaining"] == 1
    assert git_trash.is_dir() and keep.is_file()

    pruned = prune_component_runtimes(
        root, keep_paths=[], keep_recent_verified=0, budget_seconds=5.0,
        live_paths=leases.LivePaths(), inspection_complete=True,
        include_global_roots=False)
    assert pruned["reclaimed_trash"] == 0
    assert git_trash.is_dir() and keep.is_file()


@pytest.mark.parametrize("nested", [False, True])
def test_bounded_rmtree_never_deletes_a_git_tree(tmp_path, nested):
    root = tmp_path / ".trash-git"
    repo = root / "worktree" if nested else root
    (repo / ".git" / "objects").mkdir(parents=True)
    head = repo / ".git" / "HEAD"
    head.write_text("ref: refs/heads/main\n", encoding="utf-8")

    assert lifecycle._bounded_rmtree(root, time.monotonic() + 60) is False
    assert repo.is_dir() and head.is_file()


def test_bounded_rmtree_refuses_a_path_inside_git_metadata(tmp_path):
    objects = tmp_path / ".git" / "objects"
    objects.mkdir(parents=True)
    pack = objects / "pack.idx"
    pack.write_text("keep", encoding="utf-8")

    assert lifecycle._bounded_rmtree(objects, time.monotonic() + 60) is False
    assert objects.is_dir() and pack.is_file()


def test_nested_source_root_inside_an_excess_generation_is_kept(tmp_path):
    data = tmp_path / "data"
    generations = data / "dsh" / "update" / "generations"
    oldest, asset = _kept_dsh_generation(data, "v0.1.0")
    nested_source = oldest / "exported-source"
    nested_source.mkdir()
    sentinel = nested_source / "main.c"
    sentinel.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    # Creating the nested source refreshed the parent mtime: restore the
    # ordering so this generation is the one retention would otherwise retire.
    os.utime(oldest, (1000, 1000))
    for index, name in enumerate(("v0.1.1", "v0.1.2"), start=1):
        newer = generations / name
        (newer / "package").mkdir(parents=True)
        os.utime(newer, (1000 + index, 1000 + index))
    config = tmp_path / "config"
    config.mkdir()
    (config / "codex.json").write_text(
        json.dumps({"source_root": str(nested_source)}), encoding="utf-8")

    result = legacy.sweep_legacy_installations(
        config_home=config, data_home=data, codex_home=tmp_path / "codex",
        budget_seconds=30.0, force=True)

    assert result["reclaimed_dirs"] == 0
    assert result["reclaimed_files"] == 0
    assert oldest.is_dir() and sentinel.is_file() and asset.is_file()


def test_component_entry_containing_a_nested_source_root_is_kept(tmp_path):
    root = tmp_path / "component-runtimes"
    entry = _runtime(root, "nested-source", 0, mtime=1000)
    nested_source = entry / "exported-source"
    nested_source.mkdir()
    sentinel = nested_source / "main.c"
    sentinel.write_text("int main(void) { return 0; }\n", encoding="utf-8")

    result = prune_component_runtimes(
        root, keep_paths=[], keep_recent_verified=0, budget_seconds=5.0,
        live_paths=leases.LivePaths(), inspection_complete=True,
        include_global_roots=False, forbidden_roots=[nested_source])

    assert result["reclaimed_dirs"] == 0
    assert entry.is_dir() and sentinel.is_file()


def test_leaf_unlink_and_single_sweep_refuse_git_and_forbidden_paths(tmp_path):
    source = tmp_path / "source"
    protected_file = source / "state.json"
    protected_file.parent.mkdir(parents=True)
    protected_file.write_text("keep", encoding="utf-8")
    git_file = tmp_path / "checkout" / ".git" / "config"
    git_file.parent.mkdir(parents=True)
    git_file.write_text("keep", encoding="utf-8")
    account = {"reclaimed_dirs": 0, "reclaimed_files": 0, "kept_dirs": 0,
               "warnings": [], "partial": False}

    legacy._unlink_file(protected_file, account, forbidden_roots=[source])
    legacy._unlink_file(git_file, account)
    assert account["reclaimed_files"] == 0
    assert protected_file.is_file() and git_file.is_file()

    single = tmp_path / "dsh" / "venv"
    (single / "bin").mkdir(parents=True)
    (single / "bin" / "python").write_text("", encoding="utf-8")
    os.utime(single, (1000, 1000))
    legacy._sweep_single(
        {"kind": "dsh-venv", "path": single}, [], leases.LivePaths(), True,
        time.monotonic() + 60, account, forbidden_roots=[single])
    assert single.is_dir()
    assert account["reclaimed_dirs"] == 0
    assert account["kept_dirs"] == 1
