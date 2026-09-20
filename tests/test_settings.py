"""Shared community settings: fail-closed gates and toggle preservation."""

import json

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


def test_enabled_requires_generation_boundary_and_roots(tmp_path):
    path = tmp_path / "community.json"
    settings = write_settings(path, enabled=True, roots=[tmp_path])
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
    settings = write_settings(tmp_path / "c.json", enabled=True, roots=[root])
    assert settings.in_scope(root)
    assert settings.in_scope(root / "sub")
    assert settings.in_scope(str(root / "sub" / ".."))
    assert not settings.in_scope(tmp_path)
    assert not settings.in_scope(str(root) + "-sibling")
    assert not settings.in_scope(None)


def test_toggle_preserves_extension_keys_and_bumps_generation(tmp_path):
    path = tmp_path / "c.json"
    first = write_settings(path, enabled=True, roots=[tmp_path],
                           fork="me/knowledge-vllm-ascend", bot={"name": "grok"})
    second = write_settings(path, enabled=False, roots=[tmp_path])
    assert second.generation != first.generation
    assert second.raw["fork"] == "me/knowledge-vllm-ascend"
    assert second.raw["bot"] == {"name": "grok"}
    assert not second.allows_capture()
    third = write_settings(path, enabled=True, roots=[tmp_path])
    assert third.enabled_at is not None and third.raw["fork"] == "me/knowledge-vllm-ascend"


def test_as_dict_keeps_schema_extensions_and_config_path(tmp_path):
    settings = write_settings(tmp_path / "c.json", enabled=True, roots=[tmp_path],
                              transaction={"mode": "batch"})
    data = settings.as_dict()
    assert data["schema"] == "mindie-community-config/1"
    assert data["transaction"] == {"mode": "batch"}
    assert data["config_path"] == str(tmp_path / "c.json")
