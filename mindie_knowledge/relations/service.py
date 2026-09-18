"""Explicit links and clearly labelled symbol candidates from existing prose.

Inputs are already loaded Documents and code-map snapshots. This module neither
fetches sources nor opens arbitrary linked files, runs models, or edits notes.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit


def _normal_url(value):
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.scheme in {"http", "https"}:
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), parsed.query, ""))
    return value


def _local_path(value):
    # Lexical normalization must not probe a document-supplied UNC share or
    # follow arbitrary symlinks merely to build a reference worklist.
    return os.path.normcase(os.path.abspath(value))


def _values(value, trail=""):
    """Source/evidence is ordinary optional metadata, not a required schema."""
    if isinstance(value, str):
        yield trail, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _values(item, f"{trail}.{key}" if trail else str(key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _values(item, f"{trail}[{index}]")


def _links(text):
    # Fenced examples are not declarations of actual document dependencies.
    definitions = {}
    lines, fence = [], None
    for number, line in enumerate(text.splitlines(), 1):
        start = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if start:
            marker = start.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence:
            continue
        definition = re.match(r'^\s{0,3}\[([^]]+)\]:\s*(?:<([^>]+)>|(\S+))', line)
        if definition:
            definitions[definition.group(1).casefold()] = definition.group(2) or definition.group(3)
        else:
            lines.append((number, line))
    for number, line in lines:
        for match in re.finditer(r'\[[^]\n]*\]\(\s*(?:<([^>]+)>|([^\s()]*(?:\([^()]*\)[^\s()]*)*))(?:(?:\s+)["\'][^\n]*?["\'])?\s*\)', line):
            target = match.group(1) or match.group(2)
            if target:
                yield number, target
        for match in re.finditer(r"\[([^]\n]+)\](?:\[([^]\n]*)\])?(?!\()", line):
            target = definitions.get((match.group(2) or match.group(1)).casefold())
            if target:
                yield number, target
        for match in re.finditer(r"<(https?://[^>]+)>", line):
            yield number, match.group(1)


def build_relations(documents, *, code_maps=(), limit=10000, symbol_limit=8) -> dict:
    """Build explicit links/backlinks plus scoped code-mention candidates.

    ``limit`` bounds output relations, ``symbol_limit`` bounds one ambiguous
    mention. It is not a source-scanning budget: callers pass loaded documents.
    Stable ids include their source-map identity to avoid cross-repo collisions.
    """
    if not 1 <= limit <= 100000 or not 1 <= symbol_limit <= 100:
        raise ValueError("limit must be 1..100000 and symbol_limit 1..100")
    documents = list(documents)
    code_maps = list(code_maps)
    by_uri = {doc.uri: doc for doc in documents}
    by_path = {_local_path(doc.path): doc.uri for doc in documents}
    by_source = {}
    for doc in documents:
        for _, value in _values(doc.source):
            if value.startswith(("http://", "https://")):
                by_source.setdefault(_normal_url(value), []).append(doc.uri)
    symbols, files, remote_files = {}, {}, {}
    for code_map in code_maps:
        root = Path(code_map["root"])
        for record in code_map.get("files", []):
            files[_local_path(root / record["path"])] = (code_map, record)
            if code_map.get("revision"):
                for repo in code_map.get("repositories", []):
                    remote_files[f"https://github.com/{repo}/blob/{code_map['revision']}/{record['path']}"] = (code_map, record)
        for node in code_map.get("nodes", []):
            if node.get("kind") not in {"function", "function_declaration", "class", "torch_operator", "dynamic_api"}:
                continue
            for name in {node.get("name"), node.get("qualified_name")} - {None, ""}:
                symbols.setdefault(name, []).append((code_map, node))
    edges, gaps, seen = [], [], set()
    truncated = False

    def add(edge):
        nonlocal truncated
        key = (edge["source"], edge["target"], edge["kind"], edge.get("source_snapshot"), edge["evidence"].get("line_start"), edge["evidence"].get("metadata_field"))
        if key in seen:
            return
        seen.add(key)
        if len(edges) >= limit:
            truncated = True
            return
        edges.append(edge)

    for doc in documents:
        raw = doc.raw_text or doc.content
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        targets = [(line, target, None) for line, target in _links(raw)]
        for field, value in _values({"source": doc.source, "evidence": doc.evidence}):
            if value.startswith(("http://", "https://", "viking://", "file://")) or Path(value).is_absolute() or field.rsplit(".", 1)[-1] in {"path", "file", "url", "uri", "href"}:
                targets.append((None, value, field))
        for line, raw_target, field in targets:
            try:
                parsed = urlsplit(raw_target)
            except ValueError:
                gaps.append({"document": doc.uri, "kind": "invalid_link", "line_start": line})
                continue
            fragment = unquote(parsed.fragment)
            target = _normal_url(raw_target)
            status, kind = "unresolved", "explicit_link"
            mapped = []
            source_map = None
            record = None
            if raw_target in by_uri:
                mapped = [raw_target]
            elif parsed.scheme in {"http", "https"}:
                mapped = [ref for ref in by_source.get(target, []) if ref != doc.uri]
                status = "external_unchecked" if not mapped else "resolved"
                if target in remote_files:
                    source_map, record = remote_files[target]
                    mapped = [f"code:{source_map['source_id']}:{record['path']}"]
                    kind = "explicit_code_link"
            elif not parsed.scheme or parsed.scheme == "file" or Path(raw_target).is_absolute():
                if parsed.scheme == "file":
                    # Decode URI syntax before Windows Path translates its
                    # separators; '/C:/...' otherwise becomes '\\C:\\...'.
                    raw_path = unquote(parsed.path)
                    if parsed.netloc and parsed.netloc.lower() != "localhost":
                        raw_path = "//" + unquote(parsed.netloc) + raw_path
                    elif re.match(r"^/[A-Za-z]:", raw_path):
                        raw_path = raw_path[1:]
                    local = Path(raw_path)
                elif Path(raw_target).is_absolute():
                    local = Path(raw_target.split("#", 1)[0])
                else:
                    local = doc.path.parent / unquote(parsed.path) if parsed.path else doc.path
                canonical = _local_path(local)
                target = canonical
                if canonical in by_path:
                    mapped = [by_path[canonical]]
                elif canonical in files:
                    source_map, record = files[canonical]
                    mapped = [f"code:{source_map['source_id']}:{record['path']}"]
                    kind = "explicit_code_link"
                else:
                    # Deliberately no filesystem stat: an unprovided target is
                    # unknown, not proven missing or deleted.
                    status = "outside_snapshot"
            if mapped:
                status = "resolved" if len(mapped) == 1 else "ambiguous"
            evidence = {"document": doc.uri, "content_sha256": digest, "line_start": line, "original_target": raw_target}
            if field:
                evidence["metadata_field"] = field
            for ident in mapped or [target]:
                edge = {"source": doc.uri, "target": ident, "kind": kind, "status": status,
                        "fragment": fragment, "fragment_status": "unchecked" if fragment else "none", "evidence": evidence}
                if source_map:
                    edge.update(source_id=source_map["source_id"], source_path=record["path"],
                                source_snapshot=source_map.get("snapshot"), source_sha256=record["content_sha256"])
                    span = re.fullmatch(r"L(\d+)(?:-L?(\d+))?", fragment)
                    if span and isinstance(record.get("line_count"), int):
                        start, end = int(span[1]), int(span[2] or span[1])
                        edge["fragment_status"] = "verified" if 1 <= start <= end <= record["line_count"] else "outside_snapshot"
                add(edge)
        # Code spans identify candidate mentions without treating every common
        # prose word as a symbol. No case folding or name-similarity guesses.
        for number, line in enumerate(raw.splitlines(), 1):
            for mention in re.findall(r"(?<!`)`([^`\n]+)`(?!`)", line):
                matches = symbols.get(mention.strip(), [])
                if len(matches) > symbol_limit:
                    gaps.append({"document": doc.uri, "line_start": number, "kind": "symbol_candidates_clipped", "count": len(matches)})
                for code_map, node in matches[:symbol_limit]:
                    add({"source": doc.uri, "target": f"code:{code_map['source_id']}:{node['id']}",
                         "kind": "symbol_candidate", "status": "candidate", "name": mention,
                         "source_id": code_map["source_id"], "source_snapshot": code_map.get("snapshot"),
                         "source_path": node.get("path"), "symbol_id": node["id"],
                         "symbol_evidence": node.get("evidence"),
                         "evidence": {"document": doc.uri, "content_sha256": digest, "line_start": number}})
    return {"schema": 1, "documents": [{"ref": doc.uri, "path": str(doc.path), "title": doc.title} for doc in documents],
            "edges": edges, "gaps": gaps, "truncated": truncated,
            "semantics": "Explicit references and possible symbol associations; no truth, conflict or runtime inference."}


def backlinks(report: dict, target: str, *, limit=40) -> dict:
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be 1..1000")
    hits = [edge for edge in report.get("edges", []) if target in {edge["target"], edge.get("symbol_id"), edge.get("source_path")}]
    return {"target": target, "relations": hits[:limit], "truncated": len(hits) > limit or report.get("truncated", False)}


def affected_documents(report: dict, changes: dict, *, limit=100) -> dict:
    """Find linked notes to inspect, without modifying or invalidating their claims."""
    if not 1 <= limit <= 10000:
        raise ValueError("limit must be 1..10000")
    states = {path: state for state in ("changed", "removed", "unknown") for path in changes.get(state, [])}
    symbol_ids = {item["id"] for item in changes.get("symbols", [])}
    found = {}
    for edge in report.get("edges", []):
        if edge.get("source_id") != changes.get("source_id"):
            continue
        path = edge.get("source_path")
        state = states.get(path)
        if edge["kind"] == "symbol_candidate" and state == "changed" and edge.get("symbol_id") not in symbol_ids:
            continue
        if not state and edge.get("symbol_id") not in symbol_ids:
            continue
        found.setdefault(edge["source"], []).append({"source_path": path, "change": state or "symbol_changed",
            "relation": edge["kind"], "target": edge["target"], "evidence": edge["evidence"]})
    items = [{"document": doc, "reasons": reasons} for doc, reasons in sorted(found.items())]
    return {"documents": items[:limit], "truncated": len(items) > limit or report.get("truncated", False),
            "before": changes.get("before"), "after": changes.get("after"),
            "semantics": "Inspect these references; source changes do not establish that any recorded claim is false."}
