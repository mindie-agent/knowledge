"""Old clients retain a compatible service without selecting another release."""
import copy
import json
from pathlib import Path
import sys

import pytest
import transcript_double

from mindie_knowledge.loop import cli, client_contract as contract, lifecycle, transport
from mindie_knowledge.loop.locks import StartLock


def configurations(tmp_path):
    profile = str(tmp_path / "profile.json")
    value = dict(
        root=str(tmp_path / "data"), domain="test",
        admission_path=str(tmp_path / "admission.sqlite3"),
        transcript_adapter=transcript_double.__file__,
        redactor_executable=str(tmp_path / "old/gitleaks"),
        summary_command=[sys.executable, str(tmp_path / "old/summary.py")],
        service_launch_context={
            "paths": {"MINDIE_AGENT_CONFIG": str(tmp_path / "old/adapter.json"),
                      "CODEX_HOME": str(tmp_path / "codex-home")},
            "environment": ["SELECTED_PROVIDER_KEY"],
        },
        client_contract={
            "schema": contract.SCHEMA,
            "transcript_protocol": "mindie-codex-transcript/1",
            "summary_protocol": "mindie-codex-summary/1",
            "adapter_profile": profile,
            "adapter_config_env": "MINDIE_AGENT_CONFIG",
        },
    )
    old = tmp_path / "old.json"
    old.write_text(json.dumps(value))
    successor = copy.deepcopy(value)
    successor["redactor_executable"] = str(tmp_path / "new/gitleaks")
    successor["summary_command"] = [sys.executable, "-m", "installed.summary"]
    successor["service_launch_context"]["paths"]["MINDIE_AGENT_CONFIG"] = str(tmp_path / "new/adapter.json")
    new = tmp_path / "new.json"
    new.write_text(json.dumps(successor))
    return old, value, new, successor


def test_explicit_protocol_allows_runtime_locations_not_business_changes(tmp_path):
    _, old, _, new = configurations(tmp_path)
    assert contract.fingerprint(old) == contract.fingerprint(new)
    cases = [
        ("root", str(tmp_path / "another-root")),
        ("domain", "another"),
        ("admission_path", str(tmp_path / "other-admission")),
        ("community_config", str(tmp_path / "other-consent")),
        ("feeds", [{"repository": "someone/else"}]),
        ("summary_command", [sys.executable, "-m", "installed.summary", "--other-policy"]),
        ("client_contract", dict(new["client_contract"], transcript_protocol="mindie-cc-transcript/1")),
        ("client_contract", dict(new["client_contract"], summary_protocol="mindie-codex-summary/2")),
        ("client_contract", dict(new["client_contract"], adapter_profile=str(tmp_path / "other-profile"))),
        ("service_launch_context", dict(new["service_launch_context"], environment=["OTHER_PROVIDER_KEY"])),
        ("service_launch_context", dict(new["service_launch_context"], paths=dict(
            new["service_launch_context"]["paths"], CODEX_HOME=str(tmp_path / "other-provider")))),
        ("future_business_option", True),
    ]
    for key, value in cases:
        changed = dict(new, **{key: value})
        with pytest.raises(contract.ClientCompatibilityError, match="incompatible"):
            contract.require_compatible(old, changed)
    undeclared = {key: val for key, val in old.items() if key != "client_contract"}
    assert contract.fingerprint(undeclared) is None
    with pytest.raises(contract.ClientCompatibilityError):
        contract.require_compatible(undeclared, undeclared)


def test_build_receipt_changes_preserve_publication_policy_identity(tmp_path):
    _, old, _, new = configurations(tmp_path)
    publication = dict(repository="mindie-agent/knowledge-test", domain="test",
                       ref="main", contract_sha256="a" * 64, verified_commit="b" * 40)
    old["product_validation"] = dict(schema="mindie-candidate-check/1",
                                     source_sha256="a" * 64, publication=publication)
    new["product_validation"] = dict(schema="mindie-candidate-check/1",
                                     source_sha256="b" * 64, publication=publication)
    assert contract.require_compatible(old, new)
    new["product_validation"]["publication"] = dict(publication, repository="another/repository")
    with pytest.raises(contract.ClientCompatibilityError):
        contract.require_compatible(old, new)


def test_invalid_contract_cannot_turn_missing_configuration_into_compatibility(tmp_path):
    _, value, _, _ = configurations(tmp_path)
    for field in ("adapter_profile", "transcript_protocol", "summary_protocol"):
        broken = copy.deepcopy(value)
        broken["client_contract"][field] = None
        with pytest.raises(ValueError):
            cli.validate_config(broken)
    broken = copy.deepcopy(value)
    broken["service_launch_context"]["paths"].pop("MINDIE_AGENT_CONFIG")
    with pytest.raises(ValueError, match="binding"):
        contract.fingerprint(broken)


def test_real_old_client_uses_successor_and_can_restart_it_without_old_owner(tmp_path):
    old, config, new, next_config = configurations(tmp_path)
    original = cli.ensure_service(old)
    result = lifecycle.retire_service(old, successor_config_path=new)
    assert result["retirement"]["successor"] == {
        "config_path": str(new),
        "config_fingerprint": lifecycle.config_fingerprint(new, next_config),
    }
    current = None
    try:
        current = cli.ensure_client_service(old)
        assert current["config_fingerprint"] == lifecycle.config_fingerprint(new, next_config)
        assert current["token"] != original["token"]
        assert cli.ensure_client_service(new) == current
        assert cli.prepare_client_service(old) == "ready"
        status = transport.rpc(current, "status")
        assert status["service_identity"]["config_path"] == str(new)
        assert status["worker_alive"]
        with pytest.raises(lifecycle.ServiceRetired):
            cli.ensure_service(old)
        transport.rpc(current, "stop")
        from mindie_knowledge.loop.locks import lock_held
        import time
        deadline = time.monotonic() + 5
        while lock_held(cli.connection_path(config).with_name("consumer.lock")) is not False:
            assert time.monotonic() < deadline
            time.sleep(.01)
        restored = cli.ensure_client_service(old)
        assert restored["config_fingerprint"] == current["config_fingerprint"]
        assert restored["token"] != current["token"]
    finally:
        lifecycle.retire_service(new)


def test_successor_drift_and_uncertain_handoff_never_start_any_owner(tmp_path, monkeypatch):
    old, _, new, next_config = configurations(tmp_path)
    lifecycle.retire_service(old, successor_config_path=new)
    calls = []
    monkeypatch.setattr(cli, "ensure_service", lambda *a, **k: calls.append(a))
    new.write_text(json.dumps(dict(next_config, future_business_option=True)))
    with pytest.raises(contract.ClientCompatibilityError, match="changed"):
        cli.ensure_client_service(old)
    new.write_text(json.dumps(next_config))
    marker = lifecycle._marker(old)
    receipt = json.loads(marker.read_text())
    receipt["status"] = "retiring"
    marker.write_text(json.dumps(receipt))
    with pytest.raises(contract.ClientCompatibilityError, match="uncertain"):
        cli.ensure_client_service(old)
    assert calls == []


def test_completed_handoff_cannot_be_overwritten_or_target_a_retired_owner(tmp_path):
    old, _, new, value = configurations(tmp_path)
    lifecycle.retire_service(new)
    with pytest.raises(lifecycle.ServiceRetired):
        lifecycle.retire_service(old, successor_config_path=new)
    assert lifecycle.inspect_retirement(old) is None
    lifecycle.restore_service(new)
    result = lifecycle.retire_service(old, successor_config_path=new)
    alternate = tmp_path / "alternate.json"
    alternate.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="cannot be replaced"):
        lifecycle.retire_service(old, successor_config_path=alternate)
    assert lifecycle.inspect_retirement(old) == result["retirement"]


def test_cycle_is_reported_without_spawning_or_latest_selection(tmp_path):
    old, old_value, new, _ = configurations(tmp_path)
    lifecycle.retire_service(old, successor_config_path=new)
    receipt = dict(schema=lifecycle.SCHEMA, status="retired", operation_id="1" * 32,
                   config_fingerprint=lifecycle.config_fingerprint(new),
                   successor=dict(config_path=str(old),
                       config_fingerprint=lifecycle.config_fingerprint(old, old_value)))
    lifecycle._marker(new).write_text(json.dumps(receipt))
    with pytest.raises(contract.ClientCompatibilityError, match="cycle"):
        contract.resolve_config(old)


def test_forged_discovery_contract_is_not_authenticated_service_identity(tmp_path, monkeypatch):
    old, value, _, _ = configurations(tmp_path)
    connection = cli.connection_path(value)
    connection.parent.mkdir(parents=True)
    connection.write_text(json.dumps(dict(
        domain="test", config_path=str(old), token="fixture", url="http://127.0.0.1:1",
        config_fingerprint=lifecycle.config_fingerprint(old),
        client_fingerprint=contract.fingerprint(value))))
    monkeypatch.setattr(transport, "rpc", lambda *a, **k: dict(worker_alive=True))
    with StartLock(connection.with_name("consumer.lock")):
        with pytest.raises(contract.ClientCompatibilityError, match="unverified"):
            contract.connect_ready(old, value)


def test_retirement_without_handoff_cannot_attach_to_another_compatible_owner(tmp_path):
    old, value, new, _ = configurations(tmp_path)
    lifecycle.retire_service(old)
    cli.ensure_service(new)
    try:
        with pytest.raises(lifecycle.ServiceRetired, match="without a successor"):
            cli.ensure_client_service(old)
    finally:
        lifecycle.retire_service(new)


def test_late_stop_wake_resolves_exact_successor_without_reactivation(tmp_path, monkeypatch):
    old, value, new, successor = configurations(tmp_path)
    lifecycle.retire_service(old, successor_config_path=new)
    from mindie_knowledge.loop import handoff, settings
    from mindie_knowledge.loop.activation import Admission
    monkeypatch.setattr(settings, "from_engine_config", lambda config: type(
        "Settings", (), {"allows_capture": lambda self: True})())
    monkeypatch.setattr(Admission, "active_lease", lambda *a: {"fixture": True})
    seen = []
    def probe(config, timeout, *, config_path, compatible):
        assert compatible is True
        seen.append((config_path, config))
        return "ready"
    monkeypatch.setattr(handoff, "_probe", probe)
    result = handoff.request_wake(old, session_id="already-authorized")
    assert result["runtime"] == "ready" and result["wake"] == "running"
    assert seen == [(new, successor)]
    assert lifecycle.inspect_retirement(old)["status"] == "retired"


def test_client_probe_uses_compatible_live_owner_but_owner_probe_remains_exact(tmp_path):
    from mindie_knowledge.loop import handoff
    old, _, new, successor = configurations(tmp_path)
    cli.ensure_service(old)
    try:
        assert handoff._probe(successor, .25, config_path=new) == "unknown"
        assert handoff._probe(successor, .25, config_path=new, compatible=True) == "ready"
        changed = dict(successor, future_business_option=True)
        assert handoff._probe(changed, .25, config_path=new, compatible=True) == "unknown"
        assert lifecycle.inspect_retirement(old) is None
    finally:
        lifecycle.retire_service(old)


def test_client_identity_accepts_declared_adapter_path_without_harness_registry(tmp_path):
    _, old, _, new = configurations(tmp_path)
    for value in (old, new):
        paths = value["service_launch_context"]["paths"]
        paths["OTHER_ADAPTER_CONFIG"] = paths.pop("MINDIE_AGENT_CONFIG")
        value["client_contract"]["adapter_config_env"] = "OTHER_ADAPTER_CONFIG"
    assert contract.require_compatible(old, new)
    old["client_contract"]["adapter_config_env"] = "PATH"
    old["service_launch_context"]["paths"]["PATH"] = str(tmp_path)
    with pytest.raises(ValueError):
        contract.fingerprint(old)
