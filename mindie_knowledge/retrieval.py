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
