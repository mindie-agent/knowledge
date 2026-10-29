"""[Role] C8 inheritance index and condition-aware local graded retrieval.

[Invariants] Resolve all stable references at build time; never truncate semantic
inheritance by depth. inherits_from names necessary premises, not related topics:
each returned parent independently matches task conditions. Hardware constraints
are never inferred from a unit's tier, corpus path or unrelated nearby axioms.
Shared parents are charged once per response; explain_closure never truncates a
unit body and reports every budget miss explicitly.
[Timeouts & Locks] No model/network work; build time is separate from query time.
[Side-Effect Policy] Read explicit corpus roots only; never write or import tasks.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import math
import os
from pathlib import Path
import re
import time

from ..retrieval import query_tokens, token_counts, tokens
from .conditions import match_conditions
from .units import UNIT_ID, GradedUnit, canonical

SKIP_DIRECTORIES = {".git", "tasks", "feedback", "index", "docs", "private"}
MAX_UNIT_BYTES = 262_144
FOUNDATION_SCORE = 0.05


def token_estimate(text: str) -> int:
    """Conservative UTF-8/4 estimate, explicitly not a model tokenizer count."""
    return math.ceil(len(text.encode("utf-8")) / 4)


def response_token_estimate(payload) -> int:
    """Whole-response estimate over its canonical JSON serialization."""
    return token_estimate(canonical(payload))


def _excerpt(text: str, budget: int) -> tuple[str, int, bool]:
    raw = text.encode("utf-8")
    chunk = raw[:budget * 4].decode("utf-8", errors="ignore")
    return chunk, token_estimate(chunk), len(chunk) != len(text)


def read_units(root: Path) -> list[GradedUnit]:
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("corpus root must be a directory")
    units = []
    for directory, children, files in os.walk(root):
        children[:] = sorted(name for name in children if (Path(directory) != root or name not in SKIP_DIRECTORIES) and not name.startswith(".")
                             and not name.lower().startswith(("private-history", "private_history", "history-private", "history_private")))
        for name in sorted(files):
            if not name.endswith(".md"):
                continue
            path = Path(directory) / name
            path.resolve(strict=True).relative_to(root)
            with path.open("rb") as stream:
                prefix = stream.read(8192)
                # A corpus may contain ordinary documentation during migration.
                # Only explicit C5 units enter this index; task packages are never read.
                if not prefix.startswith((b"---\n", b"---\r\n")) or not re.search(rb"(?m)^schema:\s*['\"]?mindie-graded-unit/1['\"]?\s*$", prefix):
                    continue
                raw = prefix + stream.read(MAX_UNIT_BYTES + 1 - len(prefix))
            if len(raw) > MAX_UNIT_BYTES:
                raise ValueError(f"graded unit exceeds {MAX_UNIT_BYTES} bytes: {path.relative_to(root)}")
            units.append(GradedUnit.from_text(raw.decode("utf-8"), path.relative_to(root).as_posix()))
    return units


def _ref_parts(value):
    """Parse a stable ID or a mindie://<domain>/units/<id>[@<revision>] ref."""
    if not isinstance(value, str):
        return None
    if UNIT_ID.fullmatch(value):
        return None, value, None
    match = re.fullmatch(r"([^@\s]+)@([1-9][0-9]*)", value)
    if match and UNIT_ID.fullmatch(match.group(1)):
        return None, match.group(1), int(match.group(2))
    match = re.fullmatch(r"mindie://([^/\s]+)/units/([^@\s]+)(?:@([1-9][0-9]*))?", value)
    if not match:
        return None
    domain, ident, revision = match.groups()
    if not UNIT_ID.fullmatch(ident):
        return None
    return domain, ident, int(revision) if revision else None


class RoutingIndex:
    def __init__(self, units, *, counts=None):
        started = time.perf_counter()
        self.units = {}
        paths = set()
        for unit in units:
            if unit.id in self.units:
                raise ValueError(f"duplicate stable unit ID: {unit.id}")
            path = unit.path or f"{unit.metadata['domain_path']}/{unit.id}.md"
            if path in paths:
                raise ValueError(f"two units occupy the same path: {path}")
            paths.add(path)
            self.units[unit.id] = unit
        self.closure = {}
        visiting = []

        def visit(ident):
            if ident in self.closure:
                return self.closure[ident]
            if ident in visiting:
                raise ValueError("inheritance cycle: " + " -> ".join([*visiting, ident]))
            if ident not in self.units:
                raise ValueError(f"missing inherited unit: {ident}")
            visiting.append(ident)
            ancestors = []
            for parent in self.units[ident].metadata["inherits_from"]:
                for member in [*visit(parent), parent]:
                    if member not in ancestors:
                        ancestors.append(member)
            visiting.pop()
            self.closure[ident] = ancestors
            return ancestors

        for ident in self.units:
            visit(ident)
        if counts is not None and set(counts) != set(self.units):
            raise ValueError("precomputed counts do not describe the current units")
        self.counts = {}
        self.header_terms = {}
        self.frequencies = Counter()
        self.condition_keys = {}
        self.conditions = {}
        for ident, unit in self.units.items():
            key = canonical(unit.metadata["conditions"])
            self.condition_keys[ident] = key
            self.conditions[key] = unit.metadata["conditions"]
            triggers = unit.metadata["triggers"]
            header_text = "\n".join([unit.metadata["title"], *triggers["error_signatures"], *triggers["keywords"]])
            self.header_terms[ident] = frozenset(tokens(header_text))
            if counts is None:
                count = token_counts(header_text + "\n" + unit.body)
            else:
                count = Counter(counts[ident])
            self.counts[ident] = count
            self.frequencies.update(count.keys())
        self.lengths = {ident: sum(count.values()) for ident, count in self.counts.items()}
        self.average = sum(self.lengths.values()) / max(1, len(self.units)) or 1
        self.generation = hashlib.sha256(canonical([
            (ident, unit.revision, unit.path, unit.metadata["domain_path"])
            for ident, unit in sorted(self.units.items())
        ]).encode()).hexdigest()
        self.build_elapsed_ms = (time.perf_counter() - started) * 1000

    @classmethod
    def from_root(cls, root):
        started = time.perf_counter()
        index = cls(read_units(Path(root)))
        index.build_elapsed_ms = (time.perf_counter() - started) * 1000
        return index

    @classmethod
    def from_directory(cls, root):
        """Compatibility alias for callers that build from a corpus directory."""
        return cls.from_root(root)

    def manifest(self):
        rows = []
        for ident, unit in sorted(self.units.items()):
            metadata = unit.metadata
            rows.append({
                **{key: metadata[key] for key in ("id", "tier", "evidence_strength", "category", "domain_path", "title", "triggers", "conditions", "inherits_from")},
                "inherits_from_closure": self.closure[ident],
                "path": unit.path or f"{metadata['domain_path']}/{ident}.md",
                "revision": unit.revision,
                "token_estimate": token_estimate(unit.body),
                "provenance": metadata["provenance"],
            })
        return {"schema_version": "mindie-routing-manifest/1", "generation": self.generation,
                "units": rows, "unit_count": len(rows), "build_elapsed_ms": self.build_elapsed_ms,
                "token_estimator": "utf8_bytes_div_4_ceiling", "query_budget_excludes_build": True}

    def query(self, query, *, conditions=None, domain_path=None, limit=5, token_budget=1200,
              include_unknown=False, include_archived=False, navigation_only=False,
              mode="reuse", ledger_entries=None, bottleneck_summary=None,
              native_subcontext=False, goal=""):
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be nonempty text")
        if mode not in {"reuse", "diagnose", "explore"}:
            raise ValueError("mode must be one of ['diagnose', 'explore', 'reuse']")
        if conditions is not None and not isinstance(conditions, dict):
            raise ValueError("conditions must be an object of observed facts")
        if type(token_budget) is not int or not 1 <= token_budget <= 16000:
            raise ValueError("token_budget must be between 1 and 16000")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        started = time.perf_counter()
        terms = set(query_tokens(query))
        # Shared premises are evaluated once, while each child and inherited
        # rule still selects its own condition group independently.
        grouped = {key: match_conditions(required, conditions) for key, required in self.conditions.items()}
        applicability = {ident: grouped[key] for ident, key in self.condition_keys.items()}
        scores = {}
        rejected = Counter()
        archived_excluded = 0
        suspect_excluded = 0
        for ident, unit in self.units.items():
            # Archived, superseded and suspect units remain addressable by
            # reference but do not enter ordinary discovery. Suspect is a
            # demotion state: evidence is no longer sufficient for adoption,
            # while the body is retained for audit and recovery.
            if unit.status in {"archived", "superseded"} and not include_archived:
                archived_excluded += 1
                continue
            if unit.status == "suspect" and not include_archived:
                suspect_excluded += 1
                continue
            status = applicability[ident].status
            if status == "mismatch" or (status == "unknown" and not include_unknown):
                rejected[status] += 1
                continue
            score = 0.0
            count = self.counts[ident]
            headers = self.header_terms[ident]
            for term in terms:
                frequency = count[term]
                if frequency:
                    inverse = math.log(1 + (len(self.units) - self.frequencies[term] + .5) / (self.frequencies[term] + .5))
                    weight = 1.25 if term in headers else 1.0
                    score += weight * inverse * frequency * 2.2 / (frequency + 1.2 * (.25 + .75 * self.lengths[ident] / self.average))
            scores[ident] = score
        # A matched L1 premise in _common (or the requested domain) is part of
        # the task foundation even without lexical overlap. Mount each one once,
        # unless a higher-scoring matched unit already injects it as a parent.
        inherited_by_higher = set()
        for ident, score in scores.items():
            if score > 0 and applicability[ident].status == "matched":
                inherited_by_higher.update(self.closure[ident])
        ranked = []
        for ident, score in scores.items():
            path = self.units[ident].metadata["domain_path"]
            preferred = bool(domain_path and (path == domain_path or path.startswith(domain_path + "/")))
            tier = self.units[ident].metadata["tier"]
            tier_weight = 0.55 if (mode == "reuse" and tier == "L4") else 1.0
            if score:
                ranked.append((score * (1.1 if preferred else 1) * tier_weight, ident,
                               "preferred_domain" if preferred else "cross_directory"))
            elif (conditions is not None and tier == "L1"
                  and (path == "_common" or path.startswith("_common/") or preferred)
                  and applicability[ident].status == "matched" and ident not in inherited_by_higher):
                ranked.append((FOUNDATION_SCORE, ident, "foundation_baseline"))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        selected = ranked[:limit]
        source_selected = list(selected)
        stripped_l3_ids = []
        if mode == "explore":
            promoted = {}
            for _score, ident, _route in source_selected:
                unit = self.units[ident]
                if unit.metadata["tier"] != "L3":
                    continue
                stripped_l3_ids.append(ident)
                if applicability[ident].status != "matched":
                    continue
                for parent in self.closure[ident]:
                    parent_unit = self.units[parent]
                    if (parent_unit.metadata["tier"] in {"L1", "L2"}
                            and applicability[parent].status == "matched"):
                        promoted[parent] = scores.get(parent, FOUNDATION_SCORE)
            non_l3 = [
                (
                    row[0] * (1.35 if self.units[row[1]].metadata["tier"] == "L4" else 1.0),
                    row[1],
                    row[2],
                )
                for row in ranked
                if self.units[row[1]].metadata["tier"] != "L3"
            ]
            non_l3.sort(key=lambda item: (-item[0], item[1]))
            selected = non_l3[:limit]
            existing = {ident for _score, ident, _route in selected}
            for parent, score in sorted(promoted.items(), key=lambda item: (-item[1], item[0])):
                if parent not in existing:
                    selected.append((score, parent, "promoted_l3_foundation"))
                    existing.add(parent)
            stripped_l3_ids = sorted(set(stripped_l3_ids))
        active_closure = {
            ident: all(self.units[node].status == "active"
                       for node in [*self.closure[ident], ident])
            for ident in self.units
        }
        matched_closure = {
            ident: applicability[ident].status == "matched"
            and all(applicability[parent].status == "matched"
                    for parent in self.closure[ident])
            for ident in self.units
        }

        def missing_for(ident):
            missing = [reason.split(":", 1)[0] for reason in applicability[ident].reasons
                       if reason.endswith(("not supplied", "version not supplied"))]
            for parent in self.closure[ident]:
                status = applicability[parent]
                if status.status != "matched":
                    inherited = [reason.split(":", 1)[0] for reason in status.reasons
                                 if reason.endswith(("not supplied", "version not supplied"))]
                    missing.extend(inherited)
                    if not inherited:
                        missing.append(f"parent_unmatched:{parent}:{status.status}")
                if self.units[parent].status != "active":
                    missing.append(f"parent_inactive:{parent}:{self.units[parent].status}")
            deduplicated = []
            for reason in missing:
                if reason not in deduplicated:
                    deduplicated.append(reason)
            return deduplicated

        def card_for(ident, *, score, route, ready, missing):
            unit = self.units[ident]
            card = {"id": ident, "revision": unit.revision,
                    "title": unit.metadata["title"], "tier": unit.metadata["tier"],
                    "category": unit.metadata["category"],
                    "evidence_strength": unit.metadata["evidence_strength"],
                    "status": unit.status, "superseded_by": unit.superseded_by,
                    "domain_path": unit.metadata["domain_path"],
                    "match_status": {"matched": "matched", "mismatch": "mismatched",
                                     "unknown": "condition_unknown"}[applicability[ident].status],
                    "ready_to_apply": ready,
                    "conditions_matched": applicability[ident].status == "matched",
                    "missing_conditions": missing,
                    "inherits_from": list(unit.metadata["inherits_from"]),
                    "body_token_estimate": token_estimate(unit.body),
                    "closure_token_estimate": token_estimate(unit.body) + sum(
                        token_estimate(self.units[parent].body) for parent in self.closure[ident]),
                    "route": route, "score": score}
            if missing:
                unit_conds = unit.metadata["conditions"]
                card["required_conditions"] = {k: unit_conds[k] for k in missing if k in unit_conds}
            return card

        def finish(payload):
            nonlocal stripped_l3_ids
            if not payload.get("cards") and not payload.get("results") and rejected.get("unknown", 0) > 0:
                if not include_unknown and token_budget >= 256:
                    unknown_ranked = []
                    for ident, unit in self.units.items():
                        if unit.status in {"archived", "superseded", "suspect"} and not include_archived:
                            continue
                        if applicability[ident].status != "unknown":
                            continue
                        if any(r.endswith("source applicability is unknown") for r in applicability[ident].reasons):
                            continue
                        score = 0.0
                        count = self.counts[ident]
                        headers = self.header_terms[ident]
                        for term in terms:
                            frequency = count[term]
                            if frequency:
                                inverse = math.log(1 + (len(self.units) - self.frequencies[term] + .5) / (self.frequencies[term] + .5))
                                weight = 1.25 if term in headers else 1.0
                                score += weight * inverse * frequency * 2.2 / (frequency + 1.2 * (.25 + .75 * self.lengths[ident] / self.average))
                        if score <= 0:
                            continue
                        path = unit.metadata["domain_path"]
                        preferred = bool(domain_path and (path == domain_path or path.startswith(domain_path + "/")))
                        tier = unit.metadata["tier"]
                        tier_weight = 0.55 if (mode == "reuse" and tier == "L4") else 1.0
                        unknown_ranked.append((score * (1.1 if preferred else 1) * tier_weight, ident,
                                               "preferred_domain" if preferred else "cross_directory"))
                    unknown_ranked.sort(key=lambda item: (-item[0], item[1]))
                    cap = min(limit, 5)
                    if mode == "explore":
                        for u_score, ident, u_route in unknown_ranked[:cap]:
                            if self.units[ident].metadata["tier"] == "L3":
                                stripped_l3_ids.append(ident)
                                source_selected.append((u_score, ident, u_route))
                        stripped_l3_ids = sorted(set(stripped_l3_ids))
                        unknown_ranked = [
                            (
                                row[0] * (1.35 if self.units[row[1]].metadata["tier"] == "L4" else 1.0),
                                row[1],
                                row[2],
                            )
                            for row in unknown_ranked
                            if self.units[row[1]].metadata["tier"] != "L3"
                        ]
                        unknown_ranked.sort(key=lambda item: (-item[0], item[1]))
                    unverified_cards = []
                    for u_score, ident, u_route in unknown_ranked[:cap]:
                        missing = missing_for(ident)
                        card = card_for(ident, score=u_score, route=u_route, ready=False, missing=missing)
                        unit_conds = self.units[ident].metadata["conditions"]
                        card["required_conditions"] = {k: unit_conds[k] for k in missing if k in unit_conds}
                        unverified_cards.append(card)
                    if unverified_cards:
                        payload["unverified_cards"] = unverified_cards
                payload["note"] = (
                    payload.get("note", "")
                    + " No units matched the supplied conditions alone; inspect unverified_cards "
                    "(or pass include_unknown=true) to check missing_conditions and required_conditions, "
                    "verify those facts, or call knowledge_explain(ref=...) without conditions to inspect a unit."
                ).strip()
            payload["mode"] = mode
            if mode != "explore":
                return payload
            rows, seen = [], set()
            for _score, ident, _route in source_selected:
                if ident in seen:
                    continue
                seen.add(ident)
                unit = self.units[ident]
                row = {**unit.metadata, "body": unit.body}
                if unit.metadata["tier"] == "L3":
                    row["inherited_rules"] = [
                        {**self.units[parent].metadata, "body": self.units[parent].body}
                        for parent in self.closure[ident]
                        if self.units[parent].metadata["tier"] in {"L1", "L2"}]
                rows.append(row)
            for key in ("cards", "foundation_cards", "results", "unverified_cards"):
                for item in payload.get(key, ()):
                    if item["id"] in seen:
                        continue
                    seen.add(item["id"])
                    unit = self.units[item["id"]]
                    rows.append({**unit.metadata, "body": unit.body})
            from .explore import assess_stagnation, build_exploration_context
            norm_bottleneck = (
                {"summary": bottleneck_summary}
                if isinstance(bottleneck_summary, str)
                else (bottleneck_summary or {})
            )
            context = build_exploration_context(
                rows, bottleneck_summary=norm_bottleneck,
                failed_attempts=ledger_entries or [], native_subcontext=native_subcontext,
                goal=goal, conditions=conditions)
            context["stagnation"] = assess_stagnation(ledger_entries or [])
            context["stripped_l3_ids"] = stripped_l3_ids
            context["subagent_fact_pack"]["stripped_l3_ids"] = stripped_l3_ids
            payload["exploration_context"] = context
            payload["stripped_l3_ids"] = stripped_l3_ids
            return payload

        if navigation_only:
            cards = [card_for(ident, score=score, route=route,
                              ready=(matched_closure[ident] and active_closure[ident]),
                              missing=missing_for(ident))
                     for score, ident, route in selected]

            def foundation_cards_for(rows):
                found, seen = [], set()

                def add(ident, *, route="inherited_foundation"):
                    unit = self.units[ident]
                    if ident in seen or unit.metadata["tier"] != "L1" or unit.status != "active":
                        return
                    seen.add(ident)
                    found.append(card_for(
                        ident, score=scores.get(ident, FOUNDATION_SCORE), route=route,
                        ready=(matched_closure[ident] and active_closure[ident]),
                        missing=missing_for(ident)))

                for row in rows:
                    if row["tier"] == "L1" or row["route"] == "foundation_baseline":
                        add(row["id"], route=row["route"])
                    for parent in self.closure[row["id"]]:
                        add(parent)
                return found

            # Navigation cards do not charge bodies. Their own bounded metadata
            # is the delivered budget, so a truncated excerpt can never appear.
            accepted = []
            excluded_budget = []
            for card in cards:
                trial = accepted + [card]
                foundations = foundation_cards_for(trial)
                payload = {"cards": trial, "foundation_cards": foundations, "results": []}
                if response_token_estimate(payload) <= token_budget:
                    accepted = trial
                else:
                    excluded_budget.append(card["id"])
            cards = accepted
            foundation_cards = foundation_cards_for(cards)
            excluded_conditions = dict(rejected)
            if archived_excluded:
                excluded_conditions["archived_or_superseded"] = archived_excluded
            estimated = response_token_estimate(
                {"cards": cards, "foundation_cards": foundation_cards, "results": []})
            usage = {"token_budget": token_budget, "body_token_estimate": 0,
                     "estimated_tokens": estimated, "estimator": "utf8_bytes_div_4_ceiling",
                     "has_more": len(cards) < len(selected), "excluded_budget": list(excluded_budget)}
            payload = {"results": [], "cards": cards, "foundation_cards": foundation_cards,
                       "generation": self.generation, "unit_count": len(self.units),
                       "body_token_estimate": 0, "token_budget": token_budget,
                       "budget_scope": "navigation cards; metadata excluded",
                       "excluded_budget": excluded_budget, "excluded_conditions": excluded_conditions,
                       "usage": usage, "query_elapsed_ms": (time.perf_counter() - started) * 1000,
                       "note": "Conditions must be established. Unknown conditions are not evidence of applicability; retrieval scores are not confidence."}
            return finish(payload)

        left = token_budget
        results = []
        excluded_budget = []
        charged_parents: set[str] = set()
        for position, (score, ident, route) in enumerate(selected):
            if ident in charged_parents:
                continue
            if left < 1:
                break
            unit = self.units[ident]
            if unit.metadata["tier"] == "L1" and token_estimate(unit.body) > left:
                excluded_budget.append(ident)
                continue
            inherited, omitted = [], []

            def include_parent(parent_id, inherited, omitted, *, reserve_tokens=0):
                nonlocal left
                parent = self.units[parent_id]
                status = applicability[parent_id]
                if status.status != "matched":
                    omitted.append({"id": parent_id, **status.to_dict()})
                    return
                # A parent already delivered by an earlier result is referenced,
                # not charged or expanded a second time in the same response.
                if parent_id in charged_parents:
                    inherited.append({"id": parent_id, "revision": parent.revision,
                                      "tier": parent.metadata["tier"],
                                      "evidence_strength": parent.metadata["evidence_strength"],
                                      "conditions": parent.metadata["conditions"],
                                      "provenance": parent.metadata["provenance"],
                                      "applicability": status.to_dict(), "deduplicated": True})
                    return
                # An inherited rule is injected only as a complete condition-bound
                # rule. A budget miss is visible, never silent truncation of a premise.
                cost = token_estimate(parent.body)
                if cost > left - reserve_tokens:
                    omitted.append({"id": parent_id, "status": "budget_excluded"})
                    return
                left -= cost
                charged_parents.add(parent_id)
                inherited.append({"id": parent_id, "revision": parent.revision,
                                  "content": parent.body, "conditions": parent.metadata["conditions"],
                                  "tier": parent.metadata["tier"], "evidence_strength": parent.metadata["evidence_strength"],
                                  "provenance": parent.metadata["provenance"], "applicability": status.to_dict()})

            # A long workflow excerpt must not displace the hardware or numerical
            # premises on which that workflow depends.
            reserve = token_estimate(unit.body) if unit.metadata["tier"] == "L1" else 1
            for parent_id in self.closure[ident]:
                if self.units[parent_id].metadata["tier"] == "L1":
                    include_parent(parent_id, inherited, omitted, reserve_tokens=reserve)
            if unit.metadata["tier"] == "L1":
                content, used, truncated = unit.body, token_estimate(unit.body), False
            else:
                share = max(1, left // (len(selected) - position))
                content, used, truncated = _excerpt(unit.body, share)
            left -= used
            for parent_id in self.closure[ident]:
                if self.units[parent_id].metadata["tier"] != "L1":
                    include_parent(parent_id, inherited, omitted)
            results.append({"id": ident, "title": unit.metadata["title"],
                            "revision": unit.revision,
                            "match_status": {"matched": "matched", "mismatch": "mismatched", "unknown": "condition_unknown"}[applicability[ident].status],
                            "missing_conditions": missing_for(ident),
                            "tier": unit.metadata["tier"], "category": unit.metadata["category"],
                            "evidence_strength": unit.metadata["evidence_strength"],
                            "domain_path": unit.metadata["domain_path"], "conditions": unit.metadata["conditions"],
                            "provenance": unit.metadata["provenance"], "applicability": applicability[ident].to_dict(),
                            "status": unit.status, "superseded_by": unit.superseded_by,
                            "ready_to_apply": (applicability[ident].status == "matched" and not omitted
                                               and not truncated and active_closure[ident]),
                            "excerpt": content, "excerpt_truncated": truncated,
                            "score": score, "route": route,
                            "inherited_rules": inherited, "inheritance_excluded": omitted})
        cards = []
        for row in results:
            ident = row["id"]
            cards.append(card_for(
                ident, score=row["score"], route=row["route"],
                ready=(applicability[ident].status == "matched" and not row["inheritance_excluded"]
                       and active_closure[ident]),
                missing=row["missing_conditions"]))
        foundation_cards = [card for card in cards
                            if card["tier"] == "L1" or card["route"] == "foundation_baseline"]
        seen_foundation = {card["id"] for card in foundation_cards}
        for parent_id in sorted(charged_parents):
            parent = self.units[parent_id]
            if parent.metadata["tier"] != "L1" or parent_id in seen_foundation:
                continue
            foundation_cards.append(card_for(
                parent_id, score=scores.get(parent_id, FOUNDATION_SCORE),
                route="inherited_foundation",
                ready=(parent.status == "active" and active_closure[parent_id]),
                missing=[]))
        excluded_conditions = dict(rejected)
        if archived_excluded:
            excluded_conditions["archived_or_superseded"] = archived_excluded
        if suspect_excluded:
            excluded_conditions["suspect"] = suspect_excluded
        usage = {"token_budget": token_budget, "body_token_estimate": token_budget - left,
                 "estimated_tokens": response_token_estimate({"cards": cards, "results": results}),
                 "estimator": "utf8_bytes_div_4_ceiling", "has_more": bool(excluded_budget),
                 "excluded_budget": list(excluded_budget)}
        payload = {"results": results, "cards": cards, "foundation_cards": foundation_cards,
                   "generation": self.generation, "unit_count": len(self.units),
                   "body_token_estimate": token_budget - left, "token_budget": token_budget,
                   "budget_scope": "body excerpts and inherited rules; metadata excluded",
                   "excluded_budget": excluded_budget,
                   "excluded_conditions": excluded_conditions, "usage": usage,
                   "query_elapsed_ms": (time.perf_counter() - started) * 1000,
                   "note": "Conditions must be established. Unknown conditions are not evidence of applicability; retrieval scores are not confidence."}
        return finish(payload)

    def explain_closure(self, refs, *, conditions=None, known_refs=None, domain=None,
                        token_budget=4096, mode="reuse", ledger_entries=None,
                        bottleneck_summary=None, native_subcontext=False, goal=""):
        """Delegate exact closure assembly so this module stays focused on query."""
        from .closure import explain_closure
        return explain_closure(
            self, refs, conditions=conditions, known_refs=known_refs, domain=domain,
            token_budget=token_budget, mode=mode, ledger_entries=ledger_entries,
            bottleneck_summary=bottleneck_summary, native_subcontext=native_subcontext,
            goal=goal,
        )
