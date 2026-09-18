"""Small lexical complement and source excerpts for the existing vector index.

No model, query rewriting or applicability scoring.
Ranks are fused, never interpreted as confidence in a document's claims.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Sequence

from mindie_knowledge.local.backend import Hit
from mindie_knowledge.markdown import Document, retrieval_aliases

_WORDS = re.compile(r"[a-z0-9_]+(?:[./+:-][a-z0-9_]+)*|[\u3400-\u9fff]+", re.I)
_CJK = re.compile(r"^[\u3400-\u9fff]+$")


def tokens(text: str) -> list[str]:
    """Keep code identifiers and adjacent Chinese characters searchable."""
    result: list[str] = []
    for word in _WORDS.findall(text.casefold()):
        if _CJK.fullmatch(word) and len(word) > 1:
            result.extend(word[i:i + 2] for i in range(len(word) - 1))
        else:
            result.append(word)
    return result


def lexical_search(text: str, documents: Sequence[Document], *, limit: int) -> list[Hit]:
    """BM25 over already loaded Markdown; exact names survive vector misses."""
    terms = set(tokens(text))
    if not terms or not documents:
        return []
    counts = []
    for document in documents:
        count = Counter(tokens(document.title + "\n" + document.content))
        for alias_text in retrieval_aliases(document):
            for term in tokens(alias_text):
                count[term] += .5
        counts.append(count)
    lengths = [sum(count.values()) for count in counts]
    average = sum(lengths) / max(len(lengths), 1) or 1
    frequencies = {term: sum(term in count for count in counts) for term in terms}
    hits: list[Hit] = []
    for document, count, length in zip(documents, counts, lengths):
        score = 0.0
        for term in terms:
            frequency = count[term]
            if frequency:
                inverse = math.log(1 + (len(documents) - frequencies[term] + .5) / (frequencies[term] + .5))
                score += inverse * frequency * 2.2 / (frequency + 1.2 * (.25 + .75 * length / average))
        if score:
            hits.append(Hit(document.uri, score, document.title, layer=document.layer))
    return sorted(hits, key=lambda hit: (-hit.score, hit.uri))[:limit]


def fuse(vector: Sequence[Hit], lexical: Sequence[Hit]) -> list[tuple[Hit, list[str]]]:
    """Preserve each route's rank, weighting agreement by lexical strength.

    Plain RRF makes even two very weak matches outrank a strong single-route
    match. Keep the stronger rank vote and scale the additional agreement vote
    by this query's relative positive lexical score. Vector score units are
    never compared to lexical units. Flat positive lexical scores recover RRF;
    single-route ranks, URI deduplication and source validation are unchanged.
    These are retrieval signals, not confidence in a document's claims.
    """
    votes: dict[str, list[float]] = {}
    lexical_scores: dict[str, float] = {}
    selected: dict[str, Hit] = {}
    methods: dict[str, list[str]] = {}
    for method, hits in (("vector", vector), ("lexical", lexical)):
        seen: set[str] = set()
        rank = 0
        for hit in hits:
            if hit.uri in seen:
                continue
            seen.add(hit.uri)
            rank += 1
            votes.setdefault(hit.uri, []).append(1 / (60 + rank))
            selected.setdefault(hit.uri, hit)
            methods.setdefault(hit.uri, []).append(method)
            if method == "lexical" and math.isfinite(hit.score) and hit.score > 0:
                lexical_scores[hit.uri] = hit.score
    maximum = max(lexical_scores.values(), default=0)
    scores = {}
    for uri, contributions in votes.items():
        agreement = lexical_scores.get(uri, 0) / maximum if maximum else 0
        scores[uri] = max(contributions)
        if len(contributions) == 2:
            scores[uri] += min(contributions) * agreement
    result: list[tuple[Hit, list[str]]] = []
    for uri in sorted(scores, key=lambda key: (-scores[key], key)):
        hit = selected[uri]
        result.append((Hit(uri, scores[uri], hit.title, hit.excerpt, hit.layer, hit.content), methods[uri]))
    return result


def source_excerpt(raw: str, query: str, *, max_chars: int = 600) -> dict:
    """Quote bounded original spans, retaining their Markdown context."""
    from mindie_knowledge.context import structured_excerpt
    return structured_excerpt(raw, query, max_chars=max_chars)
