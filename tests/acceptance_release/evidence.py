"""Original-artifact audit, independent of the product judge.

A product ``helpful`` verdict or a Store weight/score increase is never
sufficient for effect success. Missing artifacts yield unknown.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .cases import case_by_id
from .metrics import UNKNOWN, metrics_from_record

FORBIDDEN_SUCCESS_SIGNALS = ("product_judge.helpful", "store.weight_increase", "store.score_increase")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _has(path: Path | None, snippet: str) -> bool:
    if path is None or not path.is_file():
        return False
    return snippet in _read(path)


def audit(
    case_id: str,
    *,
    artifacts: dict[str, Any] | None = None,
    product_judge: dict[str, Any] | None = None,
    record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    case = case_by_id(case_id)
    artifacts = artifacts or {}
    product_judge = product_judge or (record or {}).get("product_judge") or {}
    judge_verdict = product_judge.get("verdict") if isinstance(product_judge, dict) else None

    labels: list[str] = []
    evidence_verdict = UNKNOWN
    reasons: list[str] = []
    original_ok = None

    source = artifacts.get("source_path")
    source_path = Path(source) if source else None

    if case_id == "migration-mcp-stdio":
        original_ok = _has(source_path, "modelcontextprotocol.io") and _has(
            source_path, "2025-11-25"
        )
        mapped = artifacts.get("mapped") or {}
        if original_ok and mapped.get("kind") == "knowledge" and mapped.get("source", {}).get("url"):
            evidence_verdict = "supports"
            labels.append("mappable_knowledge")
        else:
            evidence_verdict = "contradicts" if original_ok else UNKNOWN
            reasons.append("expected knowledge mapping with specification URL")
    elif case_id == "migration-910b4-peaks":
        original_ok = _has(source_path, "theoretical") and _has(source_path, "245.76")
        mapped = artifacts.get("mapped") or {}
        conditions = mapped.get("conditions") or {}
        if original_ok and mapped.get("kind") == "knowledge" and "cann" in {k.lower() for k in conditions}:
            if artifacts.get("claimed_measured"):
                evidence_verdict = "contradicts"
                reasons.append("theoretical peaks must not be labelled measured")
            else:
                evidence_verdict = "supports"
                labels.extend(["version_conditioned", "theoretical_not_measured"])
        else:
            evidence_verdict = UNKNOWN if not original_ok else "contradicts"
    elif case_id == "migration-conflict-versions":
        kept = artifacts.get("kept_ids") or []
        if len(kept) >= 2:
            evidence_verdict = "supports"
            labels.append("conflict_retain")
            original_ok = True
        else:
            evidence_verdict = "contradicts"
            reasons.append("conflicting conditions must both remain")
    elif case_id == "migration-unmappable-and-private":
        actions = set(artifacts.get("actions") or [])
        published = bool(artifacts.get("published"))
        if "unmappable" in actions and "skip_private" in actions and not published:
            evidence_verdict = "supports"
            labels.extend(["unmappable", "skip_private"])
            original_ok = True
        else:
            evidence_verdict = "contradicts"
            reasons.append("private notes must stay unpublished; unmappable must be reported")
    elif case_id == "migration-feed-layout":
        kinds = artifacts.get("kinds") or {}
        skipped = artifacts.get("skipped") or []
        if kinds.get("topics") == "knowledge" and kinds.get("cases") == "experience" and "maintenance" in skipped:
            evidence_verdict = "supports"
            labels.extend(["feed_topics_cases", "skip_maintenance"])
            original_ok = True
        else:
            evidence_verdict = "contradicts"
    elif case_id == "no-hit":
        hits = artifacts.get("hits")
        if hits == []:
            evidence_verdict = "supports"
            labels.append("no_hit")
            original_ok = True
        elif hits is None:
            evidence_verdict = UNKNOWN
            reasons.append("no retrieval transcript")
        else:
            evidence_verdict = "contradicts"
            reasons.append("query must not invent a hit")
    elif case_id == "version-inapplicable":
        results = artifacts.get("results") or []
        knowledge_hits = [row for row in results if row.get("kind") == "knowledge"]
        if artifacts.get("query_conditions", {}).get("cann") not in {None, "9.0.0"}:
            if not knowledge_hits:
                evidence_verdict = "supports"
                labels.append("version_inapplicable")
                original_ok = True
            else:
                evidence_verdict = "contradicts"
                reasons.append("inapplicable knowledge was returned")
        else:
            evidence_verdict = UNKNOWN
    elif case_id == "hit-unused":
        hit = bool(artifacts.get("hit"))
        used = bool(artifacts.get("used"))
        if hit and not used:
            evidence_verdict = "supports"
            labels.append("hit_unused")
            original_ok = True
        elif not hit:
            evidence_verdict = UNKNOWN
            reasons.append("need a real hit that was not used")
        else:
            evidence_verdict = "contradicts"
            reasons.append("a use record was written for an unused hit")
    elif case_id == "consumption-echo":
        if artifacts.get("consumer_became_producer"):
            evidence_verdict = "contradicts"
            reasons.append("consumer echoed as producer")
        elif artifacts.get("extra_vote"):
            evidence_verdict = "contradicts"
            reasons.append("consumption echo added a vote")
        elif artifacts.get("echo_checked"):
            evidence_verdict = "supports"
            labels.append("consumption_echo")
            original_ok = True
        else:
            evidence_verdict = UNKNOWN
    elif case_id == "success-no-contribution":
        task_ok = artifacts.get("task_succeeded")
        contributed = artifacts.get("experience_contributed")
        if task_ok is True and contributed is False:
            evidence_verdict = "supports"
            labels.append("success_no_contribution")
            original_ok = True
        elif task_ok is None or contributed is None:
            evidence_verdict = UNKNOWN
            reasons.append("need original task artifacts showing success and non-use/non-contribution")
        else:
            evidence_verdict = "contradicts"
    elif case_id == "failure-hypothesis-excluded":
        failed = artifacts.get("task_failed")
        excluded = artifacts.get("hypothesis_excluded")
        if failed is True and excluded is True:
            evidence_verdict = "supports"
            labels.append("failure_hypothesis_excluded")
            original_ok = True
        else:
            evidence_verdict = UNKNOWN if failed is None else "contradicts"
            reasons.append("failure plus an excluded hypothesis needs original artifacts")
    elif case_id == "abc-protocol":
        if artifacts.get("protocol_recorded") and not artifacts.get("declared_effect_success"):
            evidence_verdict = "supports"
            labels.extend(["abc_protocol", "unknown"])
            original_ok = True
        else:
            evidence_verdict = "contradicts" if artifacts.get("declared_effect_success") else UNKNOWN
            reasons.append("protocol without original C artifacts cannot claim effect success")
    elif case_id in {"abc-effect", "negative-repeat-correction"}:
        original = artifacts.get("original_artifacts")
        reduced = artifacts.get("reduced_work")
        avoided = artifacts.get("avoided_error")
        if not original:
            evidence_verdict = UNKNOWN
            labels.append("unknown")
            reasons.append("Codex has not supplied original C artifacts")
        elif reduced or avoided:
            evidence_verdict = "supports"
            original_ok = True
        else:
            evidence_verdict = UNKNOWN
            labels.append("unknown")
            reasons.append("artifacts present but no demonstrated work reduction or avoided error")
    else:
        evidence_verdict = UNKNOWN
        reasons.append("no evidence rule for this case")

    if judge_verdict == "helpful" and "success_no_contribution" in (case.get("labels") or []):
        reasons.append("product helpful is ignored for success-without-contribution")
    if artifacts.get("store_score_increased") and evidence_verdict == "supports" and case["family"] == "effect_evidence":
        reasons.append("Store score increase is recorded but is not the success criterion")

    agreement = None
    if judge_verdict and evidence_verdict not in {UNKNOWN, None}:
        if judge_verdict == "unknown" and evidence_verdict == UNKNOWN:
            agreement = True
        elif judge_verdict == "helpful" and evidence_verdict == "supports" and "success_no_contribution" not in labels:
            agreement = True
        elif judge_verdict == "unhelpful" and evidence_verdict == "contradicts":
            agreement = True
        else:
            agreement = False

    effect_success = False
    if case["family"] == "effect_evidence" and case.get("requires_model"):
        effect_success = bool(
            evidence_verdict == "supports"
            and artifacts.get("original_artifacts")
            and (artifacts.get("reduced_work") or artifacts.get("avoided_error") or labels == ["success_no_contribution"] or "failure_hypothesis_excluded" in labels)
        )
        if judge_verdict == "helpful" and not artifacts.get("original_artifacts"):
            effect_success = False

    return {
        "case": case_id,
        "family": case["family"],
        "evidence_verdict": evidence_verdict,
        "product_judge": judge_verdict,
        "agreement": agreement,
        "labels": labels or list(case.get("labels") or []),
        "original_artifact_ok": original_ok,
        "effect_success": effect_success,
        "forbidden_success_signals": list(FORBIDDEN_SUCCESS_SIGNALS),
        "reasons": reasons,
        "metrics": metrics_from_record(record),
        "windows": case["windows"],
        "npu": case["npu"],
        "note": case["notes"],
    }


def load_artifacts(path: Path) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("artifacts file must be a JSON object")
    return raw
