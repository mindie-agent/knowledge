"""[Role] Keep one query entry point readable during C5 corpus migration.

[Invariants] Installing a local C5 candidate never hides existing task matches.
Mixed responses identify each reference kind, keep legacy block continuation
semantics, and are charged by the shared `graded.budget` owner so one declared
`usage` describes the graded and legacy material actually returned. BM25 scores
from distinct indexes are not compared as confidence or a common ranking scale.
[Timeouts & Locks] Existing Store query locks; no network or subprocess work.
[Side-Effect Policy] Only normal query observation receipts are persisted.
"""
from __future__ import annotations

from copy import deepcopy

from .budget import enforce_query_budget, recompute_body_tokens
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


def mixed_query(store, legacy, query, *, limit, conditions, domain_path, token_budget,
                include_unknown, include_archived=False, navigation_only=False,
                run_id=None, mode="reuse",
                ledger_entries=None, bottleneck_summary=None, native_subcontext=False,
                goal=""):
    # `limit` is already validated by `query_request` on every path that reaches
    # here; only the caller's original `token_budget` has no other owner, since
    # the graded index below sees the derived share instead.
    if type(token_budget) is not int or not 1 <= token_budget <= 16000:
        raise ValueError("token_budget must be between 1 and 16000")
    # Graded units are assembled first; they receive the whole response budget
    # when no legacy row competes for it, and whatever legacy excerpts leave
    # behind otherwise. There is no fixed half-budget split.
    graded_limit = max(1, (limit + 1) // 2)
    graded_budget = token_budget if not legacy["results"] else max(
        1, token_budget * min(limit, graded_limit + len(legacy["results"])) // limit)
    # The graded sub-page is not what the caller receives; this page composes
    # and trims it further, so its delivery receipt is recorded below, once.
    graded = store.graded.query(query, limit=graded_limit, conditions=conditions,
                                domain_path=domain_path, token_budget=graded_budget,
                                include_unknown=include_unknown, include_archived=include_archived,
                                navigation_only=navigation_only, observe_delivery=False,
                                run_id=run_id, mode=mode, ledger_entries=ledger_entries,
                                bottleneck_summary=bottleneck_summary,
                                native_subcontext=native_subcontext, goal=goal)
    rows = graded["results"]
    legacy_slots = limit - len(rows)
    selected_legacy = legacy["results"][:legacy_slots]
    remaining = token_budget - graded["body_token_estimate"]
    used = graded["body_token_estimate"]
    old = []
    for position, item in enumerate(selected_legacy):
        share = max(0, remaining // (len(selected_legacy) - position))
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
    graded_usage = graded.get("usage", {})
    payload = {"domain": store.domain, "retrieval": "mixed-bm25", "page": "mixed",
               "results": combined, "cards": graded.get("cards", []),
               "foundation_cards": graded.get("foundation_cards", []),
               "usage": {"token_budget": token_budget,
                         "estimator": graded_usage.get("estimator", "utf8_bytes_div_4_ceiling"),
                         "has_more": bool(graded_usage.get("has_more"))
                                     or len(old) < len(legacy["results"]),
                         "excluded_budget": list(graded_usage.get("excluded_budget", ()))},
               "mode": graded.get("mode", mode),
               "token_budget": token_budget,
               "excluded_budget": list(graded.get("excluded_budget", ())),
               "source_results": {"graded": len(rows), "legacy_material": len(old)},
               "domain_path_scope": "graded ranking preference only; legacy matches retain their existing domain",
               "note": "Graded rules and legacy task material retain distinct reference types. Per-source ranks are "
                       "interleaved within one limit. Legacy related_next cursors continue through the original block query."}
    if mode == "explore":
        payload["exploration_context"] = graded.get("exploration_context")
        payload["stripped_l3_ids"] = graded.get("stripped_l3_ids", [])
    def count_sources(current):
        current["source_results"] = {
            "graded": sum(1 for row in current["results"] if row.get("source") == "graded"),
            "legacy_material": sum(1 for row in current["results"]
                                   if row.get("source") == "legacy_material"),
        }

    # One owner charges the delivered graded + legacy material, so `usage` can
    # never describe only the graded half of what the caller receives.
    recompute_body_tokens(payload)
    index = store.graded.routing_index()
    enforce_query_budget(payload, index, navigation_only=navigation_only,
                         derive=count_sources)
    # Only the graded units that survived composition are counted as delivered.
    store.graded.observe_delivered(index, payload, delivery="query", run_id=run_id)
    return payload
