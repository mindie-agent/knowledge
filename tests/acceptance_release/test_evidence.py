from __future__ import annotations

from tests.acceptance_release.evidence import audit
from tests.acceptance_release.cases import REPO_ROOT


def test_helpful_is_not_effect_success_without_artifacts():
    result = audit(
        "abc-effect",
        artifacts={},
        product_judge={"verdict": "helpful", "reason": "sounds useful"},
        record={"product_judge": {"verdict": "helpful"}},
    )
    assert result["effect_success"] is False
    assert result["evidence_verdict"] == "unknown"
    assert "product_judge.helpful" in result["forbidden_success_signals"]
    assert "store.weight_increase" in result["forbidden_success_signals"]


def test_success_no_contribution_and_failure_excluded():
    ok = audit(
        "success-no-contribution",
        artifacts={"task_succeeded": True, "experience_contributed": False},
        product_judge={"verdict": "helpful"},
    )
    assert ok["evidence_verdict"] == "supports"
    assert "success_no_contribution" in ok["labels"]
    failed = audit(
        "failure-hypothesis-excluded",
        artifacts={"task_failed": True, "hypothesis_excluded": True},
        product_judge={"verdict": "unhelpful"},
    )
    assert failed["evidence_verdict"] == "supports"
    assert failed["agreement"] is False  # judge unhelpful vs evidence supports


def test_no_hit_hit_unused_echo_and_conflicts():
    assert audit("no-hit", artifacts={"hits": []})["evidence_verdict"] == "supports"
    assert audit("no-hit", artifacts={"hits": [{"ref": "x"}]})["evidence_verdict"] == "contradicts"
    unused = audit("hit-unused", artifacts={"hit": True, "used": False})
    assert unused["labels"] == ["hit_unused"]
    echo = audit("consumption-echo", artifacts={"echo_checked": True, "consumer_became_producer": False, "extra_vote": False})
    assert echo["evidence_verdict"] == "supports"
    conflict = audit("migration-conflict-versions", artifacts={"kept_ids": ["a", "b"]})
    assert conflict["evidence_verdict"] == "supports"


def test_mcp_stdio_uses_public_source_file():
    path = REPO_ROOT / "corpus/references/mcp-stdio-newline-delimited-jsonrpc.md"
    result = audit(
        "migration-mcp-stdio",
        artifacts={
            "source_path": str(path),
            "mapped": {
                "kind": "knowledge",
                "source": {"url": "https://modelcontextprotocol.io/specification/2025-11-25/basic/transports"},
            },
        },
    )
    assert result["evidence_verdict"] == "supports"
    assert result["original_artifact_ok"] is True


def test_abc_protocol_cannot_declare_effect_from_score():
    result = audit(
        "abc-protocol",
        artifacts={"protocol_recorded": True, "declared_effect_success": True, "store_score_increased": True},
    )
    assert result["evidence_verdict"] == "contradicts"
