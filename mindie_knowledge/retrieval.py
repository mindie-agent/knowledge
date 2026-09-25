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

from mindie_knowledge.markdown import Document


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
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$", re.I)


def tokens(text: str) -> list[str]:
    """Keep exact identifiers and their components searchable, without
    turning software versions into common numeric aliases."""
    result: list[str] = []
    for word in _WORDS.findall(text.casefold()):
        if _CJK.fullmatch(word) and len(word) > 1:
            result.extend(word[i:i + 2] for i in range(len(word) - 1))
        else:
            result.append(word)
            aliases = []
            for part in re.split(r"[./+:-]", word):
                if not _IDENTIFIER.fullmatch(part):
                    continue
                aliases.append(part)
                aliases.extend(part.split("_"))
            # Preserve the qualified name, but also allow npu_rms_norm or
            # rms_norm to find torch_npu.npu_rms_norm. One alias per source
            # occurrence avoids overweighting repeated namespace components.
            result.extend(dict.fromkeys(
                part for part in aliases if len(part) > 1 and part != word
            ))
    return result


def index_text(text: str) -> str:
    """The derived retrieval token stream for one document's source text.

    This is the single tokenization of the document — qualified identifiers,
    their underscore/namespace aliases and CJK bigrams — stored in the FTS
    index at content-change time, so a query tokenizes only the query itself.
    Tokenizer semantics are identical to ``tokens()`` by construction.
    """
    return " ".join(tokens(text))


def lexical_search(text: str, documents: Sequence[Document], *, limit: int) -> list[Hit]:
    """BM25 over already loaded Markdown; exact names survive vector misses."""
    terms = set(tokens(text))
    if not terms or not documents:
        return []
    counts = []
    for document in documents:
        count = Counter(tokens(document.title + "\n" + document.content))
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


def lexical_search_streaming(text: str, make_documents, *, limit: int) -> list[Hit]:
    """BM25 over a lazily-iterated corpus: at most one body in memory.

    ``make_documents()`` must return a fresh iterator of Documents on each
    call. The corpus is walked twice — once for document frequencies and
    lengths, once for scoring — so growing knowledge never requires loading
    every body at once, and no whole-domain size cap is needed. Scores are
    identical to ``lexical_search`` over the same corpus.
    """
    terms = set(tokens(text))
    if not terms:
        return []
    frequencies = {term: 0 for term in terms}
    lengths: dict[str, int] = {}
    for document in make_documents():
        count = Counter(tokens(document.title + "\n" + document.content))
        lengths[document.uri] = sum(count.values())
        for term in terms:
            if term in count:
                frequencies[term] += 1
    total = len(lengths)
    average = sum(lengths.values()) / max(total, 1) or 1
    hits: list[Hit] = []
    for document in make_documents():
        length = lengths[document.uri]
        count = Counter(tokens(document.title + "\n" + document.content))
        score = 0.0
        for term in terms:
            frequency = count[term]
            if frequency:
                inverse = math.log(1 + (total - frequencies[term] + .5) / (frequencies[term] + .5))
                score += inverse * frequency * 2.2 / (frequency + 1.2 * (.25 + .75 * length / average))
        if score:
            hits.append(Hit(document.uri, score, document.title, layer=document.layer))
    return sorted(hits, key=lambda hit: (-hit.score, hit.uri))[:limit]
