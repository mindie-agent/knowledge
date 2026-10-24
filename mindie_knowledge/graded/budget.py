"""[Role] Single owner of the delivered-response token budget for graded reads.

[Invariants] One budget target governs the whole delivered payload, including
`exploration_context`; `budget_scope` names exactly what is charged. A unit body
is dropped or explicitly marked `excerpt_truncated` (never silently shortened),
and a truncated or budget-excluded premise clears `ready_to_apply`. When the
irreducible envelope alone exceeds the request, `budget_met` is False and
`minimum_tokens` reports the floor rather than advertising a satisfied budget.
[Timeouts & Locks] Pure in-memory arithmetic over an assembled payload; every
trimming loop removes one item per iteration and is bounded by the payload size.
[Side-Effect Policy] Mutates only the payload passed in; no store, file or
network access and no observation receipt is written here.
"""
from __future__ import annotations

from .index import _excerpt, response_token_estimate, token_estimate

WHOLE_DELIVERY_SCOPE = (
    "whole delivered response, including results, cards, inherited rules, "
    "exploration_context and this usage block"
)


def finalize_usage_estimate(payload):
    """Bind the outer delivery estimate and its own field to one fixed point."""
    payload["usage"]["estimated_tokens"] = 0
    payload["response_token_estimate"] = 0
    for _ in range(8):
        estimate = response_token_estimate(payload)
        if (payload["response_token_estimate"] == estimate
                and payload["usage"]["estimated_tokens"] == estimate):
            break
        payload["response_token_estimate"] = estimate
        payload["usage"]["estimated_tokens"] = estimate
    return payload["response_token_estimate"]


def row_body_tokens(row):
    """Charge one delivered row: its own excerpt plus nested related excerpts.

    Legacy material rows nest `related` block excerpts; graded rows do not.
    Both reach the caller as delivered body text and are charged once.
    """
    total = token_estimate(row.get("excerpt", ""))
    for child in row.get("related", ()) or ():
        total += row_body_tokens(child)
    return total


def recompute_body_tokens(payload):
    """Recompute delivered body tokens; a deduplicated premise is charged once."""
    seen_parents = set()
    total = 0
    for row in payload["results"]:
        total += row_body_tokens(row)
        for parent in row.get("inherited_rules", ()) or ():
            if (not parent.get("deduplicated") and "content" in parent
                    and parent["id"] not in seen_parents):
                seen_parents.add(parent["id"])
                total += token_estimate(parent["content"])
    payload["body_token_estimate"] = total
    payload["usage"]["body_token_estimate"] = total
    return total


def _declare_met(payload):
    payload["budget_met"] = True
    payload["usage"]["budget_met"] = True
    payload.pop("minimum_tokens", None)
    payload["usage"].pop("minimum_tokens", None)


def _declare_unmet(payload):
    """Report the floor the caller must raise the budget to, as a fixed point.

    Writing the floor into the payload lengthens it, so the reported value and
    the delivered estimate are converged together rather than left stale.
    """
    payload["budget_met"] = False
    payload["usage"]["budget_met"] = False
    payload["minimum_tokens"] = 0
    payload["usage"]["minimum_tokens"] = 0
    payload["usage"]["has_more"] = True
    for _ in range(8):
        estimate = finalize_usage_estimate(payload)
        if payload["minimum_tokens"] == estimate:
            break
        payload["minimum_tokens"] = estimate
        payload["usage"]["minimum_tokens"] = estimate


def _drop_card(payload, card, excluded):
    if card["id"] not in excluded:
        excluded.append(card["id"])
    payload["excluded_budget"] = excluded
    payload["usage"]["excluded_budget"] = list(excluded)
    payload["usage"]["has_more"] = True


def _trim_navigation(payload, index, token_budget, excluded):
    """Navigation cards carry no body, so only whole cards can be returned."""
    original_cards = list(payload["cards"])
    original_foundations = {card["id"]: card for card in payload["foundation_cards"]}
    while len(payload["cards"]) > 1 and finalize_usage_estimate(payload) > token_budget:
        dropped = payload["cards"].pop()
        if dropped["id"] not in excluded:
            excluded.append(dropped["id"])
        keep_foundations = set()
        for card in payload["cards"]:
            if card["tier"] == "L1" or card["route"] == "foundation_baseline":
                keep_foundations.add(card["id"])
            keep_foundations.update(
                parent for parent in index.closure[card["id"]]
                if index.units[parent].metadata["tier"] == "L1"
            )
        payload["foundation_cards"] = [
            original_foundations[ident] for ident in original_foundations
            if ident in keep_foundations
        ]
        payload["excluded_budget"] = excluded
        payload["usage"]["excluded_budget"] = list(excluded)
        payload["usage"]["has_more"] = bool(excluded) or len(payload["cards"]) < len(original_cards)
    while finalize_usage_estimate(payload) > token_budget and payload["foundation_cards"]:
        dropped = payload["foundation_cards"].pop()
        if all(card["id"] != dropped["id"] for card in payload["cards"]):
            _drop_card(payload, dropped, excluded)
        else:
            payload["usage"]["has_more"] = True
    while finalize_usage_estimate(payload) > token_budget and payload["cards"]:
        _drop_card(payload, payload["cards"].pop(), excluded)


def _trim_last_result(payload, index, token_budget):
    """Shrink the final result without ever hiding that it was shortened."""
    row = payload["results"][0]
    while row.get("inherited_rules") and finalize_usage_estimate(payload) > token_budget:
        parent = row["inherited_rules"].pop()
        row["inheritance_excluded"].append({"id": parent["id"], "status": "budget_excluded"})
        row["ready_to_apply"] = False
        recompute_body_tokens(payload)
    if finalize_usage_estimate(payload) > token_budget and row.get("inheritance_excluded"):
        row["inheritance_excluded"] = [
            {"id": item["id"], "status": item.get("status", "budget_excluded")}
            for item in row["inheritance_excluded"]
        ]
    if finalize_usage_estimate(payload) > token_budget and "provenance" in row:
        row["provenance"] = {"source_type": row["provenance"].get("source_type", "")}
        row["applicability"] = {"status": row["applicability"].get("status", "")}
    # An L1 premise is mounted whole or not at all; only a procedural body may
    # be shortened, and the shortening is always declared to the caller.
    unit = None if index is None else index.units.get(row.get("id"))
    if finalize_usage_estimate(payload) > token_budget and row.get("tier") != "L1" and unit is not None:
        excess = payload["response_token_estimate"] - token_budget
        share = max(1, token_estimate(row["excerpt"]) - excess - 8)
        content, _used, _truncated = _excerpt(unit.body, share)
        row["excerpt"] = content
        row["excerpt_truncated"] = True
        row["ready_to_apply"] = False
        recompute_body_tokens(payload)


def _row_identity(row):
    """Graded rows are named by unit id; legacy material rows by their ref."""
    for key in ("id", "ref", "entry_id"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _drop_result(payload, excluded):
    dropped = payload["results"].pop()
    identity = _row_identity(dropped)
    if identity is not None and identity not in excluded:
        excluded.append(identity)
    payload["excluded_budget"] = excluded
    payload["usage"]["excluded_budget"] = list(excluded)
    payload["usage"]["has_more"] = True
    recompute_body_tokens(payload)


def _shed_related_previews(payload):
    """Give up optional preview text while keeping every delivered reference.

    A legacy material row previews its neighbouring blocks. The excerpt text is
    the cheapest thing to surrender, but the reference is not: `page_groups`
    anchors `related_next` *after* the previews it already sent, so dropping a
    preview entirely would strand its block — paging resumes past it and no
    later page returns it. Each preview therefore keeps its `ref` and matching
    evidence, and only declares that its excerpt text was withheld.
    """
    shed = False
    for row in payload["results"]:
        for preview in row.get("related", ()) or ():
            if preview.get("excerpt"):
                preview["excerpt"] = ""
                preview["excerpt_omitted"] = True
                shed = True
    if shed:
        payload["usage"]["has_more"] = True
        recompute_body_tokens(payload)
    return shed


def _omit_navigation_body(row):
    """Keep a legacy navigation reference and cursor while withholding its body."""
    if row.get("excerpt"):
        row["excerpt"] = ""
        row["excerpt_omitted"] = True
    for child in row.get("related", ()) or ():
        _omit_navigation_body(child)


def enforce_query_budget(payload, index, *, navigation_only=False, derive=None):
    """Trim one assembled query payload to its whole-delivery token budget.

    `index` supplies unit bodies and closures for graded trimming; a row with
    no matching unit (legacy material) is dropped whole rather than shortened.
    `derive` refreshes payload-specific derived fields (such as a mixed page's
    `source_results`) so the final estimate describes the outward payload.
    """
    token_budget = payload["token_budget"]
    payload["budget_scope"] = WHOLE_DELIVERY_SCOPE
    _declare_met(payload)
    excluded = list(payload["excluded_budget"])
    if derive is not None:
        derive(payload)

    while finalize_usage_estimate(payload) > token_budget and payload.get("unverified_cards"):
        payload["unverified_cards"].pop()
        if not payload["unverified_cards"]:
            payload.pop("unverified_cards", None)

    if finalize_usage_estimate(payload) > token_budget:
        _shed_related_previews(payload)

    if navigation_only:
        # Graded navigation already has cards and no result bodies. Legacy
        # material retains its references/cursors so installing a graded unit
        # cannot hide an existing match from a roomy navigation response.
        for row in payload["results"]:
            _omit_navigation_body(row)
        recompute_body_tokens(payload)
        while payload["results"] and finalize_usage_estimate(payload) > token_budget:
            _drop_result(payload, excluded)
        _trim_navigation(payload, index, token_budget, excluded)
    else:
        if finalize_usage_estimate(payload) > token_budget and payload["foundation_cards"]:
            payload["foundation_cards"] = []
        if finalize_usage_estimate(payload) > token_budget and payload["cards"]:
            payload["cards"] = []
        while len(payload["results"]) > 1 and finalize_usage_estimate(payload) > token_budget:
            _drop_result(payload, excluded)
        if finalize_usage_estimate(payload) > token_budget and len(payload["results"]) == 1:
            _trim_last_result(payload, index, token_budget)
        if finalize_usage_estimate(payload) > token_budget and payload["results"]:
            _drop_result(payload, excluded)

    # Derived fields describe what survived, so they are refreshed before the
    # estimate is bound and before any floor is claimed.
    if derive is not None:
        derive(payload)

    # Everything trimmable is gone. A remaining overrun is the irreducible
    # protocol envelope (and, in explore mode, the mounted fact pack); report
    # the floor instead of claiming an impossible budget was satisfied.
    if finalize_usage_estimate(payload) > token_budget:
        _declare_unmet(payload)


def reconcile_budget_truth(payload, token_budget):
    """Re-decide the budget verdict after compat fields enlarge a trimmed payload.

    The single-ref explain contract merges the historical whole-body view on top
    of an already-trimmed closure, so the delivered payload is legitimately
    larger than what `enforce_explain_budget` measured. The verdict and reported
    floor must describe this final payload, never the pre-merge estimate.
    """
    if finalize_usage_estimate(payload) > token_budget:
        _declare_unmet(payload)
    else:
        _declare_met(payload)


def enforce_explain_budget(result, index, token_budget):
    """Trim an assembled closure payload; a body is dropped, never shortened."""
    result["budget_scope"] = WHOLE_DELIVERY_SCOPE
    _declare_met(result)
    while finalize_usage_estimate(result) > token_budget and (result["units"] or result["requires"]):
        bucket = "units" if result["units"] else "requires"
        dropped = result[bucket].pop()
        required_tokens = token_estimate(dropped.get("content", ""))
        if bucket == "units" and result["requires"]:
            still_needed = {
                parent for item in result["units"]
                for parent in index.closure[item["id"]]
            }
            kept_requires = []
            for req in result["requires"]:
                if req["id"] in still_needed:
                    kept_requires.append(req)
                else:
                    required_tokens += token_estimate(req.get("content", ""))
            result["requires"] = kept_requires
        result["unresolved"].append({
            "id": dropped["id"], "status": "budget_excluded",
            "required_tokens": max(1, required_tokens),
        })
        result["usage"]["body_token_estimate"] = sum(
            token_estimate(item.get("content", ""))
            for group in ("requires", "units") for item in result.get(group, ())
        )
        result["usage"]["has_more"] = True
    if finalize_usage_estimate(result) > token_budget:
        _declare_unmet(result)
