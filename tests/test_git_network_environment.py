"""Real Git reads prove scoped proxy/CA settings survive product options."""
import os
import subprocess

import pytest

from mindie_knowledge.community import gitops
from mindie_knowledge.gitbin import resolve_git
from mindie_knowledge.loop.feed import _feed_git_env
from mindie_knowledge.publication_check import _git_env


@pytest.mark.parametrize("environment", [
    lambda: gitops._resolve_git_env(None),
    _feed_git_env,
    _git_env,
])
def test_caller_git_transport_settings_survive_owned_configuration(tmp_path, monkeypatch, environment):
    configuration = tmp_path / "gitconfig"
    original = "[http]\nsslBackend = schannel\n"
    configuration.write_text(original, encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(configuration))
    selected = [("http.proxy", "http://enterprise-fixture.invalid:8080"),
                ("http.sslCAInfo", str(tmp_path / "Company CA.pem")),
                ("http.sslVerify", "true"),
                ("http.schannelCheckRevoke", "false")]
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(selected)))
    for index, (key, value) in enumerate(selected):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", value)
    before = {key: value for key, value in os.environ.items() if key.startswith("GIT_CONFIG_")}
    effective = environment()
    result = subprocess.run(
        [resolve_git(), "config", "--get-regexp", r"^http\."],
        env=effective, cwd=tmp_path, capture_output=True, text=True, timeout=5, check=True,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )
    observed = dict(line.split(" ", 1) for line in result.stdout.splitlines())
    assert observed == {"http.sslbackend": "schannel", **{key.lower(): value for key, value in selected}}
    assert configuration.read_text(encoding="utf-8") == original
    assert {key: value for key, value in os.environ.items()
            if key.startswith("GIT_CONFIG_")} == before
