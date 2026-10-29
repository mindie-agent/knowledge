"""Independent locks override old direct and transitive pins without editing source."""
import hashlib
import importlib.metadata
import json
import tomllib
from pathlib import Path

import pytest
from test_component_releases import CapturedCatalog

from mindie_knowledge.component_runtime import (
    discover,
    project_text,
    source_baseline,
    source_files,
    source_identity,
)
from mindie_knowledge.runtime_inventory import inventory, verify_lock, versions

REMOTE_RELEASE = {"repository": "mindie-agent/remote-dev", "version": "0.10.8",
                  "revision": "4b1c5cab5d36cdaff79d27e4288c6d5f60667c72"}


def test_active_integration_pins_override_bootstrap_without_rewriting_it(tmp_path):
    requirements = tmp_path / "runtime-requirements.txt"
    bootstrap = (
        "mindie-knowledge @ git+https://github.com/mindie-agent/knowledge@"
        "38a42f6f778cfa555d8918fdcacd12cee4a181f5\n"
        "remote-dev @ git+https://github.com/mindie-agent/remote-dev@"
        "0463a13fce9618ec9a51904869d61acb0cdb8538\n"
    )
    requirements.write_text(bootstrap, newline="\n")
    overrides = tmp_path / "runtime" / "active-overrides.txt"
    overrides.parent.mkdir()
    overrides.write_text(
        "mindie-knowledge @ git+https://github.com/mindie-agent/knowledge@"
        "6e921b205c00d592c25f849653cbfb72b73fe318\n", newline="\n")

    bootstrap_baseline = source_baseline(tmp_path)
    baseline = source_baseline(tmp_path, active_overrides=True)

    assert bootstrap_baseline["runtime"] == {
        "mindie-knowledge": "38a42f6f778cfa555d8918fdcacd12cee4a181f5",
        "remote-dev": "0463a13fce9618ec9a51904869d61acb0cdb8538",
    }
    assert baseline["runtime"] == {
        "mindie-knowledge": "6e921b205c00d592c25f849653cbfb72b73fe318",
        "remote-dev": "0463a13fce9618ec9a51904869d61acb0cdb8538",
    }
    assert baseline["source_sha256"] == bootstrap_baseline["source_sha256"]
    assert requirements.read_text() == bootstrap
    overrides.write_text(
        "mindie-knowledge @ git+https://github.com/mindie-agent/knowledge@main\n", newline="\n")
    with pytest.raises(ValueError, match="official repository and full SHA"):
        source_baseline(tmp_path, active_overrides=True)


@pytest.mark.parametrize("active_path", [
    "runtime-active-overrides.txt",
    "runtime/active-overrides.txt",
])
def test_legacy_candidate_decision_cannot_mistake_active_pins_for_bootstrap(
        tmp_path, monkeypatch, active_path):
    import mindie_knowledge.component_runtime as runtime

    released = CapturedCatalog().latest("mindie-knowledge")
    bootstrap = dict(released, version="0.13.1",
                     revision="2e5cf79050a6634a53b9c2cff52d98ca87850e50")
    (tmp_path / "runtime-requirements.txt").write_text(
        "mindie-knowledge @ git+https://github.com/mindie-agent/knowledge@"
        + bootstrap["revision"] + "\n", newline="\n")
    overrides = tmp_path / active_path
    overrides.parent.mkdir(exist_ok=True)
    overrides.write_text(
        "mindie-knowledge @ git+https://github.com/mindie-agent/knowledge@"
        + released["revision"] + "\n", newline="\n")

    class BootstrapCatalog(CapturedCatalog):
        def source(self, name, revision):
            assert name == "mindie-knowledge"
            if revision == bootstrap["revision"]:
                return bootstrap
            return super().source(name, revision)

    # The current host has newer components, but its old builder creates the
    # candidate from runtime-requirements.txt before this selection.
    monkeypatch.setattr(runtime, "read_inventory", lambda python: {"mindie-knowledge": released})
    baseline = source_baseline(tmp_path)
    selected = discover("current-python", baseline["runtime"], catalog=BootstrapCatalog())
    selected_pins = {name: row["revision"] for name, row in selected["runtime"].items()}

    assert selected["changed"] is False
    assert selected_pins != baseline["runtime"]
    project = tomllib.loads(project_text(tmp_path, selected["runtime"]))
    assert project["project"]["dependencies"] == [
        "mindie-knowledge @ git+https://github.com/mindie-agent/knowledge@"
        + released["revision"],
    ]


class RequiredComponentCatalog(CapturedCatalog):
    """Keep the installed remote-dev identity; replay the captured knowledge release."""
    def latest(self, name, *, force=False):
        if name == "remote-dev":
            return dict(REMOTE_RELEASE)
        return super().latest(name, force=force)


@pytest.mark.parametrize("minimum_version", [None, "0.13.1", "0.13.2"])
def test_required_external_component_joins_the_verified_runtime(tmp_path, monkeypatch, minimum_version):
    import mindie_knowledge.component_runtime as runtime

    installed = {"remote-dev": dict(REMOTE_RELEASE)}
    monkeypatch.setattr(runtime, "read_inventory", lambda python: installed)
    catalog = RequiredComponentCatalog()
    selection = discover("managed-python", {}, catalog=catalog,
                         required_components={"mindie-knowledge": minimum_version})

    assert selection["installed"] == installed == {"remote-dev": REMOTE_RELEASE}
    assert selection["changed"] is True
    assert selection["runtime"]["mindie-knowledge"] == CapturedCatalog().latest("mindie-knowledge")
    assert selection["runtime"]["remote-dev"] == REMOTE_RELEASE
    project = tomllib.loads(project_text(tmp_path, selection["runtime"]))
    assert ("mindie-knowledge @ git+https://github.com/mindie-agent/knowledge@"
            "b616a38f77ca17573e4ed22ee23c08c09adefac7") in project["project"]["dependencies"]


def test_external_version_floor_cannot_be_silently_downgraded(monkeypatch):
    import mindie_knowledge.component_runtime as runtime

    installed = {"remote-dev": dict(REMOTE_RELEASE)}
    monkeypatch.setattr(runtime, "read_inventory", lambda python: installed)
    with pytest.raises(ValueError, match="older than the configured component 0.13.3"):
        discover("managed-python", {}, catalog=RequiredComponentCatalog(),
                 required_components={"mindie-knowledge": "0.13.3"})
    assert installed == {"remote-dev": REMOTE_RELEASE}


def test_required_component_still_rejects_an_incomplete_formal_release(monkeypatch):
    import mindie_knowledge.component_runtime as runtime

    monkeypatch.setattr(runtime, "read_inventory", lambda python: {"remote-dev": dict(REMOTE_RELEASE)})
    catalog = RequiredComponentCatalog()
    catalog.release["assets"] = []
    with pytest.raises(ValueError, match="artifacts are incomplete"):
        discover("managed-python", {}, catalog=catalog,
                 required_components={"mindie-knowledge": "0.13.1"})


def test_an_already_managed_component_keeps_its_newer_installed_floor(monkeypatch):
    import mindie_knowledge.component_runtime as runtime

    current = {"mindie-knowledge": dict(CapturedCatalog().latest("mindie-knowledge"), version="0.13.3",
                                       revision="38a42f6f778cfa555d8918fdcacd12cee4a181f5")}
    monkeypatch.setattr(runtime, "read_inventory", lambda python: current)
    catalog = CapturedCatalog()
    selected = discover("managed-python", {}, catalog=catalog,
                        required_components={"mindie-knowledge": "0.13.2"})
    assert selected == {"installed": current, "runtime": current, "changed": False}
    assert sum(path.endswith("/releases/latest") for path in catalog.reads) == 1


def test_external_wheel_metadata_is_only_a_version_floor(tmp_path, monkeypatch):
    distribution = tmp_path / "mindie_profiler-0.5.0.dist-info"
    distribution.mkdir()
    (distribution / "METADATA").write_text(
        "Metadata-Version: 2.3\nName: mindie-profiler\nVersion: 0.5.0\n", newline="\n")
    (distribution / "direct_url.json").write_text(json.dumps({
        "url": "file:///wheels/mindie_profiler-0.5.0-py3-none-any.whl", "archive_info": {},
    }), newline="\n")
    wheel = importlib.metadata.PathDistribution(distribution)

    def installed(name):
        if name != "mindie-profiler":
            raise importlib.metadata.PackageNotFoundError(name)
        return wheel

    monkeypatch.setattr(importlib.metadata, "distribution", installed)
    assert versions(["mindie-profiler"]) == {"mindie-profiler": "0.5.0"}
    with pytest.raises(ValueError, match="no official immutable installation receipt"):
        inventory()
    with pytest.raises(importlib.metadata.PackageNotFoundError):
        versions(["mindie-profiler", "mindie-sim"])


@pytest.mark.parametrize("tls", [{}, {"UV_SYSTEM_CERTS": "false"}, {"UV_NATIVE_TLS": "false"}])
def test_component_download_keeps_enterprise_trust_and_failure_unpublished(tmp_path, monkeypatch, tls):
    import mindie_knowledge.component_runtime as runtime

    source = tmp_path / "adapter"
    source.mkdir()
    (source / "pyproject.toml").write_bytes(b'[project]\nname="adapter"\nversion="0.1.2"\n')
    targets = {"mindie-knowledge": CapturedCatalog().latest("mindie-knowledge")}
    baseline = dict(source_sha256=source_identity(source), runtime={name: row["revision"] for name, row in targets.items()})
    environment = dict(tls, SSL_CERT_FILE="enterprise.pem", HTTPS_PROXY="http://enterprise-proxy:8080")
    observed = {}

    def download(*args, child_env, **kwargs):
        observed.update(child_env)
        raise ConnectionError("component download failed")

    monkeypatch.setattr(runtime.shutil, "which", lambda name: "/tools/uv")
    monkeypatch.setattr(runtime, "bounded_run", download)
    with pytest.raises(ConnectionError, match="component download failed"):
        runtime.build(source, tmp_path / "generations", baseline, targets, env=environment)
    assert observed.items() >= environment.items()
    if not tls:
        assert observed.get("UV_NATIVE_TLS") == "true"
    assert environment == dict(tls, SSL_CERT_FILE="enterprise.pem", HTTPS_PROXY="http://enterprise-proxy:8080")
    assert not list((tmp_path / "generations").rglob("components.json"))
    assert source_identity(source) == baseline["source_sha256"]


def test_generated_project_overrides_transitive_git_pins_and_leaves_adapter_inputs_alone(tmp_path):
    source = tmp_path / "adapter"
    source.mkdir()
    original = b'[project]\nname="native-adapter"\nversion="0.1.2"\n'
    (source / "pyproject.toml").write_bytes(original)
    targets = {"mindie-knowledge": CapturedCatalog().latest("mindie-knowledge")}
    project = tomllib.loads(project_text(source, targets, adapter_distribution="native-adapter"))
    requirement = "mindie-knowledge @ git+https://github.com/mindie-agent/knowledge@" + targets["mindie-knowledge"]["revision"]
    assert requirement in project["project"]["dependencies"]
    assert project["tool"]["uv"]["override-dependencies"] == [requirement]
    assert (source / "pyproject.toml").read_bytes() == original


def test_source_walk_prunes_runtime_trees_before_descending(tmp_path, monkeypatch):
    import mindie_knowledge.component_runtime as runtime
    (tmp_path / "script.py").write_text("pass\n")
    hidden = tmp_path / ".venv/deep"
    hidden.mkdir(parents=True)
    (hidden / "cache.py").write_text("runtime cache\n")
    walk = runtime.os.walk
    visited = []
    def walking(*args, **kwargs):
        for row in walk(*args, **kwargs):
            visited.append(Path(row[0]))
            yield row
    monkeypatch.setattr(runtime.os, "walk", walking)
    assert source_files(tmp_path) == [Path("script.py")]
    assert visited == [tmp_path]
    before = source_identity(tmp_path)
    (hidden / "cache.py").write_text("a different interpreter cache\n")
    assert source_identity(tmp_path) == before
    (tmp_path / "script.py").write_text("print('new adapter')\n")
    assert source_identity(tmp_path) != before


def test_effective_lock_is_checked_in_its_own_runtime_not_the_old_adapter(tmp_path):
    (tmp_path / "uv.lock").write_bytes(b"version = 1\n")
    manifest = {"lock_sha256": hashlib.sha256((tmp_path / "uv.lock").read_bytes()).hexdigest()}
    verify_lock(manifest, tmp_path)
    (tmp_path / "uv.lock").write_bytes(b"version = 2\n")
    with pytest.raises(ValueError, match="lock"):
        verify_lock(manifest, tmp_path)
