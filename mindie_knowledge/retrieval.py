"""Small lexical search over already loaded Markdown.

No model, query rewriting or applicability scoring.
Scores rank retrieval usefulness, never confidence in a document's claims.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Sequence

from mindie_knowledge.markdown import Document, retrieval_aliases


@dataclass
class Hit:
    uri: str
    score: float
    title: str = ""
    excerpt: str = ""
    layer: str = ""
    content: str = ""

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
