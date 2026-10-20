"""Tier 0 tests for deterministic CANNBot OKF and transcript distillation ingestion."""
from textwrap import dedent

import pytest

from mindie_knowledge.graded import units as graded_units
from mindie_knowledge.graded.cannbot_ingest import (
    EVIDENCE_STRENGTHS,
    extract_ascend_error_signatures,
    parse_cannbot_okf_blocks,
    redact_private_identifiers,
    synthesize_graded_unit_from_distillation,
)


def _parse_unit(text):
    parser = getattr(graded_units, "parse_graded_unit_markdown", None)
    if parser is None:
        return graded_units.GradedUnit.from_text(text)
    parsed = parser(text)
    return parsed if hasattr(parsed, "metadata") else graded_units.GradedUnit.from_text(text)


def test_extract_real_cann_rts_hccl_and_aicore_signatures():
    log = dedent(
        """
        [ERROR] HCCL_E_WAIT_TIMEOUT: notify wait timeout while waiting for rank 3
        [ERROR] CANN EZ9999: internal error; EI0006: parameter check failed
        [ERROR] RTS exception 507015 on device_id=3 stream_id=12 task_id=99
        [ERROR] aic_error=0x20 AICORE exception; vec_err_info; mte_err_info; p2p timeout
        repeated EZ9999 and HCCL_E_WAIT_TIMEOUT
        """
    )
    assert extract_ascend_error_signatures(log) == {
        "error_codes": ["507015", "EI0006", "EZ9999", "HCCL_E_WAIT_TIMEOUT"],
        "error_signatures": ["AICORE exception", "Notify wait timeout", "aic_error",
                             "mte_err_info", "p2p timeout", "vec_err_info"],
        "keywords": ["device_id", "stream_id", "task_id"],
    }


def test_extraction_is_sorted_deduplicated_and_ignores_version_tokens():
    result = extract_ascend_error_signatures(
        "torch_npu 2.5.1 CANN 8.1.0 EX9999 EA0001 EX9999 device_id=2 device_id=2 107002"
    )
    assert result["error_codes"] == ["107002", "EA0001", "EX9999"]
    assert result["error_signatures"] == []
    assert result["keywords"] == ["device_id"]


CANNBOT_RUNBOOK = dedent(
    """
    # CANNBot domain runbooks

    <!-- OKF_META
    okf_id: vllm-ascend/hccl-wait-timeout
    title: HCCL wait timeout recovery
    category: sop
    confidence: 0.93
    tags: [vllm-ascend, hccl, timeout]
    source: cannbot-knowledge/domains/vllm-ascend/runbook.md
    applicable_versions: ["CANN 8.0", "CANN 8.1"]
    -->
    ## [vllm-ascend/hccl-wait-timeout] HCCL wait timeout recovery

    ### Symptoms
    HCCL_E_WAIT_TIMEOUT with notify wait timeout and device_id=3.

    ### Procedure
    1. Inspect stream_id and task_id.
    2. Re-run the collective.

    <!-- OKF_META
    {"okf_id": "ascendc/aicore-vec-err", "title": "AICore vec error", "category": "sop",
     "confidence": 88, "tags": ["ascendc", "aicore"], "source": "cannbot-knowledge/domains/ascendc/runbook.md",
     "applicable_versions": ["CANN 8.2"]}
    -->
    ## [ascendc/aicore-vec-err] AICore vec error

    aic_error and vec_err_info were observed; AICORE exception follows.

    <!-- OKF_META
    okf_id = triton/recompile-timeout
    title = Triton recompile timeout
    category = workflow
    confidence = high
    tags = triton, compile, timeout
    source = cannbot-knowledge/domains/triton/runbook.md
    applicable_versions = CANN 8.1
    -->
    ## [triton/recompile-timeout] Triton recompile timeout

    p2p timeout after triton recompilation.
    """
)


def test_parse_okf_cards_from_vllm_ascend_ascendc_and_triton():
    cards = parse_cannbot_okf_blocks(CANNBOT_RUNBOOK)
    assert [card["okf_id"] for card in cards] == [
        "vllm-ascend/hccl-wait-timeout",
        "ascendc/aicore-vec-err",
        "triton/recompile-timeout",
    ]
    first = cards[0]
    assert first["title"] == "HCCL wait timeout recovery"
    assert first["category"] == "sop"
    assert first["confidence"] == pytest.approx(0.93)
    assert first["tags"] == ["vllm-ascend", "hccl", "timeout"]
    assert first["source"] == "cannbot-knowledge/domains/vllm-ascend/runbook.md"
    assert first["applicable_versions"] == ["CANN 8.0", "CANN 8.1"]
    assert first["error_codes"] == ["HCCL_E_WAIT_TIMEOUT"]
    assert "Notify wait timeout" in first["error_signatures"]
    assert first["keywords"] == ["device_id", "stream_id", "task_id"]
    assert first["body"].startswith("### Symptoms")
    assert "ascendc/aicore-vec-err" not in first["body"]

    second = cards[1]
    assert second["confidence"] == pytest.approx(0.88)
    assert second["tags"] == ["ascendc", "aicore"]
    assert second["applicable_versions"] == ["CANN 8.2"]
    assert second["error_signatures"] == ["AICORE exception", "aic_error", "vec_err_info"]
    assert "triton/recompile-timeout" not in second["body"]

    third = cards[2]
    assert third["category"] == "workflow"
    assert third["confidence"] == pytest.approx(0.85)
    assert third["tags"] == ["triton", "compile", "timeout"]
    assert third["applicable_versions"] == ["CANN 8.1"]
    assert third["error_signatures"] == ["p2p timeout"]
    assert third["body"] == "p2p timeout after triton recompilation."


def test_parser_requires_okf_metadata_and_ignores_plain_markdown():
    assert parse_cannbot_okf_blocks("# Plain runbook\n\nNo cards.") == []
    with pytest.raises(ValueError, match="OKF_META"):
        parse_cannbot_okf_blocks("<!-- OKF_META not a mapping -->\n## [x/y] Bad card")


def _distillation_spec(tier, category, strength, **updates):
    spec = {
        "id": "unit-cannbot-hccl-timeout",
        "tier": tier,
        "category": category,
        "domain_path": "serving/vllm-ascend",
        "title": "HCCL wait timeout triage",
        "conditions": {"soc": ["Ascend910B"], "cann": ">=8.0,<9"},
        "inherits_from": [],
        "evidence_strength": strength,
        "provenance": {
            "source_type": "cannbot_okf",
            "source_locator": "cannbot-knowledge@be5882e:domains/vllm-ascend/runbook.md",
            "source_digest": "a" * 64,
            "evidence_refs": ["okf:vllm-ascend/hccl-wait-timeout"],
        },
        "triggers": {
            "error_codes": ["EZ9999", "507015"],
            "error_signatures": ["HCCL_E_WAIT_TIMEOUT"],
            "keywords": ["timeout", "hccl"],
        },
        "body_sections": {
            "Symptoms": "HCCL_E_WAIT_TIMEOUT with notify wait timeout.",
            "Procedure": ["Inspect device_id and stream_id.", "Re-run the collective."],
        },
    }
    spec.update(updates)
    return spec


@pytest.mark.parametrize(("tier", "category", "strength"), [
    ("L1", "axiom", "hardware_spec"),
    ("L1", "measurement", "software_contract"),
    ("L2", "sop", "single_observation"),
    ("L3", "workflow", "reproduced_empirical"),
    ("L4", "mutator", "reproduced_empirical"),
])
def test_synthesize_valid_units_for_every_tier(tier, category, strength):
    text = synthesize_graded_unit_from_distillation(_distillation_spec(tier, category, strength))
    unit = _parse_unit(text)
    assert unit.metadata["schema"] == "mindie-graded-unit/1"
    assert unit.metadata["tier"] == tier
    assert unit.metadata["category"] == category
    assert unit.metadata["evidence_strength"] == strength
    assert unit.metadata["revision"] == 1
    assert unit.metadata["triggers"] == {
        "error_signatures": ["507015", "EZ9999", "HCCL_E_WAIT_TIMEOUT"],
        "keywords": ["hccl", "timeout"],
    }
    assert "## Symptoms" in unit.body
    assert "## Procedure" in unit.body
    assert "1. Inspect device_id and stream_id." in unit.body
    assert text == synthesize_graded_unit_from_distillation(_distillation_spec(tier, category, strength))


def test_synthesis_redacts_private_ips_and_user_paths():
    spec = _distillation_spec(
        "L2",
        "sop",
        "single_observation",
        provenance={
            "source_type": "cannbot_okf",
            "source_locator": "ssh:///Users/alice/runs/runbook.md",
            "evidence_refs": ["okf:run"],
        },
        body_sections={
            "Symptoms": "Observed 192.168.13.165 and 10.24.0.7; control 8.8.8.8.",
            "Procedure": [r"Copy /Users/alice/logs and C:\Users\bob\trace."],
        },
    )
    text = synthesize_graded_unit_from_distillation(spec)
    unit = _parse_unit(text)
    for secret in ("alice", "bob", "192.168.13.165", "10.24.0.7"):
        assert secret not in text
    assert "8.8.8.8" in text
    assert "<redacted:private-ipv4:" in text
    assert "/Users/<redacted:local-path:" in unit.metadata["provenance"]["source_locator"]


def test_redaction_is_idempotent_and_keeps_public_addresses():
    raw = (r"hosts 192.168.0.10 10.1.2.3 172.20.4.5 8.8.8.8; "
           r"/Users/alice/x /data/home/bob/y C:\Users\carol\z ~dave/q")
    once = redact_private_identifiers(raw)
    assert "8.8.8.8" in once
    for secret in ("192.168.0.10", "10.1.2.3", "172.20.4.5", "alice", "bob", "carol", "dave"):
        assert secret not in once
    assert redact_private_identifiers(once) == once


def test_synthesis_rejects_tier_evidence_and_missing_body_claims():
    with pytest.raises(ValueError, match="L1 requires"):
        synthesize_graded_unit_from_distillation(_distillation_spec("L1", "axiom", "single_observation"))
    with pytest.raises(ValueError, match="tier/category"):
        synthesize_graded_unit_from_distillation(_distillation_spec("L2", "workflow", "single_observation"))
    without_body = _distillation_spec("L2", "sop", "single_observation")
    without_body.pop("body_sections")
    with pytest.raises(ValueError, match="body"):
        synthesize_graded_unit_from_distillation(without_body)


def test_synthesis_rejects_l4_without_evidence_refs_or_source_locator():
    missing_refs = _distillation_spec("L4", "mutator", "reproduced_empirical")
    missing_refs["provenance"].pop("evidence_refs")
    with pytest.raises(ValueError, match="evidence_refs"):
        synthesize_graded_unit_from_distillation(missing_refs)
    missing_locator = _distillation_spec("L2", "sop", "single_observation")
    missing_locator["provenance"].pop("source_locator")
    with pytest.raises(ValueError, match="source_locator"):
        synthesize_graded_unit_from_distillation(missing_locator)


def test_evidence_strength_aliases_and_body_section_lists_are_normalized():
    spec = _distillation_spec("L3", "workflow", "reproduced", body_sections=[
        {"heading": "Symptoms", "body": "aic_error occurred."},
        {"title": "Procedure", "body": ["Check device_id.", "Retry."]},
    ])
    unit = _parse_unit(synthesize_graded_unit_from_distillation(spec))
    assert unit.metadata["evidence_strength"] == "reproduced_empirical"
    assert "aic_error occurred." in unit.body
    assert "1. Check device_id." in unit.body
    assert {"hardware_spec", "software_contract", "reproduced_empirical",
            "single_observation", "hypothesis"} <= set(EVIDENCE_STRENGTHS)
