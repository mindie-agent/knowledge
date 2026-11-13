"""Multi-parent crossover DAG lineage and Leave-One-Out parent ablation reuse.

[Role]
  Extend the RSI experiment ledger and loop with ``parent_exp_ids`` multi-parent
  crossover tracking, orthogonal sibling candidate discovery, and automatic
  Gate-3 Leave-One-Out ablation verification from already-measured ledger
  controls on a shared ancestor baseline.

[Invariants]
  * Cumulative speedup ALWAYS remains grounded in the single measured
    ``baseline_exp_id`` / ``baseline_source_digest`` chain — unmeasured sibling
    speedups are never multiplied into committed ledger ratios.
  * Multi-parent candidates require verified ablation evidence independently of
    whether ``factors`` was supplied.
  * Parent-reused Gate-3 ablation passes (``ablation_retained=True``) iff all
    ``N - 1`` Leave-One-Out controls (and any declared candidate factors) are
    verified on shared ancestor ``Bk`` and ``effective_vs_bk / best_control > 1 + margin``.

[Timeouts & Locks]
  Pure in-memory $O(N^2)$ scan over folded live ledger records; no I/O or locks.

[Side-Effect Policy]
  Pure functional helpers; callers commit through ``experiment_ledger.append``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ABLATION_EVIDENCE_KEYS = (
    "ablation_retained",
    "ablation_ok",
    "ablation_passed",
    "ablation",
    "ablation_receipt",
)


def normalize_parent_exp_ids(
    raw_parents: Any,
    *,
    exp_id: str | None = None,
    baseline_exp_id: str | None = None,
) -> list[str] | None:
    """Validate and deduplicate constituent ``parent_exp_ids``.

    When ``raw_parents`` already names two or more constituent parents (e.g.
    ``["A", "B"]`` measured against shared anchor ``Bk``), ``Bk`` is NOT mixed
    into ``parent_exp_ids`` unless ``Bk`` is itself one of those parents.
    When ``raw_parents`` names a single sibling on top of an incremental parent
    baseline ``A``, ``A`` is prepended to form ``["A", "B"]``.
    """

    if raw_parents is None:
        return None
    if not isinstance(raw_parents, list) or not raw_parents:
        raise ValueError("parent_exp_ids must be a nonempty array of exp_id strings")
    cleaned: list[str] = []
    own = (exp_id or "").strip()
    for item in raw_parents:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("parent_exp_ids entries must be nonempty strings")
        val = item.strip()
        if own and val == own:
            raise ValueError(f"parent_exp_ids cannot reference own exp_id {own!r}")
        if val not in cleaned:
            cleaned.append(val)
    base = (baseline_exp_id or "").strip()
    if base and base != own:
        if base in cleaned:
            cleaned = [base] + [item for item in cleaned if item != base]
        elif len(cleaned) == 1:
            cleaned.insert(0, base)
    return cleaned


def default_ablation_retained(evidence: Mapping[str, Any]) -> bool | None:
    """Return ``None`` (pending) whenever multi-factor or multi-parent ablation is required."""

    factors = evidence.get("factors")
    if isinstance(factors, list) and len(factors) > 1:
        return None
    raw_parents = evidence.get("parent_exp_ids")
    if raw_parents is not None:
        own_id = str(evidence.get("exp_id") or evidence.get("candidate_id") or "").strip() or None
        base_id = str(evidence.get("baseline_exp_id") or "").strip() or None
        parents = normalize_parent_exp_ids(raw_parents, exp_id=own_id, baseline_exp_id=base_id)
        if parents and len(parents) > 1:
            return None
    return True


def _positive_ratio(rec: Mapping[str, Any]) -> float | None:
    raw = rec.get("speedup_ratio")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    val = float(raw)
    return val if math.isfinite(val) and val > 0.0 else None


def _cond_key(conds: Any) -> str:
    return json.dumps(conds or {}, sort_keys=True)


def _factors_set(rec: Mapping[str, Any]) -> set[str]:
    raw = rec.get("factors")
    if not isinstance(raw, list):
        return set()
    return {str(f).strip() for f in raw if isinstance(f, str) and str(f).strip()}


def _canon_id(rec: Mapping[str, Any]) -> str:
    return str(rec.get("exp_id") or rec.get("candidate_id") or "").strip()


def _build_kept_lookup(
    live: Sequence[Mapping[str, Any]], *, cond_filter: str | None = None, same_source_only: bool = True,
) -> dict[str, Mapping[str, Any]]:
    """Index kept records by ``exp_id`` and unambiguous task-global aliases."""
    reserved_ids = {_canon_id(rec) for rec in live if _canon_id(rec)}
    global_non_kept_ids: set[str] = set()
    all_alias_owners: dict[str, set[int]] = {}
    for rec in live:
        cid = _canon_id(rec)
        is_kept = rec.get("verdict") in {"supported", "subtarget_valid"}
        if not is_kept and cid:
            global_non_kept_ids.add(cid)
        for raw_alias in rec.get("_superseded_ids") or ():
            alias = str(raw_alias or "").strip()
            if alias and alias != cid:
                all_alias_owners.setdefault(alias, set()).add(id(rec))
                if not is_kept and alias not in reserved_ids:
                    global_non_kept_ids.add(alias)

    scoped = [rec for rec in live if _cond_key(rec.get("conditions")) == cond_filter] if cond_filter is not None else list(live)
    exact: dict[str, Mapping[str, Any]] = {}
    kept_alias_hits: dict[str, list[Mapping[str, Any]]] = {}
    for rec in scoped:
        if rec.get("verdict") not in {"supported", "subtarget_valid"}:
            continue
        cid = _canon_id(rec)
        if cid and cid not in global_non_kept_ids:
            exact[cid] = rec
        raw_aliases = (rec.get("_same_source_aliases") if "_same_source_aliases" in rec else rec.get("_superseded_ids")) if same_source_only else rec.get("_superseded_ids")
        for raw_alias in raw_aliases or ():
            alias = str(raw_alias or "").strip()
            if alias and alias != cid:
                kept_alias_hits.setdefault(alias, []).append(rec)

    merged = dict(exact)
    for alias, owners in kept_alias_hits.items():
        if alias not in reserved_ids and alias not in global_non_kept_ids and alias not in merged and len(owners) == 1 and len(all_alias_owners.get(alias, ())) == 1:
            merged[alias] = owners[0]
    return merged


def snapshot_parent_source_digests(
    live: Sequence[Mapping[str, Any]], parent_exp_ids: Sequence[str] | None, *,
    conditions: Mapping[str, Any] | None = None, explicit: Any = None,
) -> dict[str, str] | None:
    """Capture constituent parent ``source_digest`` revisions at proposal time."""
    if explicit is not None and (not isinstance(explicit, Mapping) or any(not isinstance(k, str) or not k.strip() or not isinstance(v, str) for k, v in explicit.items())):
        raise ValueError("parent_source_digests must map exp_id strings to source_digest strings")
    if not parent_exp_ids:
        return {str(k).strip(): str(v).strip() for k, v in explicit.items()} if isinstance(explicit, Mapping) else None
    by_id = _build_kept_lookup(live, cond_filter=_cond_key(conditions), same_source_only=True)
    exp_map = {str(k).strip(): str(v).strip() for k, v in explicit.items()} if isinstance(explicit, Mapping) else {}
    return {
        pid: exp_map[pid] if pid in exp_map else (str(by_id[pid].get("source_digest") or "").strip() if pid in by_id else "")
        for raw_p in parent_exp_ids if (pid := str(raw_p or "").strip())
    }


def bind_fold_lineage(live: dict[Any, dict[str, Any]], prior: Any, key: Any, record: dict[str, Any]) -> dict[Any, dict[str, Any]]:
    """Maintain supersession aliases and bind ``parent_exp_ids`` source revisions during ``fold``."""
    prior_rec = live.get(prior) if (prior is not None and prior in live) else None
    raw_parents = record.get("parent_exp_ids")
    if isinstance(raw_parents, list) and raw_parents:
        prior_src = str(prior_rec.get("source_digest") or "").strip() if prior_rec else ""
        curr_src = str(record.get("source_digest") or "").strip()
        if isinstance(record.get("parent_source_digests"), Mapping):
            record["_bound_parent_digests"] = {str(k).strip(): str(v or "").strip() for k, v in record["parent_source_digests"].items()}
        elif prior_rec is not None and prior_src and prior_src == curr_src and prior_rec.get("parent_exp_ids") == raw_parents and isinstance(prior_rec.get("_bound_parent_digests"), Mapping):
            record["_bound_parent_digests"] = dict(prior_rec["_bound_parent_digests"])
        elif prior_rec is None and isinstance(record.get("_bound_parent_digests"), Mapping):
            record["_bound_parent_digests"] = dict(record["_bound_parent_digests"])
        else:
            kept_now = _build_kept_lookup(list(live.values()), cond_filter=_cond_key(record.get("conditions")), same_source_only=True)
            record["_bound_parent_digests"] = {
                pid: (str(kept_now[pid].get("source_digest") or "").strip() if pid in kept_now else "")
                for raw_p in raw_parents if (pid := str(raw_p or "").strip())
            }
    else:
        record.pop("_bound_parent_digests", None)

    if prior_rec is not None:
        aliases = [a for a in (*(prior_rec.get("_superseded_ids") or ()), *(record.get("_superseded_ids") or ())) if a != key]
        aliases = list(dict.fromkeys(aliases))
        if prior != key and prior not in aliases:
            aliases.append(prior)
        if aliases:
            record["_superseded_ids"] = aliases
        else:
            record.pop("_superseded_ids", None)
        p_src, c_src = str(prior_rec.get("source_digest") or "").strip(), str(record.get("source_digest") or "").strip()
        if p_src and c_src and p_src == c_src:
            same = list(dict.fromkeys(a for a in (*(prior_rec.get("_same_source_aliases") or ()), *(record.get("_same_source_aliases") or ())) if a != key))
            if prior != key and prior not in same:
                same.append(prior)
            record["_same_source_aliases"] = same
        else:
            record["_same_source_aliases"] = []
        return ({**live, key: record} if key == prior
                else {(key if k == prior else k): (record if k == prior else v) for k, v in live.items() if k != key})

    if "_superseded_ids" in record:
        kept_sup = [a for a in (record.get("_superseded_ids") or ()) if a != key]
        if kept_sup:
            record["_superseded_ids"] = kept_sup
        else:
            record.pop("_superseded_ids", None)
    if "_same_source_aliases" in record:
        record["_same_source_aliases"] = [a for a in (record.get("_same_source_aliases") or ()) if a != key]
    live[key] = record
    return live


def _resolve_bound_parent_canon_set(
    rec: Mapping[str, Any],
    lookup: Mapping[str, Mapping[str, Any]],
) -> set[str] | None:
    """Resolve ``rec['parent_exp_ids']`` to canonical IDs iff bound source digests match."""

    raw_sub = rec.get("parent_exp_ids")
    if not isinstance(raw_sub, list) or not raw_sub:
        return None
    bound_map = rec.get("_bound_parent_digests")
    explicit_map = rec.get("parent_source_digests")
    resolved: set[str] = set()
    for item in raw_sub:
        key = str(item or "").strip()
        if not key or key not in lookup:
            return None
        p_rec = lookup[key]
        p_src = str(p_rec.get("source_digest") or "").strip()
        if isinstance(bound_map, Mapping):
            if not p_src or bound_map.get(key) != p_src:
                return None
        if isinstance(explicit_map, Mapping):
            exp_src = explicit_map.get(key, explicit_map.get(_canon_id(p_rec)))
            if not p_src or exp_src != p_src:
                return None
        resolved.add(_canon_id(p_rec))
    return resolved


class _CondLineage:
    """Per-condition-scope baseline and alias index with task-global primary ID reservation."""

    def __init__(self, live: Sequence[Mapping[str, Any]], cond: str) -> None:
        cond_records = [rec for rec in live if _cond_key(rec.get("conditions")) == cond]
        self.by_id = _build_kept_lookup(live, cond_filter=cond, same_source_only=True)
        global_primary_ids = {_canon_id(rec) for rec in live if _canon_id(rec)}
        self.kept_by_digest: dict[str, list[Mapping[str, Any]]] = {}
        self.by_exp_list: dict[str, list[Mapping[str, Any]]] = {}
        self.non_kept_ids: set[str] = {
            _canon_id(rec)
            for rec in live
            if _canon_id(rec) and rec.get("verdict") not in {"supported", "subtarget_valid"}
        }
        self.non_kept_digests: set[str] = set()
        seen_objs: set[int] = set()

        for rec in live:
            if rec.get("verdict") not in {"supported", "subtarget_valid"}:
                for old in rec.get("_superseded_ids") or ():
                    if isinstance(old, str) and old.strip() and old.strip() not in global_primary_ids:
                        self.non_kept_ids.add(old.strip())

        for rec in cond_records:
            cid = _canon_id(rec)
            src = str(rec.get("source_digest") or "").strip()
            alias_ids = ([cid] if cid else []) + [
                str(old).strip() for old in (rec.get("_superseded_ids") or ())
                if isinstance(old, str) and old.strip() and old.strip() not in global_primary_ids
            ]
            if rec.get("verdict") in {"supported", "subtarget_valid"}:
                if src and id(rec) not in seen_objs:
                    seen_objs.add(id(rec))
                    self.kept_by_digest.setdefault(src, []).append(rec)
                for eid in alias_ids:
                    if eid not in self.non_kept_ids:
                        self.by_exp_list.setdefault(eid, []).append(rec)
            else:
                if src:
                    self.non_kept_digests.add(src)
                self.non_kept_ids.update(alias_ids)

    def resolve_parent_step(
        self, rec: Mapping[str, Any]
    ) -> tuple[str, Mapping[str, Any] | None]:
        cid = _canon_id(rec)
        own_ids = {
            cid,
            *(str(a).strip() for a in (rec.get("_superseded_ids") or ()) if str(a).strip()),
        }
        cand_src = str(rec.get("source_digest") or "").strip()
        b_exp = str(rec.get("baseline_exp_id") or "").strip()
        b_src = str(rec.get("baseline_source_digest") or "").strip()
        if (b_exp and b_exp in own_ids) or (b_src and b_src == cand_src):
            return "invalid", None
        if b_exp:
            if b_exp in self.non_kept_ids:
                return "invalid", None
            matches = [m for m in self.by_exp_list.get(b_exp, ()) if m is not rec]
            if len(matches) > 1:
                return "invalid", None
            if len(matches) == 1:
                p_src = str(matches[0].get("source_digest") or "").strip()
                if b_src and p_src and p_src != b_src:
                    return "invalid", None
                return "ok", matches[0]
            if b_src and (self.kept_by_digest.get(b_src) or b_src in self.non_kept_digests):
                return "invalid", None
            return "root", None
        if b_src:
            matches = [m for m in self.kept_by_digest.get(b_src, ()) if m is not rec]
            if len(matches) > 1:
                return "invalid", None
            if len(matches) == 1:
                return "ok", matches[0]
            if b_src in self.non_kept_digests:
                return "invalid", None
            return "root", None
        return "root", None

    def has_valid_lineage(
        self, rec: Mapping[str, Any], *, require_positive_step: bool = True
    ) -> bool:
        seen: set[int] = set()
        curr: Mapping[str, Any] | None = rec
        is_first = True
        while curr is not None:
            rid = id(curr)
            if rid in seen:
                return False
            if (require_positive_step or not is_first) and _positive_ratio(curr) is None:
                return False
            is_first = False
            seen.add(rid)
            kind, curr = self.resolve_parent_step(curr)
            if kind == "invalid":
                return False
            if kind == "root":
                return True
        return True

    def is_valid_baseline_ref(self, b_exp: str, b_src: str) -> bool:
        if not b_src:
            return False
        if b_exp:
            if b_exp in self.non_kept_ids:
                return False
            matches = self.by_exp_list.get(b_exp, [])
            if len(matches) > 1:
                return False
            if len(matches) == 1:
                p_src = str(matches[0].get("source_digest") or "").strip()
                return (not p_src or p_src == b_src) and self.has_valid_lineage(matches[0])
            return not self.kept_by_digest.get(b_src) and b_src not in self.non_kept_digests
        matches = self.kept_by_digest.get(b_src, [])
        if len(matches) > 1:
            return False
        if len(matches) == 1:
            return self.has_valid_lineage(matches[0])
        return b_src not in self.non_kept_digests


def resolve_kept_anchor(
    live: Sequence[Mapping[str, Any]], anchor_id: str
) -> Mapping[str, Any] | None:
    """Resolve a canonical or unambiguous ``_superseded_ids`` kept anchor with valid lineage."""

    target = anchor_id.strip()
    if not target:
        return None
    lookup = _build_kept_lookup(live)
    match = lookup.get(target)
    if match is None:
        return None
    lineage = _CondLineage(live, _cond_key(match.get("conditions")))
    if not lineage.has_valid_lineage(match, require_positive_step=False):
        return None
    return match


def suggest_orthogonal_crossovers(
    live: Sequence[Mapping[str, Any]],
    *,
    noise_margin: float = 0.01,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Identify orthogonal sibling ``supported`` / ``subtarget_valid`` pairs to combine."""

    lookup = _build_kept_lookup(live, same_source_only=True)
    lineages: dict[str, _CondLineage] = {}
    kept = [
        rec
        for rec in live
        if rec.get("verdict") in {"supported", "subtarget_valid"}
        and (_positive_ratio(rec) or 0.0) > 1.0 + max(0.0, float(noise_margin))
    ]
    recorded_sets: set[frozenset[str]] = set()
    for rec in live:
        parents = rec.get("parent_exp_ids")
        if isinstance(parents, list) and len(parents) >= 2:
            resolved = _resolve_bound_parent_canon_set(rec, lookup)
            if resolved is not None and len(resolved) >= 2:
                recorded_sets.add(frozenset(resolved))

    by_anchor: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for rec in kept:
        cond = _cond_key(rec.get("conditions"))
        lineage = lineages.setdefault(cond, _CondLineage(live, cond))
        if not lineage.has_valid_lineage(rec):
            continue
        base_digest = str(rec.get("baseline_source_digest") or "").strip()
        if not base_digest:
            continue
        kind, parent_rec = lineage.resolve_parent_step(rec)
        if kind == "invalid":
            continue
        canon_base_exp = (
            _canon_id(parent_rec)
            if kind == "ok" and parent_rec is not None
            else ""
        )
        by_anchor.setdefault((cond, base_digest, canon_base_exp), []).append(rec)

    suggestions: list[dict[str, Any]] = []
    for (_, base_digest, base_exp), group in by_anchor.items():
        if len(group) < 2:
            continue
        for idx_a in range(len(group)):
            for idx_b in range(idx_a + 1, len(group)):
                a, b = group[idx_a], group[idx_b]
                id_a, id_b = _canon_id(a), _canon_id(b)
                if not id_a or not id_b or id_a == id_b:
                    continue
                if frozenset({id_a, id_b}) in recorded_sets:
                    continue
                if str(a.get("source_digest") or "") == str(b.get("source_digest") or ""):
                    continue
                eff_base_exp = base_exp
                if not eff_base_exp:
                    raw_exps = {
                        e
                        for e in (
                            str(a.get("baseline_exp_id") or "").strip(),
                            str(b.get("baseline_exp_id") or "").strip(),
                        )
                        if e
                    }
                    if len(raw_exps) > 1:
                        continue
                    eff_base_exp = next(iter(raw_exps), "")

                fac_a, fac_b = _factors_set(a), _factors_set(b)
                if fac_a and fac_b and not fac_a.isdisjoint(fac_b):
                    continue
                lane_a = str(a.get("bottleneck_lane") or "").strip()
                lane_b = str(b.get("bottleneck_lane") or "").strip()
                if lane_a and lane_b and lane_a == lane_b:
                    continue

                ratio_a = _positive_ratio(a) or 1.0
                ratio_b = _positive_ratio(b) or 1.0
                est = round(ratio_a * ratio_b, 6)
                suggested_factors = (
                    sorted(fac_a | fac_b)
                    if len(fac_a) == 1 and len(fac_b) == 1 and fac_a != fac_b
                    else None
                )
                suggestions.append(
                    {
                        "parent_exp_ids": [id_a, id_b],
                        "shared_baseline_exp_id": eff_base_exp or None,
                        "shared_baseline_source_digest": base_digest,
                        "parent_verdicts": [a.get("verdict"), b.get("verdict")],
                        "parent_speedups": [round(ratio_a, 6), round(ratio_b, 6)],
                        "independent_estimate_ratio": est,
                        "suggested_factors": suggested_factors,
                        "parent_summaries": [
                            str(a.get("mutation_summary") or ""),
                            str(b.get("mutation_summary") or ""),
                        ],
                    }
                )

    suggestions.sort(
        key=lambda item: (
            -float(item["independent_estimate_ratio"]),
            item["parent_exp_ids"],
        )
    )
    return suggestions[: max(1, int(limit))]


def derive_crossover_ablation_receipt(
    live: Sequence[Mapping[str, Any]], *, parent_exp_ids: Sequence[str] | None,
    baseline_source_digest: str | None, conditions: Mapping[str, Any] | None,
    combined_speedup_ratio: float | None, noise_margin: float = 0.01,
    candidate_factors: Sequence[str] | None = None, baseline_exp_id: str | None = None,
    parent_source_digests: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Reuse verified Leave-One-Out ledger controls on shared baseline ``Bk``."""
    if not parent_exp_ids or len(parent_exp_ids) < 2:
        return None
    if combined_speedup_ratio is None or isinstance(combined_speedup_ratio, bool) or not math.isfinite(float(combined_speedup_ratio)) or float(combined_speedup_ratio) <= 0.0:
        return None

    margin, cond, base_src = max(0.0, float(noise_margin)), _cond_key(conditions), (baseline_source_digest or "").strip()
    if not base_src:
        return None

    lineage = _CondLineage(live, cond)
    by_id = lineage.by_id
    parents: list[Mapping[str, Any]] = []
    parent_by_canon: dict[str, Mapping[str, Any]] = {}
    seen_canon: set[str] = set()
    for pid in parent_exp_ids:
        key = str(pid).strip()
        found = by_id.get(key)
        if found is None or _positive_ratio(found) is None:
            return None
        cid = _canon_id(found)
        p_src = str(found.get("source_digest") or "").strip()
        if isinstance(parent_source_digests, Mapping):
            pinned = str(parent_source_digests.get(key, parent_source_digests.get(cid)) or "").strip()
            if not pinned or not p_src or pinned != p_src:
                return None
        if cid not in seen_canon:
            seen_canon.add(cid)
            parents.append(found)
            parent_by_canon[cid] = found
    if len(parents) < 2:
        return None

    anchor_digests = {str(p.get("baseline_source_digest") or "").strip() for p in parents}
    if len(anchor_digests) != 1 or "" in anchor_digests:
        return None
    shared_bk_digest = next(iter(anchor_digests))

    cand_facs: set[str] | None = None
    required_loo_sets: set[frozenset[str]] | None = None
    if candidate_factors is not None and len(candidate_factors) > 0:
        cand_facs = {
            str(f).strip()
            for f in candidate_factors
            if isinstance(f, str) and str(f).strip()
        }
        if len(cand_facs) < 2:
            return None
        parent_union = set().union(*(_factors_set(p) for p in parents))
        if not cand_facs.issubset(parent_union):
            return None
        required_loo_sets = {frozenset(cand_facs - {f}) for f in cand_facs}

    memo_vs_bk: dict[int, float | None] = {}

    def _resolve_rec_vs_bk(rec: Mapping[str, Any]) -> float | None:
        obj_id = id(rec)
        if obj_id in memo_vs_bk:
            return memo_vs_bk[obj_id]
        if not lineage.has_valid_lineage(rec):
            memo_vs_bk[obj_id] = None
            return None
        seen: set[int] = set()
        curr: Mapping[str, Any] | None = rec
        total = 1.0
        while curr is not None:
            if id(curr) in seen:
                memo_vs_bk[obj_id] = None
                return None
            seen.add(id(curr))
            step = _positive_ratio(curr)
            if step is None:
                memo_vs_bk[obj_id] = None
                return None
            total *= step
            b_src = str(curr.get("baseline_source_digest") or "").strip()
            kind, parent_rec = lineage.resolve_parent_step(curr)
            if kind == "invalid":
                memo_vs_bk[obj_id] = None
                return None
            if b_src == shared_bk_digest:
                memo_vs_bk[obj_id] = total
                return total
            if kind == "root" or parent_rec is None:
                memo_vs_bk[obj_id] = None
                return None
            curr = parent_rec
        memo_vs_bk[obj_id] = None
        return None

    selected_controls: dict[str, tuple[float, Mapping[str, Any]]] = {}
    unique_kept = list({id(r): r for r in by_id.values()}.values())
    if len(parents) == 2:
        for p in parents:
            p_vs_bk = _resolve_rec_vs_bk(p)
            if p_vs_bk is None:
                return None
            cid = _canon_id(p)
            selected_controls[cid] = (p_vs_bk, p)
    else:
        subset_candidates: dict[frozenset[str], list[tuple[float, str, Mapping[str, Any]]]] = {}
        for rec in unique_kept:
            raw_sub = rec.get("parent_exp_ids")
            if not isinstance(raw_sub, list) or len(raw_sub) != len(parents) - 1:
                continue
            resolved_sub = _resolve_bound_parent_canon_set(rec, by_id)
            if (
                resolved_sub is None
                or len(resolved_sub) != len(parents) - 1
                or not resolved_sub.issubset(seen_canon)
            ):
                continue
            rec_facs = _factors_set(rec)
            if required_loo_sets is not None and cand_facs is not None:
                if frozenset(rec_facs) not in required_loo_sets:
                    continue
                omitted_pid = next(iter(seen_canon - resolved_sub))
                omit_facs = _factors_set(parent_by_canon[omitted_pid])
                if omit_facs and rec_facs != (cand_facs - omit_facs):
                    continue
            else:
                sub_parent_facs = [_factors_set(parent_by_canon[pid]) for pid in resolved_sub]
                if (
                    rec_facs
                    and all(sub_parent_facs)
                    and rec_facs != set().union(*sub_parent_facs)
                ):
                    continue
            sub_vs_bk = _resolve_rec_vs_bk(rec)
            if sub_vs_bk is not None:
                subset_candidates.setdefault(frozenset(resolved_sub), []).append(
                    (sub_vs_bk, _canon_id(rec), rec)
                )
        for omit in sorted(seen_canon):
            needed = frozenset(seen_canon - {omit})
            cands = subset_candidates.get(needed)
            if not cands:
                return None
            best_ratio, best_cid, best_rec = max(
                cands, key=lambda item: (round(item[0], 12), item[1])
            )
            selected_controls[best_cid] = (best_ratio, best_rec)

    if cand_facs is not None:
        control_factor_sets = [_factors_set(ctrl) for _, ctrl in selected_controls.values()]
        if not all(any(cf == (cand_facs - {f}) for cf in control_factor_sets) for f in cand_facs):
            return None

    measured = float(combined_speedup_ratio)
    b_exp_clean = (baseline_exp_id or "").strip()
    if base_src == shared_bk_digest:
        if not lineage.is_valid_baseline_ref(b_exp_clean, base_src):
            return None
        effective_vs_bk = measured
    else:
        if b_exp_clean:
            base_rec = by_id.get(b_exp_clean)
            if base_rec is None or str(base_rec.get("source_digest") or "").strip() != base_src:
                return None
        else:
            if base_src in lineage.non_kept_digests:
                return None
            digest_hits = lineage.kept_by_digest.get(base_src, [])
            if len(digest_hits) != 1:
                return None
            base_rec = digest_hits[0]
        base_cid = _canon_id(base_rec)
        is_constituent_parent = (
            base_cid in seen_canon
            and str(parent_by_canon[base_cid].get("source_digest") or "").strip() == base_src
        )
        base_sub = _resolve_bound_parent_canon_set(base_rec, by_id)
        is_constituent_subset = (
            base_sub is not None
            and 1 <= len(base_sub) < len(seen_canon)
            and base_sub.issubset(seen_canon)
        )
        if not (is_constituent_parent or is_constituent_subset):
            return None
        base_ctrl_ratio = _resolve_rec_vs_bk(base_rec)
        if base_ctrl_ratio is None:
            return None
        if measured <= 1.0 + margin:
            return {
                "status": "candidate_for_sequential_pruning",
                "ablation_retained": False,
                "pruned_combination_verified": True,
                "correctness_verified": True,
                "performance_verified": True,
                "source": "crossover_incremental_parent_baseline",
                "parent_exp_ids": [_canon_id(p) for p in parents],
                "incremental_speedup_ratio": round(measured, 6),
                "reason": "incremental crossover gain over base control did not exceed noise_margin",
            }
        effective_vs_bk = base_ctrl_ratio * measured

    control_ratios = {cid: ratio for cid, (ratio, _) in selected_controls.items()}
    best_single_id, best_single_ratio = max(
        control_ratios.items(), key=lambda kv: (round(kv[1], 12), kv[0])
    )
    incremental_over_best = effective_vs_bk / best_single_ratio
    retained = incremental_over_best > (1.0 + margin)

    if retained:
        return {
            "status": "verified_from_orthogonal_parents",
            "ablation_retained": True,
            "pruned_combination_verified": False,
            "source": "crossover_parent_ledger_reuse",
            "shared_baseline_source_digest": shared_bk_digest,
            "parent_speedups_vs_bk": {k: round(v, 6) for k, v in control_ratios.items()},
            "crossover_speedup_vs_bk": round(effective_vs_bk, 6),
            "incremental_over_best_control": round(incremental_over_best, 6),
        }
    return {
        "status": "candidate_for_sequential_pruning",
        "ablation_retained": False,
        "pruned_combination_verified": True,
        "correctness_verified": True,
        "performance_verified": True,
        "dominant_single_parent": best_single_id,
        "shared_baseline_source_digest": shared_bk_digest,
        "parent_speedups_vs_bk": {k: round(v, 6) for k, v in control_ratios.items()},
        "crossover_speedup_vs_bk": round(effective_vs_bk, 6),
        "incremental_over_best_control": round(incremental_over_best, 6),
        "reason": (
            f"crossover ({effective_vs_bk:.4f}x) relative gain over control "
            f"{best_single_id} ({best_single_ratio:.4f}x) is {incremental_over_best:.4f}x "
            f"<= {1.0 + margin:.4f}x"
        ),
    }


def _matches_committed_evidence(existing: Mapping[str, Any], evidence: Mapping[str, Any], checked: Mapping[str, Any]) -> bool:
    norm_parents = normalize_parent_exp_ids(
        evidence.get("parent_exp_ids"),
        exp_id=str(evidence.get("exp_id") or "") or None,
        baseline_exp_id=str(existing.get("baseline_exp_id") or "") or None,
    )
    rnd = lambda key: round(float(checked[key]), 6) if checked.get(key) is not None else None  # noqa: E731
    return (
        evidence.get("source_digest") == existing.get("source_digest")
        and evidence.get("baseline_source_digest") == existing.get("baseline_source_digest")
        and dict(evidence.get("scope_conditions") or {}) == dict(existing.get("conditions") or {})
        and list(evidence.get("run_ids") or []) == list(existing.get("run_ids") or [])
        and evidence.get("factors") == existing.get("factors")
        and norm_parents == existing.get("parent_exp_ids")
        and (evidence.get("parent_source_digests") or None) == (existing.get("parent_source_digests") or None)
        and checked.get("paired_rounds") == existing.get("paired_rounds")
        and rnd("speedup_ratio") == existing.get("speedup_ratio")
        and rnd("baseline_metric") == existing.get("baseline_metric")
        and rnd("candidate_metric") == existing.get("candidate_metric")
    )


def resolve_measure_ablation_receipt(
    *, ledger_path: str | Path, task_id: str, iter_task_live: Callable[[str | Path, str], Sequence[Mapping[str, Any]]],
    candidate: Mapping[str, Any], base_info: Mapping[str, Any], body: Mapping[str, Any], evidence: Mapping[str, Any],
    checked: Mapping[str, Any], default_noise_margin: float, existing_record: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Preserve committed receipts on idempotent retries or derive LOO parent receipts."""
    if any(k in body for k in ABLATION_EVIDENCE_KEYS):
        return None
    if isinstance(existing_record, Mapping) and body.get("supersedes_exp_id") is None and _matches_committed_evidence(existing_record, evidence, checked):
        gates = existing_record.get("gates")
        gate3 = gates.get("gate3_ablation") if isinstance(gates, Mapping) else None
        committed = gate3.get("ablation_receipt") if isinstance(gate3, Mapping) else None
        return dict(committed) if isinstance(committed, Mapping) else None
    parents = candidate.get("parent_exp_ids")
    if not (isinstance(parents, list) and len(parents) >= 2 and Path(ledger_path).is_file()):
        return None
    return derive_crossover_ablation_receipt(
        iter_task_live(ledger_path, task_id), parent_exp_ids=parents,
        baseline_source_digest=base_info.get("source_digest"), baseline_exp_id=base_info.get("exp_id"),
        conditions=candidate.get("scope_conditions"), combined_speedup_ratio=checked.get("speedup_ratio"),
        noise_margin=float(evidence.get("noise_margin", default_noise_margin)), candidate_factors=candidate.get("factors"),
        parent_source_digests=candidate.get("parent_source_digests"),
    )
