"""[Role] Apply explicitly reviewed C5 edits, factoring and withdrawals locally.

[Invariants] A plan supplies complete authored units and the exact reviewed
corpus generation. This module validates the projection; it never authors a
rule, accepts a semantic proposal or promotes evidence. Existing IDs keep their
identity; semantic edits advance revision, location-only edits retain it.
[Timeouts & Locks] Local reads and atomic file replacements; no subprocesses,
network, model or lock. The invoking reviewer owns the content checkout.
[Side-Effect Policy] Writes only the declared unit changes and derived routing
index. Partial filesystem failures remain explicit; no commit or publication.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..markdown import _atomic_write_text
from ..community.common import CommunityError
from .contribution import parse_unit
from .index import RoutingIndex
from .maintenance import build_routing_index

SCHEMA = "mindie-reviewed-change/1"


def prepare_change(root, plan):
    """Validate an authored change against the full current inheritance graph.

    ``review_reference`` records the caller's source of approval. It is not a
    boolean asserting that a model proposal has received independent review.
    The caller must obtain that review before invoking the apply command.
    """
    root = Path(root).resolve(strict=True)
    if not isinstance(plan, dict) or plan.get("schema") != SCHEMA:
        raise ValueError("unsupported reviewed change schema")
    if not isinstance(plan.get("review_reference"), str) or not plan["review_reference"].strip():
        raise ValueError("an explicit review reference is required")
    before = RoutingIndex.from_root(root)
    if plan.get("base_generation") != before.generation:
        raise ValueError("the corpus changed after review; prepare a new reviewed change")
    changes = plan.get("changes")
    if not isinstance(changes, list) or not changes:
        raise ValueError("reviewed changes must contain authored units or withdrawals")
    projected = dict(before.units)
    selected, writes, removals = set(), {}, set()
    for change in changes:
        ident = change.get("id") if isinstance(change, dict) else None
        if not isinstance(ident, str) or ident in selected:
            raise ValueError("every reviewed change must select one unique stable ID")
        selected.add(ident)
        old = before.units.get(ident)
        expected = old.content_sha256 if old is not None else None
        if change.get("expected_content_sha256") != expected:
            raise ValueError(f"{ident}: reviewed content digest differs")
        content = change.get("content")
        if content is None:
            if old is None:
                raise ValueError(f"{ident}: withdrawal requires a current unit")
            projected.pop(ident)
            removals.add(old.path)
            continue
        try:
            unit = parse_unit(content, change.get("path"))
        except CommunityError as error:
            raise ValueError(str(error)) from error
        if unit.id != ident:
            raise ValueError("authored unit differs from its selected stable ID")
        if old is not None:
            semantic_change = unit.content_sha256 != old.content_sha256
            expected_revision = old.revision + 1 if semantic_change else old.revision
            if unit.revision != expected_revision:
                raise ValueError(f"{ident}: reviewed edit requires revision {expected_revision}")
            if old.path != unit.path:
                removals.add(old.path)
        elif unit.revision != 1:
            raise ValueError("a new reviewed unit starts at revision 1")
        projected[ident] = unit
        if unit.path in writes:
            raise ValueError("reviewed units share one destination path")
        writes[unit.path] = unit.render()
    after = RoutingIndex(projected.values())
    # Canonical paths bind each file name to its stable ID, so a move cannot
    # overwrite an unrelated existing unit or a repository document.
    current_paths = {unit.path for unit in before.units.values()}
    for path in writes:
        if (root / path).exists() and path not in current_paths:
            raise ValueError(f"reviewed destination already contains a non-unit file: {path}")
    return dict(root=root, before=before, after=after, writes=writes,
                removals=sorted(removals - set(writes)),
                review_reference=plan["review_reference"])


def apply_change(root, plan):
    """Apply one reviewed local projection; publication retains its own gate."""
    prepared = prepare_change(root, plan)
    root = prepared["root"]
    report = dict(schema="mindie-reviewed-change-result/1", status="prepared",
                  review_reference=prepared["review_reference"],
                  base_generation=prepared["before"].generation,
                  generation=prepared["after"].generation,
                  written=[], removed=[], publication="not_requested", errors=[])
    try:
        # Check all authored inputs before the first write. There is no
        # implicit retry after a partial application or a concurrent edit.
        if RoutingIndex.from_root(root).generation != prepared["before"].generation:
            raise ValueError("the corpus changed before reviewed application")
        for path, content in prepared["writes"].items():
            _atomic_write_text(root / path, content)
            report["written"].append(path)
        for path in prepared["removals"]:
            (root / path).unlink()
            report["removed"].append(path)
        routing = build_routing_index(root)
        report["routing"] = routing
        if not routing["ok"]:
            raise ValueError("reviewed units were applied but routing rebuild failed: "
                             + "; ".join(error["message"] for error in routing["errors"]))
        if routing["generation"] != prepared["after"].generation:
            raise ValueError("the corpus changed during reviewed application")
        report["status"] = "applied"
    except (OSError, ValueError) as error:
        report["status"] = "partial" if report["written"] or report["removed"] else "failed"
        report["errors"].append(dict(error_class=type(error).__name__, detail=str(error)))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Apply complete, independently reviewed C5 edits to one checkout.")
    parser.add_argument("--root", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--apply", action="store_true", help="write the reviewed projection; default is validation only")
    args = parser.parse_args(argv)
    try:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        if args.apply:
            result = apply_change(args.root, plan)
        else:
            prepared = prepare_change(args.root, plan)
            result = dict(status="validated", base_generation=prepared["before"].generation,
                          generation=prepared["after"].generation,
                          writes=list(prepared["writes"]), removals=prepared["removals"],
                          publication="not_requested")
    except (OSError, ValueError) as error:
        result = dict(status="failed", errors=[dict(error_class=type(error).__name__, detail=str(error))])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"applied", "validated"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
