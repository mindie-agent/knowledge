"""Test-side lexical search oracle: the pre-index in-memory BM25 path.

Kept only as the recall/ranking oracle for the FTS5 index regressions; the
product serves queries from the derived index and never walks the corpus.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass

from mindie_knowledge.markdown import Document  # noqa: F401  (re-export for callers)
from mindie_knowledge.retrieval import tokens

__all__ = ["lexical_search", "lexical_search_streaming", "Hit", "tokens"]


@dataclass
class Hit:
    uri: str
    score: float
    title: str = ""
    excerpt: str = ""
    layer: str = ""
    content: str = ""


def _bm25_scores(terms, counts_per_doc):
    lengths = [sum(count.values()) for count in counts_per_doc]
    average = sum(lengths) / max(len(lengths), 1) or 1
    total = len(counts_per_doc)
    frequencies = {term: sum(term in count for count in counts_per_doc) for term in terms}
    scores = []
    for count, length in zip(counts_per_doc, lengths):
        score = 0.0
        for term in terms:
            frequency = count[term]
            if frequency:
                inverse = math.log(1 + (total - frequencies[term] + .5) / (frequencies[term] + .5))
                score += inverse * frequency * 2.2 / (frequency + 1.2 * (.25 + .75 * length / average))
        scores.append(score)
    return scores


def lexical_search(text, documents, *, limit):
    """BM25 over already loaded Markdown; exact names survive vector misses."""
    terms = set(tokens(text))
    if not terms or not documents:
        return []
    counts = [Counter(tokens(d.title + "\n" + d.content)) for d in documents]
    scores = _bm25_scores(terms, counts)
    hits = []
    for document, score in zip(documents, scores):
        if score:
            hits.append(Hit(document.uri, score, document.title, layer=document.layer))
    return sorted(hits, key=lambda hit: (-hit.score, hit.uri))[:limit]


def lexical_search_streaming(text, make_documents, *, limit):
    """BM25 over a lazily-iterated corpus; scores identical to lexical_search."""
    terms = set(tokens(text))
    if not terms:
        return []
    frequencies = {term: 0 for term in terms}
    lengths = {}
    for document in make_documents():
        count = Counter(tokens(document.title + "\n" + document.content))
        lengths[document.uri] = sum(count.values())
        for term in terms:
            if term in count:
                frequencies[term] += 1
    total = len(lengths)
    average = sum(lengths.values()) / max(total, 1) or 1
    hits = []
    for document in make_documents():
        count = Counter(tokens(document.title + "\n" + document.content))
        score = 0.0
        for term in terms:
            frequency = count[term]
            if frequency:
                inverse = math.log(1 + (total - frequencies[term] + .5) / (frequencies[term] + .5))
                score += inverse * frequency * 2.2 / (frequency + 1.2 * (.25 + .75 * lengths[document.uri] / average))
        if score:
            hits.append(Hit(document.uri, score, document.title, layer=document.layer))
    return sorted(hits, key=lambda hit: (-hit.score, hit.uri))[:limit]
