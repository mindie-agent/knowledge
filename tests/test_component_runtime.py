"""Independent locks override old direct and transitive pins without editing source."""
import hashlib
from pathlib import Path
import tomllib

import pytest

from mindie_knowledge.component_runtime import project_text, source_files, source_identity
from mindie_knowledge.runtime_inventory import verify_lock
from test_component_releases import CapturedCatalog


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
