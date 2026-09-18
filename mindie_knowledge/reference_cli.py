"""Explicit maintenance and evaluation entry points; no ordinary task hooks."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import time
from typing import Any
import uuid

from mindie_knowledge.server.layers import ConfigError

STDOUT_BYTES = 16_384


def _json(path: Path, limit: int = 268_435_456) -> Any:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"artifact exceeds the read budget: {path.name}")
    return json.loads(raw)


def _object(path: Path, *, limit: int = 67_108_864) -> dict:
    value = _json(path, limit)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _map(path: Path) -> dict:
    value = _object(path)
    for field in ("source_id", "root"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise ValueError(f"{path.name}: source map requires {field}")
    for field in ("scope", "repositories"):
        if not isinstance(value.get(field, []), list) or any(not isinstance(item, str) for item in value.get(field, [])):
            raise ValueError(f"{path.name}: {field} must be a list of strings")
    for field, keys in (("files", ("path", "content_sha256")), ("nodes", ("id", "kind"))):
        items = value.get(field, [])
        if not isinstance(items, list) or any(not isinstance(item, dict) or any(not isinstance(item.get(key), str) for key in keys) for item in items):
            raise ValueError(f"{path.name}: {field} contains invalid source records")
        if field == "nodes" and any(item.get(key) is not None and not isinstance(item[key], str)
                                    for item in items for key in ("name", "qualified_name", "path", "content_sha256")):
            raise ValueError(f"{path.name}: node names and paths must be strings")
    return value


def _changes(path: Path) -> dict:
    value = _object(path)
    if not isinstance(value.get("source_id"), str):
        raise ValueError(f"{path.name}: changes require source_id")
    for field in ("changed", "removed", "added", "unknown"):
        if not isinstance(value.get(field, []), list) or any(not isinstance(item, str) for item in value.get(field, [])):
            raise ValueError(f"{path.name}: {field} must be a list of paths")
    symbols = value.get("symbols", [])
    if not isinstance(symbols, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in symbols):
        raise ValueError(f"{path.name}: symbols must contain symbol ids")
    return value


def _cases(path: Path) -> list[dict]:
    cases = _json(path, 8_388_608)
    if isinstance(cases, dict):
        cases = cases.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 500 or any(not isinstance(case, dict) for case in cases):
        raise ValueError("evaluation requires 1..500 question objects")
    seen = set()
    for case in cases:
        ident, question = case.get("id"), case.get("query")
        if not isinstance(ident, str) or not 1 <= len(ident) <= 256 or ident in seen:
            raise ValueError("evaluation ids must be unique nonempty strings of at most 256 characters")
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            raise ValueError(f"{ident}: query must contain 1..4000 characters")
        seen.add(ident)
        relevant = case.get("relevant")
        if relevant is not None and (not isinstance(relevant, dict) or any(
            not isinstance(ref, str) or not isinstance(grade, (int, float)) or not math.isfinite(grade) or not 0 <= grade <= 4
            for ref, grade in relevant.items())):
            raise ValueError(f"{ident}: relevant must map references to finite grades from 0 to 4")
        if "no_evidence" in case and not isinstance(case["no_evidence"], bool):
            raise ValueError(f"{ident}: no_evidence must be a boolean")
        if "expected_context" in case and (not isinstance(case["expected_context"], list) or any(not isinstance(item, str) for item in case["expected_context"])):
            raise ValueError(f"{ident}: expected_context must be a list of strings")
    return cases


def _artifact(root: Path, kind: str) -> Path:
    return Path(root) / kind / f"{uuid.uuid4().hex}.json"


def _evaluate(config, cases, *, limit: int, max_seconds: float) -> dict:
    # Reuse the evaluator's aggregation so a stopped run scores only actual
    # completed queries. A currently running synchronous query is not cancelled.
    from mindie_knowledge.evaluation import _summary, evaluate
    from mindie_knowledge.server.query import query

    started = time.perf_counter()
    deadline = started + max_seconds
    rows = []
    report = evaluate(config, [], limit=limit)

    class DeadlineReached(Exception):
        pass

    def search(*positional, **kwargs):
        if time.perf_counter() >= deadline:
            raise DeadlineReached
        return query(*positional, **kwargs)

    for case in cases:
        try:
            one = evaluate(config, [case], limit=limit, search_fn=search)
        except DeadlineReached:
            break
        rows.extend(one["cases"])
    complete = len(rows) == len(cases)
    report.update(cases=rows, summary=_summary(rows),
                  categories={category: _summary([row for row in rows if row["category"] == category])
                              for category in sorted({row["category"] for row in rows})},
                  status="complete" if complete else "incomplete", complete=complete,
                  requested_queries=len(cases), remaining_queries=len(cases) - len(rows),
                  elapsed_seconds=round(time.perf_counter() - started, 6), max_seconds=max_seconds,
                  deadline_semantics="Stops before the next query; an active synchronous query is not interrupted.")
    if not complete:
        report["reason"] = "evaluation deadline reached; unexecuted questions are not scored"
    return report


def _emit(report: dict, state: Path) -> None:
    raw = json.dumps(report, ensure_ascii=False, indent=2)
    if len(raw.encode("utf-8")) > STDOUT_BYTES:
        from mindie_knowledge.distribution.manifest import atomic_write_json
        output = _artifact(state, "reference-reports")
        atomic_write_json(output, report)
        compact = {key: report[key] for key in ("status", "complete", "snapshot", "output", "changes_output", "gap_count") if key in report}
        compact.update(stdout_truncated=True, report_output=str(output), report_bytes=len(raw.encode("utf-8")))
        raw = json.dumps(compact, ensure_ascii=False, indent=2)
    print(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    catalog = commands.add_parser("catalog", help="Refresh a rebuildable lexical catalog without starting vectors")
    catalog.add_argument("--config", required=True)
    catalog.add_argument("--verify", action="store_true")
    catalog.add_argument("--repair", action="store_true", help="Build a bounded new catalog generation and switch only when complete")
    evaluation = commands.add_parser("evaluate", help="Run labelled questions through the public query implementation")
    evaluation.add_argument("--config", required=True)
    evaluation.add_argument("--cases", type=Path, required=True)
    evaluation.add_argument("--output", type=Path)
    evaluation.add_argument("--baseline", type=Path)
    evaluation.add_argument("--limit", type=int, default=8)
    evaluation.add_argument("--max-seconds", type=float, default=120,
                            help="Stop admitting queries after this total time (active query may finish)")
    code = commands.add_parser("code-map", help="Build a bounded Python/C++ static map on demand")
    code.add_argument("--root", type=Path, required=True)
    code.add_argument("--state", type=Path, required=True)
    code.add_argument("--revision")
    code.add_argument("--path", action="append")
    code.add_argument("--symbol")
    code.add_argument("--depth", type=int, default=2)
    code.add_argument("--limit", type=int, default=40)
    code.add_argument("--output", type=Path, help="Save full map privately; stdout remains bounded")
    code.add_argument("--before", type=Path, help="Compare a previous map of the same source/scope")
    code.add_argument("--changes-output", type=Path, help="Save complete change evidence for relations --changes")
    code.add_argument("--max-files", type=int, default=3000)
    code.add_argument("--max-seconds", type=float, default=30)
    relations = commands.add_parser("relations", help="Associate selected notes with static source evidence")
    relations.add_argument("--config", required=True)
    relations.add_argument("--ref", action="append", required=True)
    relations.add_argument("--code-map", type=Path, action="append", default=[])
    relations.add_argument("--backlinks")
    relations.add_argument("--changes", type=Path)
    relations.add_argument("--limit", type=int, default=40)
    relations.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        exit_code = 0
        if args.command != "code-map":
            from mindie_knowledge.server.layers import load_config
            config_path = Path(args.config).expanduser()
            if not config_path.is_file():
                raise ValueError("the explicit --config must name an existing file")
            config = load_config(path=config_path)
            state = config.state_root
        else:
            state = args.state
        if args.command == "catalog":
            if args.repair:
                from mindie_knowledge.catalog import repair_catalog
                from mindie_knowledge.distribution.references import prepared_shared_documents
                report = repair_catalog(config, extra_documents=prepared_shared_documents(config.state_root))
            else:
                from mindie_knowledge.maintenance import refresh_references
                report = refresh_references(config, verify=args.verify)
            for key in ("changed_uris", "deleted_uris"):
                report[key + "_count"] = len(report.pop(key, []))
            report["ready"] = report.get("status") == "ready"
            exit_code = 0 if report["ready"] else 2
        elif args.command == "evaluate":
            if not 1 <= args.limit <= 20 or not math.isfinite(args.max_seconds) or not 0 < args.max_seconds <= 3600:
                raise ValueError("limit must be 1..20 and max-seconds must be positive and at most 3600")
            cases = _cases(args.cases)
            before = None
            if args.baseline:
                before = _object(args.baseline).get("summary")
                if not isinstance(before, dict):
                    raise ValueError("evaluation baseline requires a summary object")
            report = _evaluate(config, cases, limit=args.limit, max_seconds=args.max_seconds)
            if before is not None:
                report["comparison"] = {key: {"baseline": before.get(key), "current": report["summary"].get(key)}
                                        for key in ("recall", "mrr", "ndcg", "evidence_accuracy", "no_evidence_abstention", "latency_ms", "output_bytes")}
            exit_code = 0 if report["complete"] else 2
        elif args.command == "code-map":
            from mindie_knowledge.code_map import build_code_map, compare_maps, navigate
            if args.changes_output and not args.before:
                raise ValueError("--changes-output requires --before")
            if not 1 <= args.limit <= 1000 or not 0 <= args.depth <= 8:
                raise ValueError("limit must be 1..1000 and depth must be 0..8")
            if not 1 <= args.max_files <= 100000 or not math.isfinite(args.max_seconds) or not 0 < args.max_seconds <= 3600:
                raise ValueError("max-files must be 1..100000 and max-seconds must be positive and at most 3600")
            before = _map(args.before) if args.before else None
            graph = build_code_map(args.root, args.state, revision=args.revision, paths=args.path,
                                   limits={"max_files": args.max_files, "max_seconds": args.max_seconds})
            from mindie_knowledge.distribution.manifest import atomic_write_json
            output = args.output or _artifact(state, "source-maps")
            atomic_write_json(output, graph)
            report = navigate(graph, args.symbol, depth=args.depth, limit=args.limit) if args.symbol else {
                key: graph.get(key) for key in ("status", "snapshot", "revision", "source_complete", "complete", "stats")}
            report["output"] = str(output)
            report["gap_count"] = len(graph.get("gaps", []))
            if before is not None:
                changes = compare_maps(before, graph)
                changes_output = args.changes_output or _artifact(state, "source-changes")
                atomic_write_json(changes_output, changes)
                report["changes_output"] = str(changes_output)
                report["changes"] = {key: len(value) if isinstance(value, (list, dict)) else value for key, value in changes.items()}
            exit_code = 0 if graph.get("status") == "ready" else 2
        else:
            from mindie_knowledge.catalog import get_catalog_document
            from mindie_knowledge.relations import affected_documents, backlinks, build_relations
            if len(args.ref) > 128 or len(args.code_map) > 4:
                raise ValueError("select at most 128 notes and 4 source maps for one relations pass")
            if not 1 <= args.limit <= 1000:
                raise ValueError("limit must be 1..1000")
            if args.backlinks and args.changes:
                raise ValueError("select either --backlinks or --changes")
            maps = [_map(path) for path in args.code_map]
            changes = _changes(args.changes) if args.changes else None
            documents = [get_catalog_document(config, ref) for ref in args.ref]
            missing = [ref for ref, document in zip(args.ref, documents) if document is None]
            relations_report = build_relations([doc for doc in documents if doc], code_maps=maps)
            from mindie_knowledge.distribution.manifest import atomic_write_json
            output = args.output or _artifact(state, "relations")
            atomic_write_json(output, relations_report)
            if args.backlinks:
                report = backlinks(relations_report, args.backlinks, limit=args.limit)
            elif args.changes:
                report = affected_documents(relations_report, changes, limit=args.limit)
            else:
                report = {"documents": len(relations_report["documents"]), "relations": len(relations_report["edges"]),
                          "truncated": relations_report.get("truncated"), "gaps": relations_report.get("gaps", [])[:args.limit]}
            report["missing_refs"] = missing
            report["output"] = str(output)
            report["status"] = "partial" if missing or report.get("truncated") else "ready"
            exit_code = 0 if report["status"] == "ready" else 2
        if args.command == "evaluate":
            from mindie_knowledge.distribution.manifest import atomic_write_json
            output = args.output or _artifact(state, "evaluations")
            atomic_write_json(output, report)
            report = {key: report[key] for key in ("status", "complete", "summary", "meaning", "comparison", "reason", "requested_queries", "remaining_queries", "elapsed_seconds", "max_seconds", "deadline_semantics") if key in report}
            report["output"] = str(output)
        _emit(report, state)
        return exit_code
    except (OSError, ValueError, KeyError, TypeError, ConfigError) as exc:
        print(json.dumps({"status": "incomplete", "reason": str(exc)[:1000]}, ensure_ascii=False))
        return 1
