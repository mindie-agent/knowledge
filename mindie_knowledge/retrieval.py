"""Retrieval tokenization for the derived search index.

Document tokenization happens at content-change time (``index_text`` feeds
the FTS index); a query tokenizes only the query itself. Scores rank
retrieval usefulness, never confidence in a document's claims.
"""
from __future__ import annotations

import re

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
