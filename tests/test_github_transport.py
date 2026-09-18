"""Token-only callers need no gh install, persisted secret, or global Git edit."""
from __future__ import annotations

import base64
import io
import json
import subprocess
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from mindie_knowledge.contribution.consent import ContributionPaused
from mindie_knowledge.contribution.github import GitHubError, UrllibContributionGitHub, _NoRedirect
from mindie_knowledge.github_transport import ensure_fork, github_token, git_environment, redact_credentials
from contribution.support import init_git_repo


@pytest.mark.parametrize("name", ["GH_TOKEN", "GITHUB_TOKEN"])
def test_environment_token_needs_no_github_cli(monkeypatch, name):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv(name, "fixture-token-not-a-credential")
    with patch("mindie_knowledge.github_transport.gh", side_effect=AssertionError("gh must not run")):
        assert github_token() == "fixture-token-not-a-credential"


def test_missing_token_and_cli_has_actionable_redacted_error(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with patch("mindie_knowledge.github_transport.gh", side_effect=FileNotFoundError):
        with pytest.raises(RuntimeError, match="GH_TOKEN/GITHUB_TOKEN"):
            github_token()


@pytest.mark.parametrize("token", ["fixture-secret\nsecond-line", " fixture-secret", "fixture-secret\x01"])
def test_invalid_token_never_enters_http_error_messages(monkeypatch, token):
    monkeypatch.setenv("GH_TOKEN", token)
    with pytest.raises(RuntimeError) as caught:
        github_token()
    assert "fixture-secret" not in str(caught.value)
    with pytest.raises(Exception) as transport_error:
        UrllibContributionGitHub(token)
    assert "fixture-secret" not in str(transport_error.value)


def test_git_auth_is_ephemeral_scoped_and_inherited_trace_is_disabled(tmp_path, monkeypatch):
    token = "fixture-token-not-a-credential"
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Existing Config")
    monkeypatch.setenv("GIT_TRACE_CURL", str(tmp_path / "must-not-create.log"))
    monkeypatch.setenv("GIT_CURL_VERBOSE", "1")
    env = git_environment(token)
    assert env["GIT_CONFIG_VALUE_0"] == "Existing Config"
    assert not any(key.startswith("GIT_TRACE") for key in env)
    assert "GIT_CURL_VERBOSE" not in env
    scoped = {env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"]
              for index in range(int(env["GIT_CONFIG_COUNT"]))}
    assert scoped["credential.https://github.com.helper"] == ""
    authorization = "Authorization: Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()
    # A real Git child consumes the environment. No credentials enter argv or config files.
    repo = tmp_path / "git"
    init_git_repo(repo)
    before = (repo / ".git/config").read_bytes()
    command = ["git", "-C", str(repo), "config", "--get-all", "http.https://github.com/.extraHeader"]
    result = subprocess.run(command, env=env, capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ["", authorization]
    assert token not in " ".join(command) and authorization not in " ".join(command)
    assert (repo / ".git/config").read_bytes() == before
    assert redact_credentials(authorization, env=env) == "[redacted]"
    assert redact_credentials(authorization.rsplit(" ", 1)[-1], env=env) == "[redacted]"
    assert not (tmp_path / "must-not-create.log").exists()


class ForkApi:
    def __init__(self, *, existing=True, mismatch=False):
        self.calls = []
        self.existing = existing
        self.mismatch = mismatch

    def get(self, path):
        self.calls.append(("GET", path))
        if path == "/user":
            return {"login": "maoxx241", "id": 123}
        if path == "/repos/maoxx241/corpus":
            if not self.existing:
                raise GitHubError(404, path)
            return {"parent": {"full_name": "wrong/corpus" if self.mismatch else "example/corpus"}}
        if path == "/repos/example/corpus":
            return {"default_branch": "main"}
        raise AssertionError(path)

    def post(self, path, body):
        self.calls.append(("POST", path))
        assert path == "/repos/example/corpus/forks" and body == {}
        return {"parent": {"full_name": "example/corpus"}}


@pytest.mark.parametrize("existing", [True, False])
def test_token_only_fork_setup_with_real_private_clone(tmp_path, monkeypatch, existing):
    monkeypatch.setenv("GH_TOKEN", "fixture-token-not-a-credential")
    source = tmp_path / "source"
    init_git_repo(source)
    destination = tmp_path / "dedicated-clone"
    api = ForkApi(existing=existing)
    from mindie_knowledge.contribution import gitops
    native_git = gitops.run_git
    clone_calls = []

    def git(repo, args, **kwargs):
        if args[0] == "clone":
            assert args[1] == "https://github.com/maoxx241/corpus.git"
            assert "fixture-token" not in " ".join(args)
            assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
            clone_calls.append(args)
            # Real independent clone; substitute only the network endpoint.
            result = native_git(repo, ["clone", "--no-local", str(source), str(destination)], **kwargs)
            native_git(destination, ["remote", "set-url", "origin", args[1]])
            return result
        return native_git(repo, args, **kwargs)

    with patch("mindie_knowledge.github_transport.gh", side_effect=AssertionError("no CLI")), \
         patch("mindie_knowledge.contribution.github.UrllibContributionGitHub", return_value=api), \
         patch.object(gitops, "run_git", side_effect=git):
        result = ensure_fork("example/corpus", destination, github_user="maoxx241")
        again = ensure_fork("example/corpus", destination, github_user="maoxx241")
    assert result == again and result["fork"] == "maoxx241/corpus"
    assert len(clone_calls) == 1
    assert "fixture-token" not in (destination / ".git/config").read_text()
    assert native_git(destination, ["remote", "get-url", "upstream"]).stdout.strip() == "https://github.com/example/corpus.git"


@pytest.mark.parametrize("failure", ["owner", "parent", "consent"])
def test_fork_setup_rejects_wrong_identity_or_withdrawn_consent_before_write(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("GH_TOKEN", "fixture-token-not-a-credential")
    api = ForkApi(existing=failure != "consent", mismatch=failure == "parent")
    # Withdrawal occurs after read-only identity/fork inspection, before fork creation.
    consent_checks = iter([True, False])
    callback = (lambda: next(consent_checks)) if failure == "consent" else None
    with patch("mindie_knowledge.contribution.github.UrllibContributionGitHub", return_value=api):
        with pytest.raises((ValueError, RuntimeError, ContributionPaused)):
            ensure_fork("example/corpus", tmp_path / "fork",
                        github_user="different-user" if failure == "owner" else "maoxx241", authorize=callback)
    assert not (tmp_path / "fork").exists()
    assert not any(call[0] == "POST" for call in api.calls)


def test_api_error_never_echoes_credential_and_redirects_are_denied():
    token = "fixture-token-not-a-credential"
    error = urllib.error.HTTPError("https://api.github.com/user", 403, "forbidden", {}, io.BytesIO(token.encode()))
    with patch("urllib.request.build_opener") as build:
        build.return_value.open.side_effect = error
        with pytest.raises(GitHubError) as caught:
            UrllibContributionGitHub(token).get("/user")
    assert token not in str(caught.value)
    request = urllib.request.Request("https://api.github.com/user")
    assert _NoRedirect().redirect_request(request, None, 302, "Found", {}, "https://example.com") is None
