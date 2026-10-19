"""Focused contract tests for the shared domain-to-engine-config selector."""
from __future__ import annotations

import json

import pytest

from mindie_knowledge.loop.domain_routes import select_engine_config

DEFAULT_DOMAIN = "operators"
SELECTED_DOMAIN = "vllm-ascend"


def config_file(root, name, domain):
    path = root / name
    path.write_text(json.dumps({"domain": domain, "root": str(root)}), encoding="utf-8")
    return path


def routes(tmp_path):
    default = config_file(tmp_path, "default.json", DEFAULT_DOMAIN)
    selected = config_file(tmp_path, "vllm-ascend.json", SELECTED_DOMAIN)
    return default, selected, {SELECTED_DOMAIN: str(selected)}


def test_default_call_returns_the_supplied_path_without_reading_configs(tmp_path):
    default = tmp_path / "absent-default.json"
    registry = {SELECTED_DOMAIN: str(tmp_path / "absent-selected.json")}
    assert select_engine_config(default, domain_configs=registry) == default.resolve()
    assert not default.exists()


def test_default_config_registers_its_own_domain(tmp_path):
    default = config_file(tmp_path, "default.json", DEFAULT_DOMAIN)
    assert select_engine_config(default, domain=DEFAULT_DOMAIN) == default.resolve()
    assert select_engine_config(
        default, ref=f"mindie://{DEFAULT_DOMAIN}/{'a' * 64}"
    ) == default.resolve()


def test_explicit_domain_selects_the_configured_engine(tmp_path):
    default, selected, registry = routes(tmp_path)
    assert select_engine_config(
        default, domain=SELECTED_DOMAIN, domain_configs=registry
    ) == selected.resolve()


@pytest.mark.parametrize(
    "ref",
    [
        f"mindie://{SELECTED_DOMAIN}/{'a' * 64}",
        f"mindie://{SELECTED_DOMAIN}/{'a' * 64}/blocks/{'b' * 64}@{'c' * 64}",
        f"mindie://{SELECTED_DOMAIN}/{'a' * 64}@{'b' * 64}",
        f"mindie://{SELECTED_DOMAIN}/units/unit-graph-fix@{'c' * 64}",
    ],
    ids=["task", "block", "feedback", "graded"],
)
def test_reference_authority_selects_all_typed_forms(tmp_path, ref):
    default, selected, registry = routes(tmp_path)
    assert select_engine_config(
        default, ref=ref, domain_configs=registry
    ) == selected.resolve()


def test_unknown_domain_never_falls_back_to_default(tmp_path):
    default, selected, registry = routes(tmp_path)
    with pytest.raises(ValueError, match="unknown domain"):
        select_engine_config(default, domain="older-operators", domain_configs=registry)
    with pytest.raises(ValueError, match="unknown domain"):
        select_engine_config(
            default,
            ref=f"mindie://older-operators/{'a' * 64}",
            domain_configs=registry,
        )


def test_conflicting_domain_and_reference_fail(tmp_path):
    default, selected, registry = routes(tmp_path)
    with pytest.raises(ValueError, match="conflicts"):
        select_engine_config(
            default,
            domain=DEFAULT_DOMAIN,
            ref=f"mindie://{SELECTED_DOMAIN}/{'a' * 64}",
            domain_configs=registry,
        )


def test_relative_configured_path_is_rejected(tmp_path):
    default, selected, registry = routes(tmp_path)
    with pytest.raises(ValueError, match="absolute"):
        select_engine_config(
            default,
            domain=SELECTED_DOMAIN,
            domain_configs={SELECTED_DOMAIN: "vllm-ascend.json"},
        )


def test_selected_config_declared_domain_mismatch_is_rejected(tmp_path):
    default, selected, registry = routes(tmp_path)
    wrong = config_file(tmp_path, "wrong.json", DEFAULT_DOMAIN)
    with pytest.raises(ValueError, match="declares"):
        select_engine_config(
            default,
            domain=SELECTED_DOMAIN,
            domain_configs={SELECTED_DOMAIN: str(wrong)},
        )


def test_unavailable_unrelated_configs_do_not_block_selection(tmp_path):
    selected = config_file(tmp_path, "vllm-ascend.json", SELECTED_DOMAIN)
    default = tmp_path / "absent-default.json"
    registry = {
        SELECTED_DOMAIN: str(selected),
        "other-domain": str(tmp_path / "absent-other.json"),
    }
    assert select_engine_config(
        default, domain=SELECTED_DOMAIN, domain_configs=registry
    ) == selected.resolve()
