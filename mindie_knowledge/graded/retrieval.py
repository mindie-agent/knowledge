"""[Role] Keep one query entry point readable during C5 corpus migration.

[Invariants] Installing a local C5 candidate never hides existing task matches.
Mixed responses identify each reference kind, share one result/body budget and
keep legacy block continuation semantics. BM25 scores from distinct indexes are
not compared as confidence or as a common ranking scale.
[Timeouts & Locks] Existing Store query locks; no network or subprocess work.
[Side-Effect Policy] Only normal query observation receipts are persisted.
"""
from __future__ import annotations

from copy import deepcopy

from .index import _excerpt


def _legacy_excerpt(row, budget):
    result = deepcopy(row)
    result.update(source="legacy_material", reference_kind="material_block")
    text, used, shortened = _excerpt(result.get("excerpt", ""), budget)
    result["excerpt"] = text
    if shortened:
        result["excerpt_truncated"] = True
    remaining = budget - used
    related = []
    for match in result.get("related", []):
        child, cost = _legacy_excerpt(match, remaining)
        related.append(child)
        used += cost
        remaining -= cost
    if "related" in result:
        result["related"] = related
    return result, used


def mixed_query(store, legacy, query, *, limit, conditions, domain_path, token_budget, include_unknown):
    if type(token_budget) is not int or not 1 <= token_budget <= 16000:
        raise ValueError("token_budget must be between 1 and 16000")
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    # Preserve at least one result slot for each matching source when the
    # caller permits two; per-source ranks are then interleaved transparently.
    graded_limit = max(1, (limit + 1) // 2)
    graded_budget = max(1, token_budget // 2)
    graded = store.graded.query(query, limit=graded_limit, conditions=conditions,
                                domain_path=domain_path, token_budget=graded_budget,
                                include_unknown=include_unknown)
    rows = graded["results"]
    legacy_slots = limit - len(rows)
    selected_legacy = legacy["results"][:legacy_slots]
    remaining = token_budget - graded["body_token_estimate"]
    used = graded["body_token_estimate"]
    old = []
    for position, item in enumerate(selected_legacy):
        share = remaining // (len(selected_legacy) - position)
        result, cost = _legacy_excerpt(item, share)
        old.append(result)
        used += cost
        remaining -= cost
    combined = []
    for position in range(max(len(rows), len(old))):
        if position < len(rows):
            combined.append(rows[position])
        if position < len(old):
            combined.append(old[position])
    return {"domain": store.domain, "retrieval": "mixed-bm25", "page": "mixed",
            "results": combined, "body_token_estimate": used, "token_budget": token_budget,
            "budget_scope": "body excerpts, related excerpts and inherited rules; metadata excluded",
            "source_results": {"graded": len(rows), "legacy_material": len(old)},
            "domain_path_scope": "graded ranking preference only; legacy matches retain their existing domain",
            "note": "Graded rules and legacy task material retain distinct reference types. Per-source ranks are "
                    "interleaved within one limit. Legacy related_next cursors continue through the original block query."}
