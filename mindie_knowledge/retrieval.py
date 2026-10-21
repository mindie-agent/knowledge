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
_ASCEND_COMPOUND_SPLITS: dict[str, tuple[str, ...]] = {
    "aclgraph": ("acl", "graph"),
    "npugraph": ("npu", "graph"),
    "npugraph_ex": ("npugraph", "npu", "graph", "ex"),
    "torchair": ("torch", "air"),
    "modelslim": ("model", "slim"),
    "msmodelslim": ("ms", "modelslim", "model", "slim"),
    "dspark": ("ds", "spark"),
    "dflash": ("df", "flash"),
    "xlite": ("xl", "lite"),
    "eplb": ("ep", "lb"),
    "mrv2": ("mr", "v2"),
    "hixl": ("hi", "xl"),
    "lmcache": ("lm", "cache"),
    "mooncake": ("moon", "cake"),
    "sfa_v1": ("sfa", "v1"),
    "sfav1": ("sfa", "v1"),
    "dsa_v1": ("dsa", "v1"),
    "dsav1": ("dsa", "v1"),
    "mla_v1": ("mla", "v1"),
    "mlav1": ("mla", "v1"),
    "fa3": ("fa",),
    "fa3_v1": ("fa3", "fa", "v1"),
    "fa3v1": ("fa3", "fa", "v1"),
    "qli_v2": ("qli", "v2"),
    "qliv2": ("qli", "v2"),
    "quarot": ("qua", "rot"),
    "teacache": ("tea", "cache"),
    "memscope": ("mem", "scope"),
    "msmemscope": ("ms", "memscope", "mem", "scope"),
    "msprof": ("ms", "prof"),
    "msprobe": ("ms", "probe"),
    "serving_cast": ("serving", "cast"),
    "servingcast": ("serving", "cast"),
    "tensor_cast": ("tensor", "cast"),
    "tensorcast": ("tensor", "cast"),
}
_ASCEND_COMPOUND_RE = re.compile(
    r"(?:"
    + "|".join(sorted((re.escape(k) for k in _ASCEND_COMPOUND_SPLITS), key=len, reverse=True))
    + r")",
    re.I,
)


def _aliases(raw: str) -> tuple[str, ...]:
    if (
        raw.isascii()
        and raw.islower()
        and raw.isalnum()
        and not _DIGIT_ALPHA.search(raw)
        and not _ASCEND_COMPOUND_RE.search(raw)
    ):
        return ()
    word = raw.casefold()
    aliases: list[str] = []
    for part in _PARTS.split(raw):
        if part.isidentifier() if part.isascii() else _IDENTIFIER.fullmatch(part):
            part_lower = part.casefold()
            aliases.append(part_lower)
            if part_lower in _ASCEND_COMPOUND_SPLITS:
                aliases.extend(_ASCEND_COMPOUND_SPLITS[part_lower])
            for chunk in part.split("_"):
                if not chunk:
                    continue
                lowered = chunk.casefold()
                aliases.append(lowered)
                if lowered in _ASCEND_COMPOUND_SPLITS:
                    aliases.extend(_ASCEND_COMPOUND_SPLITS[lowered])
                if not chunk.islower() or _DIGIT_ALPHA.search(lowered):
                    for piece in _SUBWORDS.findall(chunk):
                        piece_lower = piece.casefold()
                        aliases.append(piece_lower)
                        if piece_lower in _ASCEND_COMPOUND_SPLITS:
                            aliases.extend(_ASCEND_COMPOUND_SPLITS[piece_lower])
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
