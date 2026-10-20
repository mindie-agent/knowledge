import json
import os
from pathlib import Path
import subprocess
import sys

from mindie_knowledge.loop import service_context


def test_default_network_context_crosses_real_service_process_boundary(tmp_path, monkeypatch):
    selected = {
        "HTTPS_PROXY": "http://fixture:encoded%40password@proxy.invalid:8080",
        "SSL_CERT_FILE": str(tmp_path / "Company CA.pem"),
        "UV_SYSTEM_CERTS": "true",
        "UV_DEFAULT_INDEX": "https://packages.example.invalid/simple",
        "GH_CONFIG_DIR": str(tmp_path / "github"),
        "GH_TOKEN": "synthetic-one-use-token",
    }
    for key in service_context.NETWORK_ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in selected.items():
        monkeypatch.setenv(key, value)
    config, binding = {}, {"attempt": "network-test"}
    path = tmp_path / "launch.context"
    path.write_bytes(service_context.capture(config, binding))
    path.chmod(0o600)
    env = {k: v for k, v in os.environ.items() if k not in service_context.NETWORK_ENV}
    env["PYTHONPATH"] = str(Path(service_context.__file__).resolve().parents[2])
    result = subprocess.run(
        [sys.executable, "-c",
         "import json,os,sys;"
         "from mindie_knowledge.loop.service_context import consume,apply;"
         "apply({},consume(sys.argv[1],{}, {'attempt':'network-test'}));"
         "print(json.dumps({k:os.environ[k] for k in json.loads(sys.argv[2])}))",
         str(path), json.dumps(list(selected))],
        env=env, capture_output=True, text=True, timeout=5, check=True,
    )
    assert json.loads(result.stdout) == selected
    assert not path.exists()


def test_absent_proxy_removes_stale_service_environment(monkeypatch):
    for key in service_context.NETWORK_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://previous-network.invalid")
    monkeypatch.setenv("GH_TOKEN", "stale-service-token")
    monkeypatch.setenv("UNRELATED_SETTING", "keep")
    service_context.apply({}, {})
    assert "HTTPS_PROXY" not in os.environ
    assert "GH_TOKEN" not in os.environ
    assert os.environ["UNRELATED_SETTING"] == "keep"
