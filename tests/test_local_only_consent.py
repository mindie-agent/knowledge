"""First-class ``local-only`` consent tier (`mindie-consent/1`)."""

from __future__ import annotations

import json

import pytest

from conftest import write_settings
from mindie_knowledge import consent_store


def test_choices_and_validation_message_include_local_only(tmp_path):
    assert "local-only" in consent_store.CHOICES
    assert consent_store.CHOICES.index("local-only") == 1
    with pytest.raises(ValueError, match="local-only"):
        consent_store.record_choice(tmp_path / "consent.json", "public")
    assert not (tmp_path / "consent.json").exists()


def test_local_only_recording_and_all_tier_transitions(tmp_path):
    path = tmp_path / "consent.json"
    assert consent_store.read(path)["state"] == "missing"
    saved = consent_store.record_choice(path, "local-only")
    assert saved["choice"] == "local-only"
    assert saved["choice_revision"]
    revision = saved["choice_revision"]
    repeated = consent_store.record_choice(path, "local-only")
    assert repeated["choice_revision"] == revision

    for choice in ("contribute", "read-only", "local-only", "disabled", "later", "local-only"):
        stored = consent_store.record_choice(path, choice)
        assert stored["choice"] == choice
        assert stored["choice_revision"] and stored["choice_revision"] != revision
        revision = stored["choice_revision"]
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["schema"] == "mindie-consent/1"
        assert document["choice"] == choice
        assert document["choice_revision"] == revision


def test_activate_reporting_stays_disabled_for_local_only(tmp_path, monkeypatch):
    from mindie_diagnostics import reporting_commands
    from mindie_diagnostics.reporting import current_consent

    consent = tmp_path / "consent.json"
    config = tmp_path / "diagnostics.json"
    started = []
    monkeypatch.setattr(
        reporting_commands, "ensure",
        lambda *args, **kwargs: started.append(args) or {"status": "running"},
    )

    consent_store.record_choice(consent, "contribute")
    enabled = consent_store.activate_reporting(
        consent, config=config, roots=[str(tmp_path)], ensure=False
    )
    assert enabled["enabled"] is True
    assert enabled["status"] == "configured"
    assert current_consent(config) is not None

    consent_store.record_choice(consent, "local-only")
    result = consent_store.activate_reporting(
        consent, config=config, roots=[str(tmp_path)]
    )
    assert result["status"] == "disabled"
    assert result["enabled"] is False
    assert result["worker"] == {"status": "not_started"}
    assert current_consent(config) is None
    assert started == []


def test_fresh_local_only_activation_writes_a_disabled_policy(tmp_path):
    from mindie_diagnostics.reporting import current_consent

    consent = tmp_path / "consent.json"
    config = tmp_path / "diagnostics.json"
    consent_store.record_choice(consent, "local-only")
    result = consent_store.activate_reporting(
        consent, config=config, roots=[str(tmp_path)], ensure=False
    )
    assert result["status"] == "disabled"
    assert result["enabled"] is False
    assert result.get("configuration", {}).get("status") == "configured"
    assert result["configuration"]["enabled"] is False
    assert result["worker"] == {"status": "not_started"}
    assert current_consent(config) is None


def test_activate_reporting_missing_local_only_is_not_onboarding(tmp_path):
    result = consent_store.activate_reporting(
        tmp_path / "absent.json", config=tmp_path / "diagnostics.json",
        roots=[str(tmp_path)], ensure=False,
    )
    assert result["status"] == "needs_choice"
    assert result["enabled"] is False
    assert not (tmp_path / "absent.json").exists()


def test_legacy_revisionless_contribute_cannot_authorize_publication(tmp_path):
    legacy = tmp_path / "legacy-consent.json"
    legacy.write_text(
        json.dumps({"schema": "mindie-consent/1", "choice": "contribute"}) + "\n",
        encoding="utf-8", newline="\n",
    )
    path = tmp_path / "community.json"
    settings = write_settings(
        path, roots=[tmp_path], consent_config=str(legacy.resolve()),
    )
    assert settings.consent["state"] == "ok"
    assert settings.consent["choice"] == "contribute"
    assert settings.consent["choice_revision"] is None
    assert settings.consent_allows_capture() is False
    assert settings.consent_allows_contribution() is False
    assert settings.allows_publication() is False
    assert settings.effective_tier() == "read-only"


def test_failed_local_only_policy_write_is_configuration_unavailable(tmp_path, monkeypatch):
    from mindie_diagnostics import integration

    consent = tmp_path / "consent.json"
    consent_store.record_choice(consent, "local-only")
    failed = {"status": "write_failed", "enabled": False}
    monkeypatch.setattr(
        integration, "configure_reporting", lambda *args, **kwargs: dict(failed),
    )
    result = consent_store.activate_reporting(
        consent, config=tmp_path / "diagnostics.json",
        roots=[str(tmp_path)], ensure=False,
    )
    assert result["status"] == "configuration_unavailable", (
        "a failed policy write must never masquerade as a confirmed disabled reporter"
    )
    assert result["enabled"] is False
    assert result["configuration"] == failed
    assert result["worker"] == {"status": "not_started"}
