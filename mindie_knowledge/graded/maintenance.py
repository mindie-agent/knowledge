"""[Role] Review and route the C5 graded corpus without rewriting its claims.

[Invariants] GradedUnit owns C5 parsing; RoutingIndex owns C8 closure. Mechanical
duplication requires identical body, conditions, tier/category and evidence
strength. Provenance variants remain distinct evidence. Trigger similarity is
review-only. A reviewed move updates only path metadata, retaining stable ID,
revision and semantic digest; there is no rollback journal or shadow state machine.
[Timeouts & Locks] In-memory analysis and atomic file replacement; no network,
model, subprocess, sleep or cross-process lock.
[Side-Effect Policy] Lint and dry-run plans only report. Routing files are
replaced atomically after a valid corpus. A reviewed apply plan may move whole
files; it never rewrites rules, deletes content or writes another repository.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Iterable

from .index import RoutingIndex, SKIP_DIRECTORIES, read_units, token_estimate
from .units import GradedUnit, canonical, is_graded_unit_path, relative_path
from ..markdown import _atomic_write_text

LINT_SCHEMA = "mindie-graded-lint/1"
PLAN_SCHEMA = "mindie-shard-plan/1"
TOPOLOGY_SCHEMA = "mindie-shard-topology/1"
APPLY_SCHEMA = "mindie-shard-apply-report/1"
APPLY_PLAN_SCHEMA = "mindie-shard-apply-plan/1"

LEAF_MAX_UNITS = 25
LEAF_MAX_TOKENS = 25000
LEAF_MAX_BYTES = 1_500_000
SUBTREE_MAX_UNITS = 120
SUBTREE_MAX_BYTES = 5_000_000
FEDERATION_MAX_UNITS = 5000
FEDERATION_MAX_BYTES = 50_000_000
FACTOR_MIN_OCCURRENCE = 0.40
SEMANTIC_MIN_JACCARD = 0.35
SEMANTIC_MIN_OVERLAP = 0.60
THRESHOLDS = dict(leaf_units=LEAF_MAX_UNITS, leaf_estimated_tokens=LEAF_MAX_TOKENS,
                  leaf_bytes=LEAF_MAX_BYTES, subtree_units=SUBTREE_MAX_UNITS,
                  subtree_bytes=SUBTREE_MAX_BYTES, federation_units=FEDERATION_MAX_UNITS,
                  federation_bytes=FEDERATION_MAX_BYTES)
_VOLATILE = {"digest", "build_elapsed_ms", "index_build_elapsed_ms", "elapsed_ms"}


def _error(kind: str, message: str) -> dict:
    return {"kind": kind, "message": message}


def _sha(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def load_corpus(root: Path | str) -> tuple[list[GradedUnit], RoutingIndex | None, list[dict]]:
    """Use the C5 scanner/index; the first invalid unit is a visible error."""
    try:
        units = read_units(Path(root))
    except (ValueError, OSError, UnicodeError) as exc:
        return [], None, [_error("invalid_graded_entry", str(exc))]
    try:
        return units, RoutingIndex(units), []
    except (ValueError, TypeError) as exc:
        return units, None, [_error("invalid_inheritance", str(exc))]


def _unit_path(unit: GradedUnit) -> str:
    return unit.path or f"{unit.metadata['domain_path']}/{unit.id}.md"


def _unit_metrics(unit: GradedUnit) -> dict:
    return {
        "path": _unit_path(unit),
        "domain_path": unit.metadata["domain_path"],
        "tier": unit.metadata["tier"],
        "category": unit.metadata["category"],
        "evidence_strength": unit.metadata["evidence_strength"],
        "inherits_from": sorted(unit.metadata["inherits_from"]),
        "content_sha256": unit.content_sha256,
        "bytes": len(unit.render().encode("utf-8")),
        "estimated_tokens": token_estimate(unit.body),
    }


def _rule_key(unit: GradedUnit) -> str:
    return _sha({
        "body": unit.body,
        "conditions": unit.metadata["conditions"],
        "tier": unit.metadata["tier"],
        "category": unit.metadata["category"],
        "evidence_strength": unit.metadata["evidence_strength"],
        "inherits_from": sorted(unit.metadata["inherits_from"]),
    })


def mechanical_duplicates(units: Iterable[GradedUnit]) -> list[dict]:
    """Report exact-rule groups; provenance variants stay separate evidence."""
    grouped: dict[str, list[GradedUnit]] = defaultdict(list)
    for unit in units:
        grouped[_rule_key(unit)].append(unit)
    groups = []
    for rule_sha, members in sorted(grouped.items()):
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda item: item.id)
        variants: dict[str, list[GradedUnit]] = defaultdict(list)
        for unit in members:
            variants[_sha(unit.metadata["provenance"])].append(unit)
        variant_rows = []
        for provenance_sha, variant_members in sorted(variants.items()):
            first = variant_members[0].metadata["provenance"]
            variant_rows.append({
                "provenance_sha256": provenance_sha,
                "unit_ids": [item.id for item in variant_members],
                "source_locator": first.get("source_locator"),
                "source_digest": first.get("source_digest"),
                "evidence_refs": list(first.get("evidence_refs", [])),
            })
        distinct = len(variant_rows) > 1
        group = {
            "rule_sha256": rule_sha,
            "conditions": members[0].metadata["conditions"],
            "unit_ids": [item.id for item in members],
            "provenance_variants": variant_rows,
            "evidence_preserved": True,
            "disposition": "retain_distinct_evidence" if distinct else "exact_rule_duplicate",
            "action": "review_evidence_preserving_merge" if distinct else "review_mechanical_dedup",
            "automatic": False,
        }
        if distinct:
            group["proposals"] = [{
                "type": "propose_evidence_preserving_merge",
                "unit_ids": [item.id for item in members],
                "reason": "Same mechanical rule with distinct provenance; keep every evidence variant.",
                "automatic": False,
            }]
        groups.append(group)
    return groups


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _trigger_strings(unit: GradedUnit) -> set[str]:
    triggers = unit.metadata["triggers"]
    return {_normalized(item) for item in [*triggers["error_signatures"], *triggers["keywords"]] if _normalized(item)}


def _trigger_terms(unit: GradedUnit) -> set[str]:
    terms: set[str] = set()
    for value in _trigger_strings(unit):
        terms.update(re.findall(r"[a-z0-9]+", value))
    return terms


def semantic_merge_reviews(units: Iterable[GradedUnit]) -> list[dict]:
    """Propose review only when fingerprints overlap but conditions differ."""
    ordered = sorted(units, key=lambda item: item.id)
    reviews = []
    for position, left in enumerate(ordered):
        left_strings, left_terms = _trigger_strings(left), _trigger_terms(left)
        if not left_terms:
            continue
        for right in ordered[position + 1:]:
            if (canonical(left.metadata["conditions"]) == canonical(right.metadata["conditions"])
                    and set(left.metadata["inherits_from"]) == set(right.metadata["inherits_from"])):
                continue
            right_strings, right_terms = _trigger_strings(right), _trigger_terms(right)
            shared_terms = left_terms & right_terms
            if not right_terms or not shared_terms:
                continue
            jaccard = len(shared_terms) / len(left_terms | right_terms)
            overlap = len(shared_terms) / min(len(left_terms), len(right_terms))
            shared_strings = left_strings & right_strings
            if not shared_strings and not (overlap >= SEMANTIC_MIN_OVERLAP and jaccard >= SEMANTIC_MIN_JACCARD):
                continue
            reviews.append({
                "type": "propose_semantic_merge_review",
                "unit_ids": [left.id, right.id],
                "paths": [_unit_path(left), _unit_path(right)],
                "jaccard": round(jaccard, 4),
                "overlap": round(overlap, 4),
                "shared_triggers": sorted(shared_strings),
                "condition_diff_keys": sorted(
                    key for key in set(left.metadata["conditions"]) | set(right.metadata["conditions"])
                    if canonical(left.metadata["conditions"].get(key)) != canonical(right.metadata["conditions"].get(key))
                ),
                "inheritance_differs": set(left.metadata["inherits_from"]) != set(right.metadata["inherits_from"]),
                "automatic": False,
            })
    reviews.sort(key=lambda item: (-item["jaccard"], item["unit_ids"]))
    return reviews[:200]


def factor_proposals(units: Iterable[GradedUnit]) -> list[dict]:
    """Propose shared body/condition rules only above the occurrence floor."""
    corpus = list(units)
    if len(corpus) < 2:
        return []
    grouped: dict[str, list[GradedUnit]] = defaultdict(list)
    for unit in corpus:
        grouped[_sha({"body": unit.body, "conditions": unit.metadata["conditions"]})].append(unit)
    proposals = []
    for rule_sha, members in sorted(grouped.items()):
        if len(members) < 2:
            continue
        ratio = len(members) / len(corpus)
        if ratio < FACTOR_MIN_OCCURRENCE:
            continue
        members = sorted(members, key=lambda item: item.id)
        body_sha = hashlib.sha256(members[0].body.encode("utf-8")).hexdigest()
        proposals.append({
            "type": "propose_factor_shared_rule",
            "rule_sha256": rule_sha,
            "conditions": members[0].metadata["conditions"],
            "unit_ids": [item.id for item in members],
            "occurrences": len(members),
            "corpus_units": len(corpus),
            "occurrence_ratio": round(ratio, 4),
            "recommended_target": f"_common/{body_sha[:12]}.md",
            "preserves_conditions": True,
            "preserves_unit_id": True,
            "application": "explicit_reviewed_change",
            "automatic": False,
        })
    return proposals


def _groups(units: Iterable[GradedUnit]) -> tuple[dict, dict, dict, dict]:
    metrics = {unit.id: _unit_metrics(unit) for unit in units}
    leaves: dict[str, set[str]] = defaultdict(set)
    subtrees: dict[str, set[str]] = defaultdict(set)
    federations: dict[str, set[str]] = defaultdict(set)
    for unit in units:
        parent = PurePosixPath(_unit_path(unit)).parent
        parts = [] if parent.as_posix() in {"", "."} else [part for part in parent.parts if part not in {"", "."}]
        leaves["/".join(parts) if parts else "."].add(unit.id)
        subtree_parts = parts[:-1] if len(parts) >= 3 else parts[:2]
        subtrees["/".join(subtree_parts) if subtree_parts else "."].add(unit.id)
        federations[parts[0] if parts else "."].add(unit.id)
    return metrics, leaves, subtrees, federations


def _entry(kind: str, path: str, ids: set[str], metrics: dict) -> dict:
    ordered = sorted(ids)
    return {
        "kind": kind,
        "path": path,
        "unit_ids": ordered,
        "units": len(ordered),
        "bytes": sum(metrics[ident]["bytes"] for ident in ordered),
        "estimated_tokens": sum(metrics[ident]["estimated_tokens"] for ident in ordered),
        "automatic": False,
    }


def _exceeds(kind: str, entry: dict) -> list[str]:
    checks = {
        "leaf": (("units", LEAF_MAX_UNITS), ("estimated_tokens", LEAF_MAX_TOKENS), ("bytes", LEAF_MAX_BYTES)),
        "subtree": (("units", SUBTREE_MAX_UNITS), ("bytes", SUBTREE_MAX_BYTES)),
        "federation": (("units", FEDERATION_MAX_UNITS), ("bytes", FEDERATION_MAX_BYTES)),
    }[kind]
    return [name for name, limit in checks if entry[name] > limit]


def _rows(kind: str, groups: dict, metrics: dict) -> list[dict]:
    rows = []
    for path, ids in sorted(groups.items()):
        row = _entry(kind, path, ids, metrics)
        row["exceeds"] = _exceeds(kind, row)
        row["recommended_action"] = {
            "leaf": "review_leaf_split",
            "subtree": "review_two_level_subtree_split",
            "federation": "review_federation_boundary",
        }[kind]
        rows.append(row)
    return rows


def _status(unit: GradedUnit) -> str:
    status = unit.metadata.get("status", "active")
    return status if status in {"active", "suspect", "archived", "superseded"} else "active"


def growth_metrics(units, *, completed_milestones: int = 0, previous_units=None,
                   loaded_tokens_delta: int | None = None) -> dict:
    """Measure knowledge growth per completed milestone; revisions are not new units."""
    if type(completed_milestones) is not int or completed_milestones < 0:
        raise ValueError("completed_milestones must be a nonnegative integer")
    if loaded_tokens_delta is not None and type(loaded_tokens_delta) is not int:
        raise ValueError("loaded_tokens_delta must be an integer or None")
    current = list(units)
    if isinstance(previous_units, dict):
        previous = list(previous_units.values())
    else:
        previous = list(previous_units or ())
    active = {unit.id for unit in current if _status(unit) == "active"}
    suspect = {unit.id for unit in current if _status(unit) == "suspect"}
    archived = {unit.id for unit in current if _status(unit) == "archived"}
    superseded = {unit.id for unit in current if _status(unit) == "superseded"}
    previous_active = {unit.id for unit in previous if _status(unit) == "active"}
    body_tokens = sum(token_estimate(unit.body) for unit in current)
    previous_body_tokens = sum(token_estimate(unit.body) for unit in previous)
    delta_active = len(active) - len(previous_active)
    net_body = body_tokens - previous_body_tokens
    return {
        "active_units": len(active),
        "suspect_units": len(suspect),
        "archived_units": len(archived),
        "superseded_units": len(superseded),
        "delta_active_units": delta_active,
        "g_units": delta_active / completed_milestones if completed_milestones > 0 else None,
        "g_body": net_body / completed_milestones if completed_milestones > 0 else None,
        "g_read": loaded_tokens_delta,
    }


def shard_plan(units: Iterable[GradedUnit]) -> dict:
    """Return review-only split candidates; nothing here applies a split."""
    units = list(units)
    metrics, leaves, subtrees, federations = _groups(units)
    federation_groups = {".": set(metrics), **{path: ids for path, ids in federations.items() if path != "."}}
    rows = [*_rows("leaf", leaves, metrics), *_rows("subtree", subtrees, metrics),
            *_rows("federation", federation_groups, metrics)]
    return {
        "schema": PLAN_SCHEMA,
        "dry_run": True,
        "automatic_splits": False,
        "cross_repo_writes": False,
        "thresholds": dict(THRESHOLDS),
        "candidates": [row for row in rows if row["exceeds"]],
        "factor_proposals": factor_proposals(units),
        "notes": [
            "Plans only: no leaf was split, no file was moved and no cross-repository write occurred.",
            "Every candidate requires human review; threshold crossing is not evidence of a correct boundary.",
            "Factor proposals are review-only; automatic factoring is not implemented.",
        ],
    }


def topology(units: Iterable[GradedUnit], closure: dict | None = None) -> dict:
    units = list(units)
    metrics, leaves, subtrees, federations = _groups(units)
    domains = {path: ids for path, ids in federations.items() if path != "."}
    payload = {
        "schema": TOPOLOGY_SCHEMA,
        "closure": closure or {},
        "unit_count": len(units),
        "units": metrics,
        "leaves": _rows("leaf", leaves, metrics),
        "two_level_subtrees": _rows("subtree", subtrees, metrics),
        "federation": {"corpus": _entry("federation", ".", set(metrics), metrics),
                       "domains": _rows("federation", domains, metrics)},
    }
    return payload


def _digest(payload: dict) -> str:
    return _sha({key: value for key, value in payload.items() if key not in _VOLATILE})


def _payloads(units: Iterable[GradedUnit], index: RoutingIndex) -> tuple[dict, dict]:
    manifest = index.manifest()
    manifest["units"] = sorted(manifest["units"], key=lambda row: row["id"])
    manifest["id_path"] = {row["id"]: row["path"] for row in manifest["units"]}
    manifest["closure"] = {ident: list(parents) for ident, parents in sorted(index.closure.items())}
    topology_doc = topology(units, manifest["closure"])
    topology_doc["index_build_elapsed_ms"] = index.build_elapsed_ms
    return manifest, topology_doc


def _json_bytes(payload: dict) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _stage(path: Path, payload: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_json_bytes(payload))
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return temporary


def _write_pair(manifest_path: Path, manifest: dict, topology_path: Path, topology_doc: dict) -> None:
    staged: list[tuple[str, Path]] = []
    try:
        staged.append((_stage(manifest_path, manifest), manifest_path))
        staged.append((_stage(topology_path, topology_doc), topology_path))
        for temporary, destination in staged:
            os.replace(temporary, destination)
        staged.clear()
    finally:
        for temporary, _ in staged:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _check_payloads(manifest_path: Path, topology_path: Path, manifest: dict, topology_doc: dict) -> list[str]:
    errors = []
    for label, path, expected in (("routing manifest", manifest_path, manifest),
                                  ("shard topology", topology_path, topology_doc)):
        if not path.is_file():
            errors.append(f"{label} is missing: {path}")
            continue
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(f"{label} cannot be read: {exc}")
            continue
        if not isinstance(existing, dict) or existing.get("schema") != expected.get("schema"):
            errors.append(f"{label} schema changed")
        elif _digest(existing) != _digest(expected):
            errors.append(f"{label} is stale; rebuild required")
    return errors


def build_routing_index(root: Path | str, *, index_dir: Path | str | None = None, check: bool = False) -> dict:
    """Validate, then atomically write or check the two routing documents."""
    root = Path(root)
    out = Path(index_dir) if index_dir is not None else root / "index"
    manifest_path, topology_path = out / "routing-manifest.json", out / "shard-topology.json"
    units, index, errors = load_corpus(root)
    report = {"schema": "mindie-routing-build/1", "ok": False, "check": check, "written": False,
              "manifest": str(manifest_path), "topology": str(topology_path), "errors": list(errors)}
    if errors:
        return report
    try:
        manifest, topology_doc = _payloads(units, index)
    except (ValueError, OSError, UnicodeError) as exc:
        report["errors"].append(_error("routing_build_failed", str(exc)))
        return report
    report.update(unit_count=manifest["unit_count"], generation=manifest["generation"],
                  index_build_elapsed_ms=manifest["build_elapsed_ms"])
    if check:
        report["errors"].extend(_error("stale_routing_index", message)
                                for message in _check_payloads(manifest_path, topology_path, manifest, topology_doc))
        report["ok"] = not report["errors"]
        return report
    try:
        _write_pair(manifest_path, manifest, topology_path, topology_doc)
    except OSError as exc:
        report["errors"].append(_error("routing_write_failed", str(exc)))
        return report
    report.update(ok=True, written=True)
    return report


def build_index(root: Path | str, *, index_dir: Path | str | None = None,
                check: bool = False) -> dict:
    """Compatibility name for the routing-index rebuild entry point."""
    return build_routing_index(root, index_dir=index_dir, check=check)


def validate_repository(root: Path | str) -> dict:
    """Validate a graded corpus and its derivable routing index without writing."""
    units, index, errors = load_corpus(root)
    tiers = {tier: sum(1 for unit in units if unit.metadata["tier"] == tier)
             for tier in ("L1", "L2", "L3", "L4")}
    categories = {category: sum(1 for unit in units if unit.metadata["category"] == category)
                  for category in ("axiom", "measurement", "sop", "workflow", "mutator")}
    return {"schema": "mindie-repository-validation/1", "ok": not errors,
            "valid": not errors and index is not None, "unit_count": len(units),
            "tiers": tiers, "categories": categories,
            "generation": index.generation if index is not None else None,
            "errors": list(errors)}


def lint_corpus(root: Path | str) -> dict:
    """Validate C5/C8 and report review candidates without writing anything."""
    units, index, errors = load_corpus(root)
    duplicates = mechanical_duplicates(units)
    reviews = semantic_merge_reviews(units)
    factors = factor_proposals(units)
    return {
        "schema": LINT_SCHEMA,
        "valid": not errors,
        "root": str(Path(root).resolve()),
        "counts": {
            "units": len(units),
            "bytes": sum(len(unit.render().encode("utf-8")) for unit in units),
            "estimated_tokens": sum(token_estimate(unit.body) for unit in units),
            "mechanical_duplicate_groups": len(duplicates),
            "semantic_merge_reviews": len(reviews),
            "factor_proposals": len(factors),
        },
        "errors": list(errors),
        "closure": {ident: list(parents) for ident, parents in sorted(index.closure.items())} if index else {},
        "mechanical_duplicates": duplicates,
        "semantic_merge_reviews": reviews,
        "factor_proposals": factors,
    }


def _plan_operations(plan: dict) -> list[dict]:
    if not isinstance(plan, dict) or plan.get("schema") != APPLY_PLAN_SCHEMA:
        raise ValueError("unsupported apply plan schema")
    operations = plan.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ValueError("apply plan requires a nonempty operations list")
    normalized = []
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("every apply operation must be an object")
        if operation.get("action", "move") != "move":
            raise ValueError(f"action {operation.get('action')!r} is not implemented; only reviewed whole-file moves are supported")
        for key in ("id", "from", "to"):
            if not isinstance(operation.get(key), str) or not operation[key].strip():
                raise ValueError(f"move operation requires {key}")
        source, destination = relative_path(operation["from"]), relative_path(operation["to"])
        if not source.endswith(".md") or not destination.endswith(".md"):
            raise ValueError("move operations require normalized .md paths")
        normalized.append({"id": operation["id"], "from": source,
                           "to": destination, "content_sha256": operation.get("content_sha256")})
    return normalized


def apply_move_plan(root: Path | str, plan: dict) -> dict:
    """Prevalidate, then recheck digest immediately before each explicit move."""
    root = Path(root)
    report = {"schema": APPLY_SCHEMA, "applied": False, "partial": False, "dry_run": False,
              "automatic_splits": False, "cross_repo_writes": False,
              "factor_application": "separate_reviewed_change", "moved": [], "errors": []}
    try:
        operations = _plan_operations(plan)
        units, index, errors = load_corpus(root)
        if errors:
            raise ValueError(errors[0]["message"])
        before = {ident: list(parents) for ident, parents in index.closure.items()}
        sources: set[str] = set()
        destinations: set[str] = set()
        prepared = []
        for operation in operations:
            source_rel, destination_rel = operation["from"], operation["to"]
            if source_rel == destination_rel:
                raise ValueError(f"{operation['id']}: source and destination are identical")
            for label, relative in (("source", source_rel), ("destination", destination_rel)):
                if not is_graded_unit_path(relative):
                    raise ValueError(f"{label} {relative} is not a canonical graded unit path")
                if set(PurePosixPath(relative).parts[:-1]) & SKIP_DIRECTORIES:
                    raise ValueError(f"{label} {relative} is inside an ignored corpus area")
            if source_rel in sources or destination_rel in destinations:
                raise ValueError("apply plan repeats a source or destination")
            sources.add(source_rel)
            destinations.add(destination_rel)
            source, destination = root / source_rel, root / destination_rel
            if not source.is_file():
                raise ValueError(f"{operation['id']}: source does not exist: {source_rel}")
            if PurePosixPath(destination_rel).name != operation["id"] + ".md":
                raise ValueError("a moved unit keeps its stable ID as the file name")
            if destination.exists():
                raise ValueError(f"{operation['id']}: destination already exists: {destination_rel}")
            unit = GradedUnit.from_text(source.read_text(encoding="utf-8"), source_rel)
            if unit.id != operation["id"]:
                raise ValueError(f"{source_rel}: declared id {operation['id']} does not match {unit.id}")
            if operation.get("content_sha256") not in (None, unit.content_sha256):
                raise ValueError(f"{operation['id']}: content digest changed; review the plan again")
            prepared.append((source_rel, destination_rel, unit))
        for source_rel, destination_rel, expected in prepared:
            source, destination = root / source_rel, root / destination_rel
            current = GradedUnit.from_text(source.read_text(encoding="utf-8"), source_rel)
            if current.id != expected.id or current.content_sha256 != expected.content_sha256:
                raise ValueError(f"{source_rel}: content changed after review; move refused")
            moved = GradedUnit.from_parts(
                {**current.metadata, "domain_path": PurePosixPath(destination_rel).parent.as_posix()},
                current.body, destination_rel,
            )
            _atomic_write_text(destination, moved.render())
            report["moved"].append({"id": current.id, "from": source_rel, "to": destination_rel,
                                    "content_sha256": current.content_sha256})
            source.unlink()
        after_units, after_index, after_errors = load_corpus(root)
        if after_errors:
            raise ValueError(f"post-move validation failed: {after_errors[0]['message']}")
        for ident in before:
            if ident not in after_index.closure or after_index.closure[ident] != before[ident]:
                raise ValueError(f"post-move validation changed inheritance for {ident}")
        report.update(applied=True, verified=True, unit_count=len(after_units))
    except (ValueError, OSError, UnicodeError) as exc:
        report["partial"] = bool(report["moved"])
        report["errors"].append(_error("apply_plan_rejected", str(exc)))
    return report


def build_routing_index_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write or check routing-manifest.json and shard-topology.json.")
    parser.add_argument("--root", default=".", help="corpus root (default: current directory)")
    parser.add_argument("--index-dir", default=None, help="output directory (default: <root>/index)")
    parser.add_argument("--check", action="store_true", help="validate existing files without writing")
    args = parser.parse_args(argv)
    report = build_routing_index(args.root, index_dir=args.index_dir, check=args.check)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


def lint_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lint C5/C8 graded knowledge and report review candidates.")
    parser.add_argument("--root", default=".", help="corpus root (default: current directory)")
    args = parser.parse_args(argv)
    report = lint_corpus(args.root)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["valid"] else 1


def auto_shard_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Propose shard/factor review candidates; apply reviewed whole-file moves only.")
    parser.add_argument("--root", default=".", help="corpus root (default: current directory)")
    parser.add_argument("--dry-run", action="store_true", help="print candidates only (the default without --apply-plan)")
    parser.add_argument("--apply-plan", default=None, metavar="JSON", help="reviewed whole-file move plan")
    args = parser.parse_args(argv)
    if args.apply_plan and args.dry_run:
        parser.error("--dry-run and --apply-plan are mutually exclusive")
    if args.apply_plan:
        try:
            with open(args.apply_plan, encoding="utf-8") as stream:
                plan = json.load(stream)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            print(f"apply plan cannot be read: {exc}", file=sys.stderr)
            return 1
        report = apply_move_plan(args.root, plan)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["applied"] else 1
    units, index, errors = load_corpus(args.root)
    if errors:
        print(json.dumps({"schema": PLAN_SCHEMA, "valid": False, "dry_run": True,
                          "automatic_splits": False, "cross_repo_writes": False,
                          "errors": errors}, indent=2, sort_keys=True))
        return 1
    print(json.dumps(shard_plan(units), indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print("usage: python -m mindie_knowledge.graded.maintenance {build-routing-index|lint|plan} [options]")
        return 0 if argv else 2
    handlers = {"build-routing-index": build_routing_index_main, "build": build_routing_index_main,
                "lint": lint_main, "lint-and-merge": lint_main,
                "plan": auto_shard_main, "auto-shard-and-factor": auto_shard_main}
    if argv[0] in handlers:
        return handlers[argv[0]](argv[1:])
    print(f"unknown maintenance command: {argv[0]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
