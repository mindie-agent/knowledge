"""[Role] Pure in-memory deterministic retrieval evaluation harness for RoutingIndex and GradedRepository.

[Invariants] Recall@K, MRR, foundation-baseline mounting, explore-mode L3 stripping,
and forbidden-unit leak rates are computed deterministically without fabricating
positive evidence on empty suites. Mode "standard" maps to "reuse" on the underlying
index while preserving "standard" in case reports. Any forbidden unit leak,
unstripped L3 workflow in explore mode, or threshold miss fails the suite gate.
[Timeouts & Locks] Pure CPU in-memory evaluation; zero subprocesses, network calls,
model invocations, locks, or sleeps.
[Side-Effect Policy] Read-only query execution against the supplied RoutingIndex,
GradedRepository, or Store; never mutates corpus files or unit revisions.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any

from .index import RoutingIndex

EVAL_SCHEMA = "mindie-retrieval-eval/1"
VALID_MODES = frozenset({"standard", "reuse", "diagnose", "explore"})
FOUNDATION_ROUTES = frozenset({"foundation_baseline", "inherited_foundation", "promoted_l3_foundation"})


def _id_tuple(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, Mapping)):
        items = list(value)
    else:
        raise ValueError(f"{field} must be a sequence of unit IDs")
    cleaned: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field} entries must be nonempty strings")
        token = item.strip()
        if token not in cleaned:
            cleaned.append(token)
    return tuple(cleaned)


def _check_rate(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number between 0.0 and 1.0")
    rate = float(value)
    if not 0.0 <= rate <= 1.0:
        raise ValueError(f"{name} must be between 0.0 and 1.0")
    return rate


def _append_unique(dest: list[str], value: Any) -> None:
    if isinstance(value, str) and value and value not in dest:
        dest.append(value)


@dataclass(frozen=True)
class RetrievalEvalCase:
    query: str
    conditions: Mapping[str, Any] | None = None
    mode: str = "standard"
    expected_unit_ids: Sequence[str] = ()
    forbidden_unit_ids: Sequence[str] = ()
    require_foundation_baseline: bool | Sequence[str] = False
    top_k: int = 5
    case_id: str = ""
    id: str = ""
    domain_path: str | None = None
    token_budget: int = 4096
    include_unknown: bool = False
    navigation_only: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.query, str) or not self.query.strip():
            raise ValueError("query must be nonempty text")
        if self.mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
        if type(self.top_k) is not int or not 1 <= self.top_k <= 20:
            raise ValueError("top_k must be an integer between 1 and 20")
        if type(self.token_budget) is not int or not 1 <= self.token_budget <= 16000:
            raise ValueError("token_budget must be an integer between 1 and 16000")
        if self.conditions is not None and not isinstance(self.conditions, Mapping):
            raise ValueError("conditions must be a mapping or None")
        ident = (self.case_id or self.id or "").strip()
        object.__setattr__(self, "case_id", ident)
        object.__setattr__(self, "id", ident)
        object.__setattr__(self, "expected_unit_ids", _id_tuple(self.expected_unit_ids, "expected_unit_ids"))
        object.__setattr__(self, "forbidden_unit_ids", _id_tuple(self.forbidden_unit_ids, "forbidden_unit_ids"))
        if not isinstance(self.require_foundation_baseline, bool):
            req = _id_tuple(self.require_foundation_baseline, "require_foundation_baseline")
            object.__setattr__(self, "require_foundation_baseline", req)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], index: int = 0) -> RetrievalEvalCase:
        if not isinstance(data, Mapping):
            raise ValueError("retrieval eval case must be a mapping or RetrievalEvalCase")
        case_id = str(data.get("case_id") or data.get("id") or f"case-{index + 1}").strip()
        return cls(
            query=data.get("query", ""),
            conditions=data.get("conditions"),
            mode=data.get("mode", "standard"),
            expected_unit_ids=data.get("expected_unit_ids", data.get("expected", ())),
            forbidden_unit_ids=data.get("forbidden_unit_ids", data.get("forbidden", data.get("excluded", ()))),
            require_foundation_baseline=data.get("require_foundation_baseline", False),
            top_k=data.get("top_k", data.get("limit", 5)),
            case_id=case_id,
            domain_path=data.get("domain_path"),
            token_budget=data.get("token_budget", 4096),
            include_unknown=bool(data.get("include_unknown", False)),
            navigation_only=bool(data.get("navigation_only", False)),
        )


def _resolve_query_target(target: Any):
    if hasattr(target, "query") and callable(target.query):
        return target.query
    if isinstance(target, (str, Path)):
        return RoutingIndex.from_root(Path(target)).query
    if isinstance(target, Iterable) and not isinstance(target, (bytes, bytearray, Mapping)):
        return RoutingIndex(list(target)).query
    raise ValueError("target must be a RoutingIndex, GradedRepository, Store, path, or unit iterable")


def _evaluate_case(query_fn, case: RetrievalEvalCase, index: int) -> dict[str, Any]:
    case_id = case.case_id or f"case-{index + 1}"
    response = query_fn(
        case.query,
        conditions=dict(case.conditions) if case.conditions is not None else None,
        domain_path=case.domain_path,
        limit=case.top_k,
        token_budget=case.token_budget,
        include_unknown=case.include_unknown,
        navigation_only=case.navigation_only,
        mode="reuse" if case.mode == "standard" else case.mode,
    )
    primary_rows = response.get("cards" if case.navigation_only else "results", ())
    primary_ids: list[str] = []
    for row in primary_rows[: case.top_k]:
        _append_unique(primary_ids, row.get("id"))

    ranked_ids = list(primary_ids)
    for row in response.get("results", ())[: case.top_k]:
        for rule in row.get("inherited_rules", ()):
            _append_unique(ranked_ids, rule.get("id"))
    for card in response.get("foundation_cards", ()):
        _append_unique(ranked_ids, card.get("id"))

    foundation_ids: list[str] = []
    for group in (response.get("foundation_cards", ()), response.get("results", ()), response.get("cards", ())):
        for item in group:
            if item.get("route") in FOUNDATION_ROUTES or item.get("tier") == "L1":
                _append_unique(foundation_ids, item.get("id"))
    explore_ctx = response.get("exploration_context") or {}
    for ident in (explore_ctx.get("subagent_fact_pack") or {}).get("l1_foundation_ids", ()):
        _append_unique(foundation_ids, ident)
        _append_unique(ranked_ids, ident)
    for unit in explore_ctx.get("mounted_units", ()):
        _append_unique(ranked_ids, unit.get("id"))

    unstripped_l3 = [
        item["id"]
        for group in (response.get("results", ()), response.get("cards", ()), explore_ctx.get("mounted_units", ()))
        for item in group
        if case.mode == "explore" and item.get("tier") == "L3" and isinstance(item.get("id"), str)
    ]
    rank_map = {ident: pos for pos, ident in enumerate(ranked_ids, start=1)}
    matched = [uid for uid in case.expected_unit_ids if uid in rank_map]
    missing = [uid for uid in case.expected_unit_ids if uid not in rank_map]
    recall = len(matched) / len(case.expected_unit_ids) if case.expected_unit_ids else 1.0
    rr = 1.0 / min(rank_map[uid] for uid in matched) if matched else (0.0 if case.expected_unit_ids else 1.0)

    req_foundation = case.require_foundation_baseline
    if isinstance(req_foundation, tuple):
        foundation_ok, requires_foundation = bool(req_foundation) and set(req_foundation).issubset(foundation_ids), True
    elif req_foundation is True:
        foundation_ok, requires_foundation = bool(foundation_ids), True
    else:
        foundation_ok, requires_foundation = True, False

    leaked = sorted((set(case.forbidden_unit_ids) & set(ranked_ids)) | set(unstripped_l3))
    forbidden_leaked = bool(leaked)
    explore_l3_stripped = not unstripped_l3
    passed = not missing and foundation_ok and not forbidden_leaked and explore_l3_stripped
    return {
        "case_id": case_id,
        "id": case_id,
        "query": case.query,
        "mode": case.mode,
        "top_k": case.top_k,
        "passed": passed,
        "hit_unit_ids": primary_ids,
        "ranked_unit_ids": ranked_ids,
        "expected_unit_ids": list(case.expected_unit_ids),
        "matched_unit_ids": matched,
        "missing_unit_ids": missing,
        "forbidden_unit_ids": list(case.forbidden_unit_ids),
        "leaked_unit_ids": leaked,
        "foundation_unit_ids": foundation_ids,
        "stripped_l3_ids": list(response.get("stripped_l3_ids", ())),
        "recall_at_k": recall,
        "reciprocal_rank": rr,
        "requires_foundation_baseline": requires_foundation,
        "foundation_baseline_satisfied": foundation_ok,
        "forbidden_leaked": forbidden_leaked,
        "explore_l3_stripped": explore_l3_stripped,
    }


def evaluate_retrieval_suite(
    target: Any,
    cases: Sequence[RetrievalEvalCase | Mapping[str, Any]],
    *,
    min_recall_at_k: float = 1.0,
    min_mrr: float = 1.0,
    min_foundation_baseline_rate: float = 1.0,
    max_forbidden_leak_rate: float = 0.0,
) -> dict[str, Any]:
    """Evaluate a declarative retrieval suite and enforce quality-gate thresholds."""
    min_recall = _check_rate(min_recall_at_k, "min_recall_at_k")
    min_mrr_val = _check_rate(min_mrr, "min_mrr")
    min_foundation = _check_rate(min_foundation_baseline_rate, "min_foundation_baseline_rate")
    max_leak = _check_rate(max_forbidden_leak_rate, "max_forbidden_leak_rate")
    if not isinstance(cases, Sequence) or isinstance(cases, (str, bytes)) or not cases:
        raise ValueError("cases must be a nonempty sequence of RetrievalEvalCase items")

    query_fn = _resolve_query_target(target)
    normalized = [
        item if isinstance(item, RetrievalEvalCase) else RetrievalEvalCase.from_mapping(item, idx)
        for idx, item in enumerate(cases)
    ]
    per_case = [_evaluate_case(query_fn, case, idx) for idx, case in enumerate(normalized)]

    expected_total = sum(len(item["expected_unit_ids"]) for item in per_case)
    expected_found = sum(len(item["matched_unit_ids"]) for item in per_case)
    positive_cases = [item for item in per_case if item["expected_unit_ids"]]
    recall_at_k = expected_found / expected_total if expected_total > 0 else 1.0
    mrr = sum(item["reciprocal_rank"] for item in positive_cases) / len(positive_cases) if positive_cases else 1.0

    foundation_cases = [item for item in per_case if item["requires_foundation_baseline"]]
    if foundation_cases:
        foundation_baseline_rate = (
            sum(1 for item in foundation_cases if item["foundation_baseline_satisfied"]) / len(foundation_cases)
        )
    else:
        foundation_baseline_rate = (
            sum(1 for item in per_case if item["foundation_unit_ids"]) / len(per_case)
            if any(item["foundation_unit_ids"] for item in per_case)
            else 1.0
        )
    forbidden_leak_rate = sum(1 for item in per_case if item["forbidden_leaked"]) / len(per_case)

    failures: list[str] = []
    if recall_at_k < min_recall:
        failures.append(f"recall_at_k={recall_at_k:.3f} < min_recall_at_k={min_recall:.3f}")
    if mrr < min_mrr_val:
        failures.append(f"mrr={mrr:.3f} < min_mrr={min_mrr_val:.3f}")
    if foundation_baseline_rate < min_foundation:
        failures.append(
            f"foundation_baseline_rate={foundation_baseline_rate:.3f} < min_foundation_baseline_rate={min_foundation:.3f}"
        )
    if forbidden_leak_rate > max_leak:
        failures.append(f"forbidden_leak_rate={forbidden_leak_rate:.3f} > max_forbidden_leak_rate={max_leak:.3f}")
    for item in per_case:
        if not item["explore_l3_stripped"]:
            failures.append(f"{item['case_id']}: explore mode leaked L3 workflow units")

    passed = not failures
    return {
        "schema": EVAL_SCHEMA,
        "passed": passed,
        "ok": passed,
        "gate_passed": passed,
        "case_count": len(per_case),
        "passed_case_count": sum(1 for item in per_case if item["passed"]),
        "failed_case_ids": [item["case_id"] for item in per_case if not item["passed"]],
        "recall_at_k": recall_at_k,
        "mrr": mrr,
        "foundation_baseline_rate": foundation_baseline_rate,
        "forbidden_leak_rate": forbidden_leak_rate,
        "per_case_results": per_case,
        "failures": failures,
    }


__all__ = ["EVAL_SCHEMA", "RetrievalEvalCase", "evaluate_retrieval_suite"]
