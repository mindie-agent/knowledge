"""[Role] Technical identifier and CJK token boundaries for local BM25.

[Invariants] Retain qualified identifiers, versions and searchable components.
Frequency aggregation must equal individual token expansion; relevance is not
factual confidence. [Timeouts & Locks] Pure CPU work; no waits or locks.
[Side-Effect Policy] No I/O; only the bounded identifier-alias cache is shared.
"""
from __future__ import annotations

from collections import Counter
import re
from functools import lru_cache

_WORDS = re.compile(r"[a-z0-9_]+(?:[./+:-][a-z0-9_]+)*|[\u3400-\u9fff]+", re.I)
_CJK = re.compile(r"^[\u3400-\u9fff]+$")
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$", re.I)
_PARTS = re.compile(r"[./+:-]")
_DIGIT_ALPHA = re.compile(r"[0-9][a-z]")
# Split camelCase/PascalCase/acronym transitions (e.g., DataCopyPad, ACLGraph,
# aclrtSynchronizeStream) and chip/API digit boundaries (e.g., Ascend910B4 ->
# ascend, 910b4) while keeping the full compound token indexed for exact boosts.
_SUBWORDS = re.compile(
    r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+[A-Za-z][A-Za-z0-9]*"
)


def _aliases(raw: str) -> tuple[str, ...]:
    if raw.isascii() and raw.islower() and raw.isalnum() and not _DIGIT_ALPHA.search(raw):
        return ()
    word = raw.casefold()
    aliases: list[str] = []
    for part in _PARTS.split(raw):
        if part.isidentifier() if part.isascii() else _IDENTIFIER.fullmatch(part):
            aliases.append(part.casefold())
            for chunk in part.split("_"):
                if not chunk:
                    continue
                lowered = chunk.casefold()
                aliases.append(lowered)
                if not chunk.islower() or _DIGIT_ALPHA.search(lowered):
                    for piece in _SUBWORDS.findall(chunk):
                        aliases.append(piece.casefold())
    return tuple(dict.fromkeys(part for part in aliases if len(part) > 1 and part != word))


_cached_aliases = lru_cache(maxsize=2048)(_aliases)


def _tokens(text):
    for match in _WORDS.finditer(text):
        raw = match.group()
        word = raw.casefold()
        if _CJK.fullmatch(word) and len(word) > 1:
            for index in range(len(word) - 1):
                yield word[index:index + 2]
        else:
            yield word
            # Long identifiers remain searchable in full, but do not occupy
            # the small cache shared by ordinary repeated software names.
            yield from (_cached_aliases(raw) if len(raw) <= 256 else _aliases(raw))

def tokens(text: str) -> list[str]:
    """Keep exact identifiers and their components searchable, without
    turning software versions into common numeric aliases."""
    return list(_tokens(text))


def token_counts(text: str) -> Counter[str]:
    """Expand each distinct lexeme once when a BM25 index needs frequencies."""
    counts = {}
    for raw, frequency in Counter(_WORDS.findall(text)).items():
        word = raw.casefold()
        # _WORDS emits an entire CJK run separately from identifier lexemes.
        if '\u3400' <= word[0] <= '\u9fff' and len(word) > 1:
            for index in range(len(word) - 1):
                token = word[index:index + 2]
                counts[token] = counts.get(token, 0) + frequency
        else:
            counts[word] = counts.get(word, 0) + frequency
            for alias in (_cached_aliases(raw) if len(raw) <= 256 else _aliases(raw)):
                counts[alias] = counts.get(alias, 0) + frequency
    return Counter(counts)
