"""Shared community settings: fail-closed gates and toggle preservation."""

import json

import pytest

from mindie_knowledge.loop import settings as settings_mod

from conftest import write_settings


def test_missing_or_malformed_settings_fail_closed(tmp_path):
    missing = settings_mod.load(tmp_path / "absent.json")
    assert not missing.allows_capture() and not missing.enabled
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert not settings_mod.load(bad).allows_capture()
    wrong_schema = tmp_path / "wrong.json"
    wrong_schema.write_text(json.dumps({"schema": "other/9", "enabled": True}))
    assert not settings_mod.load(wrong_schema).allows_capture()


def test_oversized_settings_are_corrupt_and_not_repaired(tmp_path):
    from mindie_knowledge import consent_store

    path = tmp_path / "oversized.json"
    raw = b'{"schema":"mindie-community-config/1","enabled":false}' + b" " * consent_store.MAX_BYTES
    path.write_bytes(raw)
    loaded = settings_mod.load(path)
    assert loaded.state == "corrupt"
    assert loaded.capture_block_kind() == "fault"
    with pytest.raises(ValueError, match="corrupt"):
        settings_mod.write(path, enabled=False, repository=None, project_roots=[])
    assert path.read_bytes() == raw


def test_missing_choice_reference_is_a_fault_without_rewriting_saved_choice(tmp_path):
    from mindie_knowledge import consent_store

    consent = tmp_path / "consent.json"
    consent_store.record_choice(consent, "contribute")
    saved = consent.read_bytes()
    path = tmp_path / "community.json"
    original = write_settings(path, roots=[tmp_path], consent_config=str(consent))
    raw = json.loads(path.read_text())
    del raw["consent_config"]
    path.write_text(json.dumps(raw))
    broken = path.read_bytes()
    for _ in range(2):
        loaded = settings_mod.load(path)
        assert loaded.capture_block_kind() == "fault"
        assert loaded.public_status()["consent"]["state"] == "invalid"
        assert "invalid" in loaded.contribution_block_reason()
        assert path.read_bytes() == broken
        assert consent.read_bytes() == saved
    # Wiring repair reuses the saved choice and authorization boundary.
    settings_mod.update_extensions(path, consent_config=str(consent))
    restored = settings_mod.load(path)
    assert restored.allows_capture()
    assert restored.generation == original.generation
    assert restored.enabled_at == original.enabled_at
    assert consent.read_bytes() == saved


def test_missing_enabled_field_is_not_an_explicit_optout(tmp_path):
    path = tmp_path / "community.json"
    original = write_settings(path, consent_choice="contribute", roots=[tmp_path])
    raw = dict(original.raw)
    del raw["enabled"]
    path.write_text(json.dumps(raw))
    loaded = settings_mod.load(path)
    assert loaded.state == "corrupt"
    assert loaded.capture_block_kind() == "fault"
    assert not loaded.allows_capture()


def test_states_distinguish_missing_corrupt_disabled_and_enabled(tmp_path):
    """A damaged file is never a never-configured install, and a saved
    explicit off is neither missing nor damaged (no fresh onboarding, no
    guessed consent)."""
    missing = settings_mod.load(tmp_path / "absent.json")
    assert missing.state == "missing" and not missing.configured
    unconfigured = settings_mod.load(None)
    assert unconfigured.state == "unconfigured"
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    loaded = settings_mod.load(corrupt)
    assert loaded.state == "corrupt" and not loaded.configured
    wrong_schema = tmp_path / "wrong.json"
    wrong_schema.write_text(json.dumps({"schema": "other/9", "enabled": True}))
    assert settings_mod.load(wrong_schema).state == "corrupt"
    off = write_settings(tmp_path / "off.json", consent_choice="contribute", enabled=False, roots=[tmp_path])
    assert off.state == "disabled" and off.configured and not off.enabled
    on = write_settings(tmp_path / "on.json", consent_choice="contribute", enabled=True, roots=[tmp_path])
    assert on.state == "enabled" and on.configured and on.allows_capture()
    assert settings_mod.load(tmp_path / "off.json").state == "disabled"
    assert "state" in on.public_status()


def test_enabled_requires_generation_boundary_and_roots(tmp_path):
    path = tmp_path / "community.json"
    settings = write_settings(path, consent_choice="contribute", enabled=True, roots=[tmp_path])
    assert settings.allows_capture()
    assert isinstance(settings.generation, str) and settings.generation
    raw = json.loads(path.read_text())
    for mutate in (
        lambda d: d.update(generation=""),
        lambda d: d.update(generation=7),
        lambda d: d.update(enabled_at=None),
        lambda d: d.update(enabled_at=-5),
        lambda d: d.update(project_roots=[]),
        lambda d: d.update(project_roots=["relative/path"]),
    ):
        data = dict(raw)
        mutate(data)
        variant = tmp_path / "variant.json"
        variant.write_text(json.dumps(data))
        assert not settings_mod.load(variant).allows_capture(), mutate


def test_scope_is_canonical_containment(tmp_path):
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    settings = write_settings(tmp_path / "c.json", consent_choice="contribute", enabled=True, roots=[root])
    assert settings.in_scope(root)
    assert settings.in_scope(root / "sub")
    assert settings.in_scope(str(root / "sub" / ".."))
    assert not settings.in_scope(tmp_path)
    assert not settings.in_scope(str(root) + "-sibling")
    assert not settings.in_scope(None)


def test_toggle_preserves_extension_keys_and_bumps_generation(tmp_path):
    path = tmp_path / "c.json"
    first = write_settings(path, consent_choice="contribute", enabled=True, roots=[tmp_path],
                           fork="me/knowledge-vllm-ascend", bot={"name": "grok"})
    second = write_settings(path, consent_choice="contribute", enabled=False, roots=[tmp_path])
    assert second.generation != first.generation
    assert second.raw["fork"] == "me/knowledge-vllm-ascend"
    assert second.raw["bot"] == {"name": "grok"}
    assert not second.allows_capture()
    third = write_settings(path, consent_choice="contribute", enabled=True, roots=[tmp_path])
    assert third.enabled_at is not None and third.raw["fork"] == "me/knowledge-vllm-ascend"


def test_as_dict_keeps_schema_extensions_and_config_path(tmp_path):
    settings = write_settings(tmp_path / "c.json", consent_choice="contribute", enabled=True, roots=[tmp_path],
                              transaction={"mode": "batch"})
    data = settings.as_dict()
    assert data["schema"] == "mindie-community-config/1"
    assert data["transaction"] == {"mode": "batch"}
    assert data["config_path"] == str(tmp_path / "c.json")


@pytest.mark.parametrize('fault', ['missing', 'corrupt', 'undetermined'])
def test_authority_fault_does_not_renew_existing_permission(tmp_path, fault):
    from mindie_knowledge import consent_store

    consent = tmp_path / 'consent.json'
    consent_store.record_choice(consent, 'contribute')
    path = tmp_path / 'community.json'
    original = write_settings(path, roots=[tmp_path], consent_config=str(consent))
    before = path.read_bytes()
    if fault == 'missing':
        consent.unlink()
    elif fault == 'corrupt':
        consent.write_text('{broken', encoding='utf-8')
    else:
        consent.write_text(json.dumps(dict(schema='mindie-consent/1')), encoding='utf-8')
    for _ in range(2):
        with settings_mod.CommunityWriteContext(path) as writer:
            writer.write(path, enabled=True, repository=original.repository,
                         project_roots=[tmp_path])
        assert path.read_bytes() == before
        assert settings_mod.load(path).capture_block_kind() == 'fault'
    # Restoring the same authority resumes the original boundary, rather
    # than making already accepted work stale after a temporary fault.
    consent.unlink(missing_ok=True)
    consent_store.record_choice(consent, 'contribute')
    restored = settings_mod.load(path)
    assert restored.allows_capture()
    assert restored.generation == original.generation
    assert restored.enabled_at == original.enabled_at


def test_removed_policy_extension_stays_removed_and_changes_boundary_once(tmp_path):
    path = tmp_path / 'community.json'
    original = write_settings(path, consent_choice="contribute", roots=[tmp_path], fork='owner/fork', bot=dict(name='bot'))
    with settings_mod.CommunityWriteContext(path) as writer:
        removed = writer.write(path, enabled=True, repository=original.repository,
                               project_roots=[tmp_path], fork=None)
    assert 'fork' not in removed.raw
    assert removed.raw['bot'] == original.raw['bot']
    assert removed.generation != original.generation
    with settings_mod.CommunityWriteContext(path) as writer:
        repeated = writer.write(path, enabled=True, repository=original.repository,
                                project_roots=[tmp_path], fork=None)
    assert repeated.generation == removed.generation


def test_scope_aliases_duplicates_and_order_do_not_rewrite_authority(tmp_path):
    path = tmp_path / 'community.json'
    left, right = tmp_path / 'left', tmp_path / 'right'
    left.mkdir()
    right.mkdir()
    original = write_settings(path, consent_choice="contribute", roots=[left, right])
    before = path.read_bytes()
    alias = str(left) + '/../left'
    assert settings_mod.normalized_roots([str(left), alias]) == [str(left.resolve())]
    with settings_mod.CommunityWriteContext(path) as writer:
        repeated = writer.write(path, enabled=True, repository=original.repository,
                                project_roots=[right, alias, left])
    assert repeated.generation == original.generation
    assert repeated.enabled_at == original.enabled_at
    assert path.read_bytes() == before


@pytest.mark.parametrize('choice', ['disabled', 'read-only', 'later'])
def test_interrupted_enable_reuses_one_boundary_for_the_same_optout(tmp_path, choice):
    from mindie_knowledge import consent_store
    consent = tmp_path / 'consent.json'
    consent_store.record_choice(consent, 'contribute')
    path = tmp_path / 'community.json'
    original = write_settings(path, roots=[tmp_path], consent_config=str(consent))
    consent_store.record_choice(consent, choice)

    def enable():
        with settings_mod.CommunityWriteContext(path) as writer:
            return writer.write(path, enabled=True, repository=original.repository,
                                project_roots=[tmp_path])

    first = enable()
    assert first.generation != original.generation
    assert first.enabled_at >= original.enabled_at
    saved = path.read_bytes()
    for _ in range(3):
        repeated = enable()
        assert not repeated.allows_capture()
        assert repeated.enabled_at == first.enabled_at
        assert path.read_bytes() == saved
    # Completion of the interrupted operation grants the one saved boundary.
    consent_store.record_choice(consent, 'contribute')
    completed = enable()
    assert completed.allows_capture()
    assert path.read_bytes() == saved
    # Another genuine opt-out and enable still establishes a new boundary.
    consent_store.record_choice(consent, choice)
    again = enable()
    assert again.generation != first.generation
    assert again.enabled_at >= first.enabled_at


def test_interrupted_first_enable_keeps_its_original_boundary(tmp_path):
    from mindie_knowledge import consent_store
    consent = tmp_path / 'consent.json'
    consent_store.record_choice(consent, 'later')
    path = tmp_path / 'community.json'
    requested = dict(enabled=True, repository='owner/knowledge',
                     project_roots=[str(tmp_path)], consent_config=str(consent))
    with settings_mod.CommunityWriteContext(path) as writer:
        first = writer.configure(path, requested)
    saved = path.read_bytes()
    for _ in range(3):
        with settings_mod.CommunityWriteContext(path) as writer:
            repeated = writer.configure(path, requested)
        assert repeated.enabled_at == first.enabled_at
        assert not repeated.allows_capture()
        assert path.read_bytes() == saved
    consent_store.record_choice(consent, 'contribute')
    assert settings_mod.load(path).allows_capture()


def test_malformed_consent_and_enabled_field_shapes_fail_closed_as_fault(tmp_path):
    from mindie_knowledge import consent_store

    consent = tmp_path / "consent.json"
    consent_store.record_choice(consent, "contribute")
    path = tmp_path / "community.json"
    write_settings(path, enabled=True, roots=[tmp_path], consent_config=str(consent))
    base_raw = json.loads(path.read_text())

    for field, bad_val in (
        ("consent_config", None),
        ("consent_config", ""),
        ("consent_config", 1),
        ("consent_config", True),
        ("consent_config", "mindie-consent.json"),
        ("enabled", "false"),
        ("enabled", "true"),
        ("enabled", 0),
    ):
        variant = tmp_path / "variant.json"
        data = dict(base_raw)
        data[field] = bad_val
        variant.write_text(json.dumps(data))
        loaded = settings_mod.load(variant)
        assert not loaded.allows_capture(), (field, bad_val)
        assert loaded.capture_block_kind() == "fault", (field, bad_val)

    consent.write_text(json.dumps({"schema": "mindie-consent/1", "reporting": "later"}) + "\n")
    loaded = settings_mod.load(path)
    assert not loaded.allows_capture()
    assert loaded.capture_block_kind() == "fault"

