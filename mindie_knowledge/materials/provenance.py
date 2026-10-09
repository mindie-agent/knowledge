"""Derived citation relationships for retrieval, never evidence or authority.

Only literal references in material bodies create relationships. Historical
task-revision citations can identify a current task, but are never rewritten
into a readable reference to the old bytes. All grouping is query-specific.
"""
from __future__ import annotations

import base64
import binascii
import json
import re

from ..retrieval import tokens
from .references import block_ref, task_ref


POLICY = "citation-query/1"
_ID = r"[0-9a-f]{64}"
_REF = re.compile(
    rf"(?<![\w/])mindie://(?P<domain>[a-z][a-z0-9-]{{0,63}})/(?P<task>{_ID})"
    rf"(?:/blocks/(?P<block>{_ID})@(?P<sha>{_ID})|@(?P<revision>{_ID}))?"
    r"(?![\w/@-])"
)
_CURSOR_SCHEMA = "mindie-query-page/1"
_CURSOR_BYTES = 24 * 1024
INITIAL_RELATED = 2


class QueryContinuationError(ValueError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(f"{code}: {detail}")


def query_request(query, limit, conditions, continuation):
    """Validate caller data before reading or repairing any material/index."""
    if continuation is not None:
        if query is not None or conditions is not None:
            raise QueryContinuationError("continuation_invalid", "send only continuation and optional limit")
        cursor = decode_continuation(continuation)
        query, conditions = cursor["query"], cursor["conditions"]
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise ValueError("query must contain 1..2000 characters")
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    if conditions is not None and (not isinstance(conditions, dict)
                                   or not all(isinstance(key, str) for key in conditions)):
        raise ValueError("conditions must be an object with string keys")
    return query, conditions


def citations(body):
    """Deduplicate literal canonical references without consulting other data."""
    return list(dict.fromkeys(match.group() for match in _REF.finditer(body)))


def body_tokens(body):
    # The reference itself must not make a source body match a query about its
    # domain, hexadecimal identity, or a word such as 'blocks'.
    return sorted(set(tokens(_REF.sub(" ", body))))


def resolve_citations(literals, tasks, domain):
    """Describe current availability, retaining the exact cited literal.

    `tasks` contains validated current manifests. Missing membership is ordinary
    unavailability; a caller's failure to read/validate a present manifest must
    propagate before entering this pure function.
    """
    output = []
    for literal in literals:
        match = _REF.fullmatch(literal)
        if match is None:
            raise ValueError("invalid derived material citation; rebuild the index")
        fields = match.groupdict()
        target = tasks.get(fields["task"]) if fields["domain"] == domain else None
        item = dict(cited_ref=literal, citation_status="missing",
                    source_task_id=fields["task"], source_domain=fields["domain"])
        if fields["domain"] != domain:
            item["citation_status"] = "cross_domain"
        elif target is not None:
            item["current_source_ref"] = task_ref(domain, fields["task"])
            if fields["block"]:
                descriptor = next((b for b in target["blocks"]
                                   if b["block_id"] == fields["block"]), None)
                if descriptor is not None and descriptor["sha256"] == fields["sha"]:
                    item["citation_status"] = "current"
                    item["current_source_ref"] = block_ref(
                        domain, fields["task"], fields["block"], fields["sha"])
                else:
                    item["citation_status"] = "block_unavailable"
            elif fields["revision"] and fields["revision"] != target["entry"]["revision"]:
                item["citation_status"] = "version_unavailable"
            else:
                item["citation_status"] = "current"
        output.append(item)
    return output


def _rank(hit):
    return (-hit["score"], hit["ref"])


def _public(hit):
    return {key: value for key, value in hit.items() if not key.startswith("_")}


def _related_preview(hit):
    """Keep each observation's evidence and identity, without repeated indexes.

    The excerpt remains the match's own material, including failures and
    corrections. It is a locating aid, not a complete account of that block.
    Reading ``ref`` supplies the complete body; continuation visits the other
    matches rather than replaying the two already visible observations.
    """
    fields = ("entry_id", "ref", "feedback_ref", "excerpt", "cites", "conditions",
              "match_basis", "score")
    return {key: hit[key] for key in fields}


def group_matches(matches, tasks, domain):
    """Group resolvable single-source matches without treating citation as proof.

    A source must cover all matched query terms in the citing block's body.
    Index-only matches cannot establish that relationship. A missing source,
    cross-domain edge, multiple source tasks or a cycle prevents lineage grouping.
    Different matching blocks of one ordinary task remain one navigable result.
    """
    ordered = sorted(matches, key=_rank)
    by_task = {}
    by_ref = {}
    for hit in ordered:
        by_task.setdefault(hit["entry_id"], []).append(hit)
        by_ref[hit["ref"]] = hit
        hit["cites"] = resolve_citations(hit["_cites"], tasks, domain)

    edges, blocked = {}, set()
    for hit in ordered:
        cites = hit["cites"]
        if not cites:
            continue
        sources = {(item["source_domain"], item["source_task_id"]) for item in cites}
        if len(sources) != 1 or any(item["citation_status"] not in
                                   {"current", "version_unavailable"} for item in cites):
            blocked.add(hit["ref"])
            continue
        source_id = cites[0]["source_task_id"]
        source_matches = by_task.get(source_id, [])
        # A task with no independent body match is not a source anchor for this
        # query. A newly observed term therefore stays with its actual observer.
        if not hit["_body_terms"]:
            continue
        exact_blocks = {item["current_source_ref"] for item in cites
                        if "/blocks/" in item["current_source_ref"]}
        for source in source_matches:
            if exact_blocks and source["ref"] not in exact_blocks:
                continue
            if source["_body_terms"] and hit["_body_terms"] <= source["_body_terms"]:
                edges[hit["ref"]] = source["ref"]
                break

    def root(reference):
        seen = set()
        while True:
            if reference in blocked or reference in seen:
                return None
            seen.add(reference)
            if reference not in edges:
                return reference
            reference = edges[reference]

    grouped = {}
    for hit in ordered:
        anchor_ref = root(hit["ref"])
        anchor_id = by_ref[anchor_ref]["entry_id"] if anchor_ref else hit["entry_id"]
        group = grouped.setdefault(anchor_id, dict(anchors={}, linked_anchors=set(), matches={}))
        group["matches"][hit["ref"]] = hit
        anchor = by_ref[anchor_ref] if anchor_ref else hit
        group["anchors"][anchor["ref"]] = anchor
        if anchor["entry_id"] != hit["entry_id"]:
            group["linked_anchors"].add(anchor["ref"])

    result = []
    for group in grouped.values():
        anchors = ([group["anchors"][reference] for reference in group["linked_anchors"]]
                   if group["linked_anchors"] else list(group["anchors"].values()))
        anchor = min(anchors, key=_rank)
        members = sorted(group["matches"].values(), key=_rank)
        related = [_public(item) for item in members if item["ref"] != anchor["ref"]]
        result.append(dict(_public(anchor), related=related,
                           group_score=max(item["score"] for item in members),
                           group_basis=("citation" if any(item["entry_id"] != anchor["entry_id"]
                                                          for item in members) else "task")))
    return sorted(result, key=lambda item: (-item["group_score"], item["ref"]))


def decode_continuation(value):
    if not isinstance(value, str) or not value or len(value) > _CURSOR_BYTES * 2:
        raise QueryContinuationError("continuation_invalid", "invalid cursor envelope")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        if len(raw) > _CURSOR_BYTES:
            raise ValueError("oversized cursor")
        item = json.loads(raw)
    except (ValueError, UnicodeError, binascii.Error):
        raise QueryContinuationError("continuation_invalid", "invalid cursor encoding") from None
    fields = {"schema", "query", "conditions", "corpus", "anchor", "after"}
    if (not isinstance(item, dict) or set(item) != fields or item["schema"] != _CURSOR_SCHEMA
            or not isinstance(item["query"], str) or not 0 < len(item["query"]) <= 2000
            or not isinstance(item["conditions"], dict)
            or not all(isinstance(item[key], str) and item[key] for key in ("corpus", "anchor", "after"))):
        raise QueryContinuationError("continuation_invalid", "invalid cursor fields")
    return item


def _encode_continuation(query, conditions, fingerprint, anchor, after):
    item = dict(schema=_CURSOR_SCHEMA, query=query, conditions=conditions or {},
                corpus=fingerprint, anchor=anchor, after=after)
    raw = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                     allow_nan=False).encode("utf-8")
    if len(raw) > _CURSOR_BYTES:
        raise QueryContinuationError("continuation_invalid", "query metadata exceeds the cursor envelope")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def page_groups(groups, *, query, conditions, fingerprint, limit, continuation=None):
    """Recomputable pages; tokens grant no authority and store no query state."""
    if continuation is not None:
        cursor = decode_continuation(continuation)
        if cursor["corpus"] != fingerprint:
            raise QueryContinuationError("continuation_expired", "the searchable corpus changed; query again")
        if cursor["query"] != query or cursor["conditions"] != (conditions or {}):
            raise QueryContinuationError("continuation_invalid", "cursor query differs from the request")
        group = next((item for item in groups if item["ref"] == cursor["anchor"]), None)
        if group is None:
            raise QueryContinuationError("continuation_invalid", "cursor anchor is not a current result")
        refs = [item["ref"] for item in group["related"]]
        if cursor["after"] not in refs:
            raise QueryContinuationError("continuation_invalid", "cursor position is not a related match")
        selected = [(group, refs.index(cursor["after"]) + 1, limit)]
    else:
        selected = [(group, 0, INITIAL_RELATED) for group in groups[:limit]]
    result = []
    for group, start, count in selected:
        related = group["related"][start:start + count]
        next_page = None
        if start + len(related) < len(group["related"]):
            next_page = _encode_continuation(query, conditions, fingerprint, group["ref"], related[-1]["ref"])
        if continuation is None:
            related = [_related_preview(item) for item in related]
        result.append(dict(group, related=related, related_count=len(group["related"]),
                           related_next=next_page))
    return result
