"""Source-preserving Markdown context; no semantic or applicability verdicts."""
from __future__ import annotations

import re
from typing import Any, Mapping

from mindie_knowledge.markdown import normalized_sha256
from mindie_knowledge.retrieval import tokens

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FIELD = re.compile(r"^\s*[-*]?\s*([\w./ -]{1,40}):\s*(.+)$")


def blocks(raw: str) -> list[dict[str, Any]]:
    """Recognize original section, paragraph, table and fenced-code spans."""
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    output = []
    headings: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            depth = len(heading[1])
            headings = [(level, title) for level, title in headings if level < depth]
            headings.append((depth, heading[2]))
            output.append({"kind": "heading", "line_start": index + 1, "line_end": index + 1,
                           "section": [title for _, title in headings]})
            index += 1
            continue
        start = index
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
            language = stripped[3:].strip()
            index += 1
            while index < len(lines) and not lines[index].lstrip().startswith(fence):
                index += 1
            closed = index < len(lines)
            index += int(closed)
            kind = "code"
        else:
            kind = "table" if "|" in line and index + 1 < len(lines) and re.match(r"^[\s|:-]+$", lines[index + 1]) else "paragraph"
            index += 1
            while index < len(lines) and lines[index].strip() and not _HEADING.match(lines[index]) and not lines[index].lstrip().startswith(("```", "~~~")):
                if kind == "table" and "|" not in lines[index]:
                    break
                index += 1
        item = {"kind": kind, "line_start": start + 1, "line_end": index,
                "section": [title for _, title in headings]}
        if kind == "code":
            item.update(language=language, closed=closed)
        output.append(item)
    return output


def document_context(raw: str, conditions: Mapping[str, Any] | None = None) -> dict[str, Any]:
    source_hash = normalized_sha256(raw)
    sections = blocks(raw)
    fields = []
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    for block in sections:
        if block["kind"] != "paragraph" or not any(re.search(r"condition|prerequis|context|条件|环境|上下文", heading, re.I) for heading in block["section"]):
            continue
        for number in range(block["line_start"], block["line_end"] + 1):
            match = _FIELD.match(lines[number - 1])
            if match and match[2].strip().casefold() not in {"unknown", "not recorded", "未记录", "未知"}:
                fields.append({"field": match[1].strip(), "value": match[2].strip(),
                               "line_start": number, "line_end": number, "origin": "markdown"})
    for key, value in (conditions or {}).items():
        if str(value).strip().casefold() not in {"", "unknown", "not recorded", "未记录", "未知"}:
            fields.append({"field": key, "value": str(value), "origin": "recorded_sidecar"})
    return {"source_sha256": source_hash, "sections": sections, "conditions": fields}


def structured_excerpt(raw: str, query: str, *, max_chars: int = 600) -> dict[str, Any]:
    """Return bounded exact fragments with source and excerpt coordinates.

    Table headers, section ancestry, fence language and recorded conditions
    survive distant matches. Clipped code is labelled; no closing syntax or
    missing words are invented. Multiple fragments explicitly carry positions.
    """
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = raw.splitlines()
    if not lines or max_chars < 1:
        return {}
    wanted = set(tokens(query))
    weights = [len(wanted.intersection(tokens(line))) for line in lines]
    best = max(range(len(lines)), key=lambda number: weights[number])
    context = document_context(raw)
    owner = next((block for block in context["sections"] if block["line_start"] <= best + 1 <= block["line_end"]), {})
    ranges: list[tuple[int, int, str]] = []
    if owner.get("kind") == "table" and best + 1 > owner["line_start"] + 1:
        ranges.append((owner["line_start"] - 1, min(owner["line_start"] + 1, len(lines)), "table_header"))
    start, end = max(0, best - 2), min(len(lines), best + 5)
    if owner.get("kind") in {"table", "code"}:
        start, end = max(start, owner["line_start"] - 1), min(end, owner["line_end"])
    # Preserve simple contiguous windows verbatim, as in the original API.
    main = "\n".join(lines[start:end])
    if len(main) > max_chars:
        start, end = best, min(best + 5, end)
    ranges.append((start, end, owner.get("kind", "paragraph")))
    # Include a condition outside the main window if it limits this evidence.
    for condition in sorted(context["conditions"], key=lambda item: -len(wanted.intersection(tokens(item["value"])) )):
        line = condition.get("line_start")
        if line and not any(first <= line - 1 < last for first, last, _ in ranges):
            ranges.append((line - 1, line, "condition"))
            break
    fragments: list[str] = []
    spans = []
    used = 0
    clipped = False
    # Reserve source evidence space even when a table header is long.
    for index, (first, last, kind) in enumerate(ranges):
        if any(span["line_start"] <= first + 1 and span["line_end"] >= last for span in spans):
            continue
        text = "\n".join(lines[first:last])
        separator = "\n…\n" if fragments else ""
        available = max_chars - used - len(separator)
        if available <= 0:
            clipped = True
            break
        if kind == "table_header":
            available = min(available, max_chars // 3)
        elif kind != "condition":
            remaining_conditions = ["\n".join(lines[a:b]) for a, b, role in ranges[index + 1:] if role == "condition"]
            if remaining_conditions:
                reserved = min(sum(len(text) + 3 for text in remaining_conditions), max_chars // 4)
                available = max(1, available - reserved)
        column = 1
        if len(text) > available:
            clipped = True
            if first <= best < last and len(lines[best]) > available:
                first = best
                line = lines[best]
                positions = [match.start() for match in re.finditer(r"\S+", line)
                             if wanted.intersection(tokens(match.group()))]
                offset = max(0, (positions[0] if positions else 0) - available // 3)
                column = offset + 1
                text = line[offset:offset + available]
            else:
                text = text[:available]
            last = first + len(text.splitlines())
        if not text:
            continue
        excerpt_start = used + len(separator)
        fragments.append(separator + text)
        used = excerpt_start + len(text)
        spans.append({"line_start": first + 1, "line_end": last, "column_start": column,
                      "excerpt_start": excerpt_start, "excerpt_end": used, "kind": kind})
    primary = next((span for span in spans if span["line_start"] <= best + 1 <= span["line_end"]), spans[0] if spans else {})
    return {"text": "".join(fragments), "line_start": primary.get("line_start", 1),
            "line_end": primary.get("line_end", 1), "column_start": primary.get("column_start", 1),
            "content_sha256": context["source_sha256"], "hash_scope": "utf8_text_with_normalized_newlines",
            "truncated": clipped, "spans": spans, "section": owner.get("section", []),
            **({"language": owner.get("language", ""), "code_clipped": clipped or start > owner["line_start"] - 1 or end < owner["line_end"]}
               if owner.get("kind") == "code" else {})}
