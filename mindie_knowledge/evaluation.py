"""Reproducible retrieval evaluation, separate from ordinary Agent tools."""
from __future__ import annotations

import json
import math
import statistics
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from mindie_knowledge.markdown import normalized_sha256


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1))]


def _relevance(refs: list[str], relevant: Mapping[str, Any], limit: int) -> dict[str, float]:
    grades = {ref: float(grade) for ref, grade in relevant.items() if float(grade) > 0}
    found = [ref for ref in dict.fromkeys(refs[:limit]) if ref in grades]
    first = next((index + 1 for index, ref in enumerate(refs[:limit]) if ref in grades), None)
    seen: set[str] = set()
    gains = []
    for ref in refs[:limit]:
        gains.append(2 ** grades.get(ref, 0) - 1 if ref not in seen else 0)
        seen.add(ref)
    ideal = sorted((2 ** grade - 1 for grade in grades.values()), reverse=True)[:limit]
    dcg = sum(gain / math.log2(index + 2) for index, gain in enumerate(gains))
    idcg = sum(gain / math.log2(index + 2) for index, gain in enumerate(ideal))
    return {"recall": len(found) / len(grades), "mrr": 1 / first if first else 0.0,
            "ndcg": dcg / idcg if idcg else 0.0}


def _source(config: Any, hit: Mapping[str, Any]) -> str | None:
    location = hit.get("path")
    if not isinstance(location, str):
        return None
    path = Path(location)
    for layer in ("shared", "project", "candidate"):
        for root in config.mount(layer).roots:
            try:
                path.resolve().relative_to(Path(root).resolve())
                return path.read_text(encoding="utf-8")
            except (OSError, ValueError, UnicodeError):
                continue
    return None


def _evidence(raw: str | None, hit: Mapping[str, Any]) -> bool | None:
    if raw is None:
        return None
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    evidence = hit.get("evidence") or {}
    excerpt = str(hit.get("excerpt") or "")
    if evidence.get("content_sha256") != normalized_sha256(normalized):
        return False
    lines = normalized.splitlines()
    spans = evidence.get("spans") or [{**evidence, "excerpt_start": 0, "excerpt_end": len(excerpt)}]
    try:
        for span in spans:
            first, last = int(span["line_start"]), int(span["line_end"])
            column = int(span.get("column_start", 1))
            start, end = int(span["excerpt_start"]), int(span["excerpt_end"])
            if not (1 <= first <= last <= len(lines) and column >= 1 and 0 <= start < end <= len(excerpt)):
                return False
            source = "\n".join(lines[first - 1:last])[column - 1:]
            if not source.startswith(excerpt[start:end]):
                return False
        return bool(spans)
    except (KeyError, TypeError, ValueError):
        return False


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [row for row in rows if "quality" in row]
    evidence = [value for row in rows for value in row["evidence_valid"] if value is not None]
    no_evidence = [row for row in rows if row.get("expects_no_evidence")]
    delays = [row["elapsed_ms"] for row in rows]
    return {"queries": len(rows), "scored_queries": len(scored),
            "unlabelled_queries": sum(row.get("unlabelled", False) for row in rows),
            **{metric: statistics.mean(row["quality"][metric] for row in scored) if scored else None
               for metric in ("recall", "mrr", "ndcg")},
            "evidence_accuracy": sum(evidence) / len(evidence) if evidence else None,
            "evidence_checked": len(evidence),
            "evidence_unknown": sum(value is None for row in rows for value in row["evidence_valid"]),
            "no_evidence_abstention": sum(not row["refs"] for row in no_evidence) / len(no_evidence) if no_evidence else None,
            "incomplete_queries": sum(row["incomplete"] for row in rows),
            "latency_ms": {"p50": percentile(delays, .5), "p95": percentile(delays, .95), "max": max(delays) if delays else None},
            "output_bytes": sum(row["output_bytes"] for row in rows)}


def evaluate(config: Any, cases: Iterable[Mapping[str, Any]], *, limit: int = 8,
             search_fn: Callable[..., Any] | None = None,
             source_fn: Callable[[Mapping[str, Any]], str | None] | None = None) -> dict[str, Any]:
    """Run labelled questions through the actual query function by default.

    Qrels map exact document refs to relevance grades. Missing labels are
    unknown, not negatives. Explicit ``no_evidence`` cases measure abstention.
    Aliases and generated questions used for development must not be described
    as a held-out or independently annotated production evaluation.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    if search_fn is None:
        from mindie_knowledge.server.query import query
        search_fn = query
    rows = []
    seen = set()
    for case in cases:
        ident = str(case.get("id") or "")
        question = str(case.get("query") or "").strip()
        if not ident or ident in seen or not question:
            raise ValueError("evaluation cases require unique ids and nonempty queries")
        seen.add(ident)
        relevant = case.get("relevant")
        if relevant is not None and not isinstance(relevant, Mapping):
            raise ValueError(f"{ident}: relevant must map document refs to numeric grades")
        if relevant is not None and any(not isinstance(grade, (int, float)) or not math.isfinite(grade) or not 0 <= grade <= 4 for grade in relevant.values()):
            raise ValueError(f"{ident}: relevance grades must be finite numbers from 0 to 4")
        started = time.perf_counter()
        result = search_fn(config, text=question, limit=limit)
        elapsed = (time.perf_counter() - started) * 1000
        payload = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        hits = list(payload.get("results", []))[:limit]
        refs = [str(hit.get("ref") or hit.get("uri") or "") for hit in hits]
        row = {"id": ident, "category": str(case.get("category") or "unspecified"), "query": question,
               "refs": refs, "elapsed_ms": elapsed, "incomplete": bool(payload.get("incomplete") or payload.get("degraded")),
               "output_bytes": len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
               "evidence_valid": [_evidence(source_fn(hit) if source_fn else _source(config, hit), hit) for hit in hits]}
        if relevant and any(grade > 0 for grade in relevant.values()):
            row["quality"] = _relevance(refs, relevant, limit)
        elif case.get("no_evidence") is True:
            row["expects_no_evidence"] = True
        else:
            row["unlabelled"] = True
        expected = case.get("expected_context")
        if isinstance(expected, list):
            row["context_coverage"] = {str(term): any(str(term).casefold() in (str(hit.get("excerpt", "")) + json.dumps(hit.get("conditions", {}), ensure_ascii=False)).casefold() for hit in hits) for term in expected}
        rows.append(row)
    categories = {category: _summary([row for row in rows if row["category"] == category])
                  for category in sorted({row["category"] for row in rows})}
    return {"schema": "vaws-retrieval-evaluation/1", "limit": limit, "summary": _summary(rows),
            "categories": categories, "cases": rows,
            "meaning": "Retrieval relevance and source fidelity, not correctness of hardware or software claims."}
