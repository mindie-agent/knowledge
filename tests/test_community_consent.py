"""Withdrawal is checked by already-running callers and durable submissions."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from mindie_knowledge.contribution.consent import read_consent
from mindie_knowledge.contribution.pending import iter_pending
from mindie_knowledge.contribution.submit import SubmitConfig, submit_pending
from mindie_knowledge.publishing import configure, current_publishing, publication_allowed, run_once
from mindie_knowledge.server.capture import capture
from mindie_knowledge.server.layers import load_config
from mindie_knowledge.summary_hook import capture_summary

from test_publishing import configured
from contribution.support import FakeContributionGitHub, init_git_repo


def decision(path, *, enabled=True, revision="a" * 32, workspace="1" * 32):
    path.write_text(json.dumps({"schema": "mindie.community.v1", "workspace_id": workspace,
                                "decision": "enabled" if enabled else "disabled", "revision": revision}),
                    encoding="utf-8")


def binding(tmp_path):
    config = configured(tmp_path)
    pointer = tmp_path / "community.json"
    decision(pointer)
    config.publishing["consent_file"] = str(pointer)
    config.shared_sync = {"enabled": False}
    return config, pointer


@pytest.mark.parametrize("payload", [None, "not JSON", [], {},
    {"schema": "mindie.community.v1", "decision": True, "workspace_id": "1" * 32, "revision": "a" * 32},
    {"schema": "mindie.community.v1", "decision": "enabled", "workspace_id": "1" * 31, "revision": "a" * 32},
    {"schema": "mindie.community.v1", "decision": "enabled", "workspace_id": "1" * 32, "revision": False},
    "x" * 16_385])
def test_invalid_or_missing_decision_fails_closed(tmp_path, payload):
    pointer = tmp_path / "community.json"
    if payload is not None:
        pointer.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    assert read_consent(pointer) is None


def test_withdrawal_keeps_summary_local_and_never_requeues_history(tmp_path):
    config, pointer = binding(tmp_path)
    first = capture(title="Enabled observation", content="A new observation made with sharing enabled.",
                    config=config, index=False)
    assert first["contribution"]["status"] == "pending"
    old = iter_pending(config.state_root)[0]
    assert old.consent_revision == "a" * 32 and old.consent_workspace_id == "1" * 32
    decision(pointer, enabled=False, revision="b" * 32)
    summary = capture_summary({"hook_event_name": "Stop", "last_assistant_message":
                               "A private observation made after sharing was withdrawn."}, config=config, client="codex")
    assert summary["status"] == "saved" and summary["contribution"]["status"] == "local_only"
    assert len(list((tmp_path / "candidate").glob("*.md"))) == 2
    with patch("mindie_knowledge.github_transport.github_token", side_effect=AssertionError("no upload authentication")):
        assert run_once(config, force=True)["status"] == "disabled"
        decision(pointer, revision="c" * 32)
        assert run_once(config, force=True)["status"] == "ok"
    assert len(iter_pending(config.state_root)) == 1
    assert not publication_allowed(current_publishing(config), old)
    # Replaying the old event cannot silently renew its publication consent.
    replay = capture(title="Enabled observation", content="A new observation made with sharing enabled.",
                     config=config, index=False)
    assert replay["contribution"]["status"] == "local_only"
    capture(title="Fresh enabled observation", content="This new observation belongs to the newly enabled decision.",
            config=config, index=False)
    assert {record.consent_revision for record in iter_pending(config.state_root)} == {"a" * 32, "c" * 32}


def test_running_service_rereads_config_opt_out_without_restart(tmp_path):
    config, _pointer = binding(tmp_path)
    path = tmp_path / "service.json"
    payload = {"publishing": config.publishing, "shared_sync": {"enabled": False},
               "state_root": str(config.state_root), "layers": {"candidate": {"root": str(tmp_path / "candidate")}}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    running = load_config(path=path, env={})
    payload["publishing"]["enabled"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert running.publishing["enabled"] is True
    with patch("mindie_knowledge.github_transport.github_token", side_effect=AssertionError("stale worker uploads")):
        assert run_once(running, force=True)["status"] == "disabled"
    path.unlink()
    assert not publication_allowed(current_publishing(running))


def test_missing_pointer_field_cannot_bypass_running_consent_binding(tmp_path):
    config, pointer = binding(tmp_path)
    path = tmp_path / "service.json"
    config.config_path = path
    path.write_text(json.dumps({"publishing": {key: value for key, value in config.publishing.items()
                                              if key != "consent_file"}}), encoding="utf-8")
    decision(pointer, enabled=False)
    assert not publication_allowed(current_publishing(config))


def test_record_from_other_workspace_is_not_authorized(tmp_path):
    config, pointer = binding(tmp_path)
    capture(title="Bound observation", content="This observation is specific to one consent owner.", config=config, index=False)
    record = iter_pending(config.state_root)[0]
    decision(pointer, workspace="2" * 32)
    assert not publication_allowed(current_publishing(config), record)


@pytest.mark.parametrize("boundary", ["commit", "pr_lookup", "push"])
def test_withdrawal_during_submit_stops_next_external_write(tmp_path, boundary):
    config, pointer = binding(tmp_path)
    fork = tmp_path / "fork"
    init_git_repo(fork)
    remote = tmp_path / "remote.git"
    if boundary == "push":
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        for name in ("origin", "upstream"):
            subprocess.run(["git", "-C", str(fork), "remote", "add", name, str(remote)], check=True)
        subprocess.run(["git", "-C", str(fork), "push", "origin", "main"], check=True, capture_output=True)
    capture(title="Midflight withdrawal", content="The next external write requires the original decision.",
            config=config, index=False)
    record = iter_pending(config.state_root)[0]
    authorize = lambda: publication_allowed(current_publishing(config), record)
    github = FakeContributionGitHub()
    from mindie_knowledge.contribution import submit
    original_commit = submit.commit_public_file

    def commit(*args, **kwargs):
        result = original_commit(*args, **kwargs)
        if boundary in {"commit", "push"}:
            decision(pointer, enabled=False, revision="b" * 32)
        return result

    original_get = github.get

    def get(path):
        result = original_get(path)
        if boundary == "pr_lookup":
            decision(pointer, enabled=False, revision="b" * 32)
        return result

    github.get = get
    with patch.object(submit, "commit_public_file", side_effect=commit):
        result = submit_pending(record, state_root=config.state_root, public_root=config.state_root / "contribution/public",
                                git_repo=fork, github=github,
                                config=SubmitConfig("example/corpus", "example/corpus", push_remote="origin" if boundary == "push" else None),
                                authorize=authorize)
    assert result.status == "pending"
    assert not any(call[0] == "POST" for call in github.calls)
    if boundary == "push":
        assert subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "--verify", record.branch],
                              capture_output=True).returncode != 0


def test_enabled_setting_alone_never_authorizes_automatic_publication(tmp_path):
    config = configured(tmp_path)
    config.publishing.pop("consent_file")
    assert not publication_allowed(current_publishing(config))
    capture(title="No community decision", content="A publishing flag without an explicit community decision stays private.",
            config=config, index=False)
    assert iter_pending(config.state_root) == []
    with patch("mindie_knowledge.github_transport.ensure_fork", side_effect=AssertionError("no automatic fork")):
        with pytest.raises(ValueError, match="consent-file"):
            configure(tmp_path / "service.json", repository="example/corpus")


def test_configuration_persists_pointer_and_read_only_never_authenticates(tmp_path):
    path = tmp_path / "service.json"
    pointer = tmp_path / "community.json"
    decision(pointer, enabled=False)
    with patch("mindie_knowledge.github_transport.ensure_fork", side_effect=AssertionError("must not fork")):
        result = configure(path, repository="example/corpus", read_only=True, consent_file=pointer, github_user="maoxx241")
        with pytest.raises(ValueError, match="must be enabled"):
            configure(path, repository="example/corpus", consent_file=pointer, github_user="maoxx241")
    payload = json.loads(path.read_text())
    assert result["contributions"] is False
    assert payload["publishing"]["consent_file"] == str(pointer.resolve())
    assert payload["shared_sync"]["enabled"] is True


def test_opt_out_keeps_central_release_downloads_available(tmp_path):
    from types import SimpleNamespace
    from mindie_knowledge.distribution.sync import SyncResult

    config, pointer = binding(tmp_path)
    decision(pointer, enabled=False)
    config.shared_sync = {"enabled": True}
    instance = SimpleNamespace(state_root=config.state_root, cache_dir=tmp_path / "model")
    with patch("mindie_knowledge.publishing.instance_for_config", return_value=instance), \
         patch("mindie_knowledge.github_transport.github_token", side_effect=AssertionError("no contribution auth")), \
         patch("mindie_knowledge.publishing.iter_pending", side_effect=AssertionError("no private history")), \
         patch("mindie_knowledge.distribution.release.source_from_location") as source, \
         patch("mindie_knowledge.publishing.check_and_sync", return_value=SyncResult(status="unchanged")):
        result = run_once(config, force=True)
    assert source.call_args.args[0] == "github://example/corpus"
    assert result["sync"]["status"] == "unchanged"
