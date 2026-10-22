"""[Role] Exact dependency-closure assembly extracted from `RoutingIndex`.

[Invariants]
- Shared ancestors are emitted once in topological order; a body is never
  shortened to fit the token budget and every exclusion stays in `unresolved`.
- A requested ancestor excluded for budget blocks its child rather than
  returning a partial rule; explore mode strips L3 bodies only from the payload.
- Known refs suppress only an exact `id@revision`, never an older/unversioned ref.

[Timeouts & Locks]
- Pure in-memory graph and metadata work; no subprocess, network or lock wait.

[Side-Effect Policy]
- Reads the already-built RoutingIndex; returns a new payload and never mutates
  units, closures, conditions or cached counts.
"""
from __future__ import annotations

from .conditions import match_conditions
from .index import _ref_parts, response_token_estimate, token_estimate


def explain_closure(index, refs, *, conditions=None, known_refs=None, domain=None,
                    token_budget=4096, mode="reuse", ledger_entries=None,
                    bottleneck_summary=None, native_subcontext=False, goal=""):
    """Assemble an exact dependency closure without truncating any body.

    ``known_refs`` names only exact, currently visible ``id@revision``
    pairs. Shared ancestors are emitted once in topological order and
    budget misses stay explicit; a body is never shortened to fit.
    """
    if mode not in {"reuse", "diagnose", "explore"}:
        raise ValueError("mode must be one of ['diagnose', 'explore', 'reuse']")
    if type(token_budget) is not int or not 1 <= token_budget <= 16000:
        raise ValueError("token_budget must be between 1 and 16000")
    if conditions is not None and not isinstance(conditions, dict):
        raise ValueError("conditions must be an object of observed facts")
    if isinstance(refs, str):
        refs = [refs]
    if refs is None:
        refs = []
    try:
        requested = list(refs)
    except TypeError as exc:
        raise ValueError("refs must be a sequence of stable unit references") from exc
    if isinstance(known_refs, str):
        known_refs = [known_refs]
    known_refs = [] if known_refs is None else known_refs

    visible = set()
    for raw in known_refs:
        parts = _ref_parts(raw)
        if parts is None:
            continue
        ref_domain, ident, revision = parts
        if domain is not None and ref_domain is not None and ref_domain != domain:
            continue
        # Only an exact revision can suppress re-sending a body. An
        # unversioned or older ref never hides the current revision.
        if revision is not None:
            visible.add((ident, revision))

    grouped = None
    if conditions is not None:
        grouped = {key: match_conditions(required, conditions)
                   for key, required in index.conditions.items()}

    def status_of(ident):
        return grouped[index.condition_keys[ident]]

    def missing_of(status):
        return [reason.split(":", 1)[0] for reason in status.reasons
                if reason.endswith(("not supplied", "version not supplied"))]

    units_out = []
    requires_out = []
    unresolved_out = []
    seen_unresolved = set()

    def add_unresolved(entry):
        key = (entry.get("id"), entry.get("status"))
        if key in seen_unresolved:
            return
        seen_unresolved.add(key)
        unresolved_out.append(entry)

    resolved = []
    seen_requested = set()
    for raw in requested:
        parts = _ref_parts(raw)
        if parts is None:
            add_unresolved({"id": str(raw), "status": "not_found"})
            continue
        ref_domain, ident, revision = parts
        if domain is not None and ref_domain is not None and ref_domain != domain:
            add_unresolved({"id": ident, "status": "not_found"})
            continue
        if (ident, revision) in seen_requested:
            continue
        seen_requested.add((ident, revision))
        unit = index.units.get(ident)
        if unit is None:
            add_unresolved({"id": ident, "status": "not_found"})
            continue
        if revision is not None and revision != unit.revision:
            entry = {"id": ident, "status": "removed_or_superseded",
                     "current_revision": unit.revision}
            if unit.superseded_by is not None:
                entry["superseded_by"] = unit.superseded_by
            add_unresolved(entry)
            continue
        if unit.status == "superseded":
            entry = {"id": ident, "status": "removed_or_superseded",
                     "current_revision": unit.revision}
            if unit.superseded_by is not None:
                entry["superseded_by"] = unit.superseded_by
            add_unresolved(entry)
            continue
        resolved.append((ident, revision, raw))

    valid = []
    for ident, _revision, raw in resolved:
        superseded_node = next(
            (node for node in index.closure[ident]
             if node != ident and index.units[node].status == "superseded"), None)
        if superseded_node is not None:
            unit = index.units[ident]
            add_unresolved({"ref": raw, "id": ident, "status": "removed_or_superseded",
                            "blocked_by": superseded_node,
                            "superseded_by": index.units[superseded_node].superseded_by,
                            "current_revision": unit.revision})
            continue
        suspect_node = next(
            (node for node in index.closure[ident]
             if node != ident and index.units[node].status == "suspect"), None)
        if suspect_node is not None:
            unit = index.units[ident]
            add_unresolved({"ref": raw, "id": ident, "status": "suspect_ancestor",
                            "blocked_by": suspect_node,
                            "current_revision": unit.revision})
            continue
        if grouped is None:
            valid.append(ident)
            continue
        nodes = [*index.closure[ident], ident]
        unmatched = [node for node in nodes if status_of(node).status != "matched"]
        if not unmatched:
            valid.append(ident)
            continue
        for node in unmatched:
            status = status_of(node)
            missing = missing_of(status)
            unit_conds = index.units[node].metadata["conditions"]
            add_unresolved({"id": node,
                            "status": "condition_mismatch" if status.status == "mismatch" else "condition_unknown",
                            "missing_conditions": missing,
                            "required_conditions": {k: unit_conds[k] for k in missing if k in unit_conds}})
        # A matched child is still not applicable when a required premise
        # is missing or contradicts the observed facts.
        if ident not in {entry["id"] for entry in unresolved_out}:
            mismatched = any(status_of(node).status == "mismatch" for node in unmatched)
            missing = []
            required_conds = {}
            for node in unmatched:
                unit_conds = index.units[node].metadata["conditions"]
                for name in missing_of(status_of(node)):
                    if name not in missing:
                        missing.append(name)
                    if name in unit_conds and name not in required_conds:
                        required_conds[name] = unit_conds[name]
            add_unresolved({"id": ident,
                            "status": "condition_mismatch" if mismatched else "condition_unknown",
                            "missing_conditions": missing,
                            "required_conditions": required_conds})

    valid_set = set(valid)
    ordered = []

    def add_ordered(ident):
        if ident in ordered:
            return
        for parent in index.closure[ident]:
            if parent in valid_set:
                add_ordered(parent)
        ordered.append(ident)

    for ident in valid:
        add_ordered(ident)

    def entry_for(ident):
        unit = index.units[ident]
        status = status_of(ident) if grouped is not None else None
        return {"id": ident, "revision": unit.revision,
                "title": unit.metadata["title"], "tier": unit.metadata["tier"],
                "category": unit.metadata["category"],
                "evidence_strength": unit.metadata["evidence_strength"],
                "status": unit.status, "superseded_by": unit.superseded_by,
                "domain_path": unit.metadata["domain_path"],
                "conditions": unit.metadata["conditions"],
                "provenance": unit.metadata["provenance"],
                "inherits_from": list(unit.metadata["inherits_from"]),
                "content": unit.body,
                "applicability": status.to_dict() if status is not None else None,
                "ready_to_apply": bool(status is not None and status.status == "matched"
                                       and unit.status == "active"),
                "missing_conditions": missing_of(status) if status is not None else []}

    loaded = set()
    body_tokens = 0

    def estimate(requires, units):
        return response_token_estimate(
            {"units": units, "requires": requires, "unresolved": unresolved_out})

    for ident in ordered:
        unit = index.units[ident]
        ancestors = index.closure[ident]
        new_ancestors = [parent for parent in ancestors
                         if parent not in loaded
                         and (parent, index.units[parent].revision) not in visible]
        unit_needed = ident not in loaded and (ident, unit.revision) not in visible
        # A requested ancestor excluded earlier for budget leaves this
        # child without a required premise; do not assemble a partial rule.
        blocked = [parent for parent in new_ancestors
                   if parent in valid_set and parent not in loaded
                   and (parent, index.units[parent].revision) not in visible]
        if blocked:
            new_requires = [entry_for(parent) for parent in new_ancestors if parent not in valid_set]
            new_units = [entry_for(ident)] if unit_needed else []
            required = estimate(requires_out + new_requires, units_out + new_units) - estimate(
                requires_out, units_out)
            add_unresolved({"id": ident, "status": "budget_excluded",
                            "required_tokens": max(0, required)})
            continue
        new_requires = [entry_for(parent) for parent in new_ancestors if parent not in valid_set]
        new_units = [entry_for(ident)] if unit_needed else []
        if not new_requires and not new_units:
            continue
        trial_requires = requires_out + new_requires
        trial_units = units_out + new_units
        current_estimate = estimate(requires_out, units_out)
        trial_estimate = estimate(trial_requires, trial_units)
        if trial_estimate > token_budget:
            required = trial_estimate - current_estimate
            if required <= 0:
                required = sum(token_estimate(item["content"])
                               for item in new_requires + new_units)
            add_unresolved({"id": ident, "status": "budget_excluded",
                            "required_tokens": required})
            continue
        requires_out, units_out = trial_requires, trial_units
        loaded.add(ident)
        if unit_needed:
            body_tokens += token_estimate(unit.body)
        for item in new_requires:
            loaded.add(item["id"])
            body_tokens += token_estimate(item["content"])

    stripped_l3_ids = []
    if mode == "explore":
        all_entries = [*requires_out, *units_out]
        stripped_l3_ids = sorted({item["id"] for item in all_entries
                                  if item.get("tier") == "L3"})
        if stripped_l3_ids:
            body_tokens -= sum(token_estimate(item.get("content", ""))
                               for item in all_entries if item.get("tier") == "L3")
            requires_out = [item for item in requires_out if item.get("tier") != "L3"]
            units_out = [item for item in units_out if item.get("tier") != "L3"]
    estimated = response_token_estimate(
        {"units": units_out, "requires": requires_out, "unresolved": unresolved_out})
    payload = {"units": units_out, "requires": requires_out, "unresolved": unresolved_out,
               "usage": {"token_budget": token_budget, "estimated_tokens": estimated,
                         "body_token_estimate": body_tokens,
                         "estimator": "utf8_bytes_div_4_ceiling",
                         "has_more": any(item["status"] == "budget_excluded"
                                         for item in unresolved_out)},
               "mode": mode}
    if mode == "explore":
        rows, seen = [], set()
        for item in [*requires_out, *units_out]:
            if item["id"] not in seen:
                seen.add(item["id"])
                rows.append({**item, "body": item.get("content", "")})
        for ident in stripped_l3_ids:
            if ident in seen:
                continue
            unit = index.units[ident]
            rows.append({**unit.metadata, "body": unit.body,
                         "inherited_rules": [
                             {**index.units[parent].metadata, "body": index.units[parent].body}
                             for parent in index.closure[ident]
                             if index.units[parent].metadata["tier"] in {"L1", "L2"}]})
        from .explore import assess_stagnation, build_exploration_context
        context = build_exploration_context(
            rows, bottleneck_summary=bottleneck_summary or {},
            failed_attempts=ledger_entries or [], native_subcontext=native_subcontext,
            goal=goal, conditions=conditions)
        context["stagnation"] = assess_stagnation(ledger_entries or [])
        context["stripped_l3_ids"] = stripped_l3_ids
        context["subagent_fact_pack"]["stripped_l3_ids"] = stripped_l3_ids
        payload["exploration_context"] = context
        payload["stripped_l3_ids"] = stripped_l3_ids
    return payload

