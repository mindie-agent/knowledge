"""Technical identifier and CJK token boundaries for ReMe BM25.

ReMe owns indexing, persistence and ranking; these tokens retain exact
qualified identifiers and their searchable components. Ranking scores
measure retrieval relevance, never factual confidence.
"""
from __future__ import annotations

import re
from functools import lru_cache

_WORDS = re.compile(r"[a-z0-9_]+(?:[./+:-][a-z0-9_]+)*|[\u3400-\u9fff]+", re.I)
_CJK = re.compile(r"^[\u3400-\u9fff]+$")
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$", re.I)
_PARTS = re.compile(r"[./+:-]")


def _aliases(word):
    if word.isascii() and word.isalnum():
        return ()
    aliases = []
    for part in _PARTS.split(word):
        if _IDENTIFIER.fullmatch(part):
            aliases.append(part)
            aliases.extend(part.split('_'))
    return tuple(dict.fromkeys(part for part in aliases if len(part) > 1 and part != word))


_cached_aliases = lru_cache(maxsize=2048)(_aliases)


def _tokens(text):
    for match in _WORDS.finditer(text.casefold()):
        word = match.group()
        if _CJK.fullmatch(word) and len(word) > 1:
            for index in range(len(word) - 1):
                yield word[index:index + 2]
        else:
            yield word
            # Long identifiers remain searchable in full, but do not occupy
            # the small cache shared by ordinary repeated software names.
            yield from (_cached_aliases(word) if len(word) <= 256 else _aliases(word))

def tokens(text: str) -> list[str]:
    """Keep exact identifiers and their components searchable, without
    turning software versions into common numeric aliases."""
    return list(_tokens(text))
