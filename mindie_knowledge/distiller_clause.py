"""Constituent boundaries and finite-clause agreement in technical prose.

[Role]
Constituent boundaries and finite-clause agreement in technical prose.
[Invariants]
Nominal attachment and finite predicates share one local grammar.
[Timeouts & Locks]
Pure bounded string processing; no I/O, locks, waits, or subprocesses.
[Side-Effect Policy]
No mutations of inputs, files, environment, or external state.
"""

from __future__ import annotations

import re

from .distiller_lexicon import (
    _PARSE_CACHE, _LEGACY_MENTION, _HARD_BREAK, _SENTENCE_BOUNDARY, _PREDICATE_WORD, _PREDICATE_ONLY,
    _ADVERB_WORD, _LEADING_ADVERBS, _LY_PREFIX, _LY_STEM, _LY_VERB_FORM, _FUNCTION_WORD,
    _PREPOSITION, _SUBORDINATOR, _DETERMINER, _PLURAL_DETERMINER, _SINGULAR_DETERMINER,
    _PRONOUN, _PLURAL_PRONOUN, _AUXILIARY, _SINGULAR_AUX, _PLURAL_AUX, _PHRASE_TOKEN,
    _NONFINITE_PAST, _FINITE_PRETERITE, _REGULAR_ED, _SYNCRETIC_PARTICIPLE, _FINITE_ING,
    _PRESENT_PARTICIPLE, _PREDICATIVE_PARTICIPLE, _PREDICATIVE_STATE, _PARTICLE, _COORDINATOR,
    _CLAUSE_LINKER, _DEGREE_INTENSIFIER, _DEGREE_COMPLEMENT, _NUMERIC_SPECIFIER,
    _PURE_ADJECTIVE, _VERBAL_STEM, _CLAUSE_VERB, _MASS_HEAD, _VERBAL_ER, _NOMINAL_HEAD,
    _IRREGULAR_PLURAL, _SINGULAR_S, _INITIALISM_PLURAL, _PREDICATE_FRAGMENT,
    _finite_lexeme,
)

def _is_plural_nominal(word: str) -> bool:
    low = word.lower()
    return (_IRREGULAR_PLURAL.fullmatch(low) is not None or _INITIALISM_PLURAL.fullmatch(low) is not None
            or (low.endswith("s") and len(low) > 2 and not _SINGULAR_S.fullmatch(low)))


def _is_3sg_verb(word: str) -> bool:
    low = word.lower()
    return low.endswith("s") and len(low) > 2 and not _SINGULAR_S.fullmatch(low)


def _degree_end(after: str) -> int | None:
    """Return the end offset in *after* of '[intensifier*] <manner-adverb>' after 'so'."""
    pos, matched = len(after) - len(after.lstrip()), None
    if pos == 0:
        return None
    while pos < len(after):
        if (comp := _DEGREE_COMPLEMENT.match(after, pos)) and not _LY_STEM.fullmatch(comp.group()):
            matched = comp.end()
        step = _DEGREE_INTENSIFIER.match(after, pos)
        if not step or _LY_STEM.fullmatch(step.group()):
            break
        nxt = after[step.end():]
        if not nxt or not nxt[0].isspace():
            break
        pos = step.end() + len(nxt) - len(nxt.lstrip())
    return matched


def _without_adverbs(text: str, *, context: str = "clause") -> str:
    """Consume modifiers, never a lexical verb or a medial clause linker."""
    cache = _PARSE_CACHE.get()
    if cache is not None:
        key = ("wa", text, context)
        if key in cache:
            return cache[key]
    clause_text = _SENTENCE_BOUNDARY.split(text, maxsplit=1)[0]
    end, previous = 0, ""
    while modifier := _LEADING_ADVERBS.match(clause_text, end):
        word = modifier.group().lower()
        if _LY_STEM.fullmatch(word) or (word == "yet" and previous not in ("not", "never")):
            break
        after_mod = clause_text[modifier.end():]
        if (word == "once" and context == "clause" and not previous
                and "," in after_mod and not after_mod.lstrip().startswith(",")):
            break
        if word == "so":
            degree = _degree_end(after_mod)
            if degree is None:
                break
            end = modifier.end() + degree
        else:
            end = modifier.end()
        end += re.match(r"\s*(?:,\s*)?", clause_text[end:]).end()
        previous = word
    res = clause_text[end:]
    if cache is not None:
        cache[key] = res
    return res


def _is_present_participle(text: str) -> re.Match[str] | None:
    match = _PRESENT_PARTICIPLE.match(text)
    return None if match and _FINITE_ING.fullmatch(match.group()) else match


def _base_from_3sg(low: str) -> tuple[str, str]:
    if low.endswith("ies") and len(low) > len("ies"):
        return low.removesuffix("ies") + "y", low[:-1]
    if low.endswith(("ches", "shes", "xes", "zes", "ses")) and len(low) > len("ches"):
        return low.removesuffix("es"), low[:-1]
    return low[:-1], (low.removesuffix("es") if low.endswith("es") and len(low) > len("es") + 1 else low[:-1])


def _vp_complement(text: str, *, verb: str = "") -> bool:
    """Recognize adverbial or oblique continuations beyond a candidate predicate."""
    clause = _SENTENCE_BOUNDARY.split(text, maxsplit=1)[0]
    stripped = _without_adverbs(clause, context="tail")
    if stripped != clause:
        return True
    prep = _PREPOSITION.match(stripped)
    return bool(prep and prep.group().lower() not in ("of", "by", "via")
                and (not verb or _finite_lexeme(verb, context="oblique"))
                and _pp_tail(stripped) is not None)


def _strong_vp_frame(text: str) -> bool:
    """Separate an object or VP adverbial from a bare attached preposition."""
    clause = _SENTENCE_BOUNDARY.split(text, maxsplit=1)[0].strip()
    first = _PHRASE_TOKEN.match(clause)
    if not first:
        return False
    if _DETERMINER.fullmatch(first.group()):
        return _np_span(clause, role="object") is not None
    stripped = _without_adverbs(clause, context="tail")
    if stripped == clause:
        return False
    if not stripped:
        return not re.fullmatch(r"(?:only|just|merely|solely|even|not|so)", clause, re.I)
    return bool(_PREPOSITION.match(stripped) or _np_span(stripped, role="object") is not None
                or _LEGACY_MENTION.match(stripped))


def _finite_frame(word: str, rest: str, number: str | None = None) -> bool:
    """Use open stems in strong frames, lexical valency in weaker frames."""
    return (_finite_lexeme(word, number)
            or (_finite_lexeme(word, number, context="strong") and _strong_vp_frame(rest))
            or (_finite_lexeme(word, number, context="oblique") and _vp_complement(rest, verb=word)))


def _imperative_clause(text: str) -> bool:
    """Require a base-form predicate and its argument for an implicit subject."""
    first = _PHRASE_TOKEN.match(text)
    if not first or not _finite_lexeme(first.group(), "infinitive"):
        return False
    rest = text[first.end():].lstrip()
    return bool(_LEGACY_MENTION.match(rest) or _strong_vp_frame(rest))


def _medial_head(text: str, number: str | None) -> bool:
    """Look through an adjective sequence for a nominal, not a finite VP."""
    cur = text
    while tok := _PHRASE_TOKEN.match(cur):
        word, rest = tok.group().lower(), cur[tok.end():].lstrip()
        if (_PURE_ADJECTIVE.fullmatch(word) or _REGULAR_ED.fullmatch(word)
                or _NUMERIC_SPECIFIER.fullmatch(word)):
            if not rest or rest == cur:
                return False
            cur = rest
            continue
        return not (any(p.fullmatch(word) for p in (
            _FUNCTION_WORD, _DETERMINER, _PRONOUN, _AUXILIARY, _LEADING_ADVERBS))
            or _LEGACY_MENTION.match(cur) or _NONFINITE_PAST.fullmatch(word)
            or _is_present_participle(word) or _FINITE_PRETERITE.fullmatch(word)
            or (_finite_lexeme(word, number, context="strong") and _strong_vp_frame(rest)))
    return False


def _np_span(text: str, *, role: str = "subject", outer_number: str | None = None) -> tuple[str, str] | None:
    """Consume a complete pronoun, determined, or compound/plural NP."""
    cache = _PARSE_CACHE.get()
    if cache is not None:
        key = ("np", text, role, outer_number)
        if key in cache:
            return cache[key]
        res = _np_span_uncached(text, role=role, outer_number=outer_number)
        cache[key] = res
        return res
    return _np_span_uncached(text, role=role, outer_number=outer_number)


def _np_span_uncached(text: str, *, role: str = "subject", outer_number: str | None = None) -> tuple[str, str] | None:
    first = _PHRASE_TOKEN.match(text)
    if not first:
        return None
    w0 = first.group()
    if _PRONOUN.fullmatch(w0):
        return text[first.end():].lstrip(), ("plural" if _PLURAL_PRONOUN.fullmatch(w0) else "singular")
    determiner = _DETERMINER.fullmatch(w0) is not None
    det_word = w0.lower() if determiner else ""
    if determiner:
        after0 = text[first.end():]
        if not after0 or not after0[0].isspace():
            return None
        pos = len(text) - len(after0.lstrip())
    else:
        pos = 0
    pre_modifiers, nominal_fallback, nominals = 0, None, []
    while tok := _PHRASE_TOKEN.match(text, pos):
        word, low = tok.group(), tok.group().lower()
        tail = text[tok.end():]
        rest = tail.lstrip()
        nxt_tok = _PHRASE_TOKEN.match(rest)
        nxt_low = nxt_tok.group().lower() if nxt_tok else ""
        if (_LEGACY_MENTION.match(text[pos:]) or _FUNCTION_WORD.fullmatch(low)
                or _AUXILIARY.fullmatch(low) or _DETERMINER.fullmatch(low)
                or _PRONOUN.fullmatch(low) or _PREDICATE_ONLY.fullmatch(low)
                or (_LY_VERB_FORM.fullmatch(low) and not determiner)
                or _FINITE_PRETERITE.fullmatch(low)
                or (_LEADING_ADVERBS.fullmatch(low) and not _LY_STEM.fullmatch(low))):
            break
        is_past_part = (_NONFINITE_PAST.fullmatch(low) or _REGULAR_ED.fullmatch(low)
                        or _SYNCRETIC_PARTICIPLE.fullmatch(low)) is not None
        is_pres_part = _is_present_participle(word) is not None
        if nominal_fallback is not None and (is_past_part or is_pres_part) and (
                _DETERMINER.fullmatch(nxt_low) or _PREPOSITION.match(rest)):
            break
        if nominals:
            if (_PURE_ADJECTIVE.fullmatch(low) or _REGULAR_ED.fullmatch(low)) and _medial_head(
                    text[pos:], outer_number if role in ("object", "agent") else "singular"):
                pre_modifiers += 1
                pos = len(text) - len(rest)
                continue
            if is_past_part or _NUMERIC_SPECIFIER.fullmatch(low) or _PURE_ADJECTIVE.fullmatch(low):
                break
            if is_pres_part and (_DETERMINER.fullmatch(nxt_low) or _PRONOUN.fullmatch(nxt_low) or _PARTICLE.match(rest)
                                 or re.match(r"(?:only|just|merely|solely|even)\b", rest, re.I)):
                break
        elif is_past_part or is_pres_part or _NUMERIC_SPECIFIER.fullmatch(low) or _PURE_ADJECTIVE.fullmatch(low):
            if determiner and (is_pres_part or _SYNCRETIC_PARTICIPLE.fullmatch(low)):
                nominal_fallback = tail if not tail or not tail[0].isspace() else rest
            pre_modifiers += 1
            if not tail or not tail[0].isspace():
                break
            pos = len(text) - len(rest)
            continue
        plural = _is_plural_nominal(low)
        rem = tail if not tail or not tail[0].isspace() else rest
        after_adv = _without_adverbs(rest, context="tail")
        part_after = (_NONFINITE_PAST.match(after_adv) or _is_present_participle(after_adv)
                      or _passive_by_phrase(after_adv) or _REGULAR_ED.match(after_adv)
                      or _SYNCRETIC_PARTICIPLE.match(after_adv))
        nxt_ok = (nxt_low != "" and not _LEGACY_MENTION.match(rest) and not _FUNCTION_WORD.fullmatch(nxt_low)
                  and not _AUXILIARY.fullmatch(nxt_low) and not _DETERMINER.fullmatch(nxt_low)
                  and not _PRONOUN.fullmatch(nxt_low) and not _PURE_ADJECTIVE.fullmatch(nxt_low)
                  and not (_LEADING_ADVERBS.fullmatch(nxt_low) and not _LY_STEM.fullmatch(nxt_low)))
        nxt_pl_head = nxt_ok and _is_plural_nominal(nxt_low) and not _SINGULAR_DETERMINER.fullmatch(det_word)
        nxt_compound_head = nxt_pl_head or (nxt_ok and (_MASS_HEAD.fullmatch(nxt_low) is not None or _NOMINAL_HEAD.fullmatch(nxt_low) is not None))
        prior_rem = nominals[-1][2] if nominals else nominal_fallback
        continuation = prior_rem is not None and _finite_frame(low, rest) and _vp_complement(rest, verb=low)
        headed = bool(nominals)
        embedded = role in ("object", "agent")
        agreement = outer_number if embedded else "singular"
        if (prior_rem is not None and embedded and part_after
                and _finite_lexeme(low, outer_number) and _np_span(rest, role="object") is not None):
            break
        if (prior_rem is not None and (embedded or not _PLURAL_DETERMINER.fullmatch(det_word))
                and _finite_lexeme(low, agreement, context="strong") and _strong_vp_frame(rest)):
            break
        if (prior_rem is not None and plural and _finite_frame(low, rest, agreement)
                and (embedded or not _PLURAL_DETERMINER.fullmatch(det_word))):
            b1, b2 = _base_from_3sg(low)
            if _SINGULAR_DETERMINER.fullmatch(det_word) or _VERBAL_STEM.fullmatch(b1) or _VERBAL_STEM.fullmatch(b2):
                break
            if not part_after and not _has_finite_vp(rest, number="plural") and (
                _LEGACY_MENTION.match(after_adv) or _np_span(after_adv, role="object") is not None
                or (nominals and role == "subject" and not after_adv.strip())
                or _CLAUSE_VERB.fullmatch(b1) or _CLAUSE_VERB.fullmatch(b2)
                or ((role == "subject" or embedded) and continuation)
            ):
                break
        if prior_rem is not None and not plural:
            if _VERBAL_STEM.fullmatch(low):
                break
            if role == "inverted" and _has_finite_vp(prior_rem, number="infinitive") and not _has_finite_vp(rem, number="infinitive"):
                break
            if (embedded and _finite_frame(low, rest, outer_number) and not part_after and (after_adv.strip() or continuation)
                    and _has_finite_vp(prior_rem, number=outer_number or "plural")
                    and not _has_finite_vp(rem, number=outer_number or "plural")
                    and not (_LEADING_ADVERBS.match(rest) and _has_finite_vp(rem, number="singular"))
                    and not _AUXILIARY.match(rest)):
                direct_obj = (not (rest == after_adv and nxt_pl_head and len(nominals) < 2
                                   and not _CLAUSE_VERB.fullmatch(low))
                              and _np_span(after_adv, role="object") is not None)
                if (_LEGACY_MENTION.match(after_adv) or direct_obj
                        or (_CLAUSE_VERB.fullmatch(low) and _PREPOSITION.match(rest))
                        or (continuation and headed)):
                    break
        if nominals and not plural and not determiner and pre_modifiers == 0 and not (
            part_after or not rest or rest[0] in ",.;:!?" or _MASS_HEAD.fullmatch(low) or nxt_compound_head
        ):
            break
        nominals.append((low, plural, rem))
        if plural or not tail or not tail[0].isspace():
            break
        pos = len(text) - len(rest)
    if nominals:
        _, last_plural, last_rem = nominals[-1]
        if _PLURAL_DETERMINER.fullmatch(det_word):
            last_plural = True
        elif _SINGULAR_DETERMINER.fullmatch(det_word):
            last_plural = False
        if not determiner and not last_plural and pre_modifiers == 0 and len(nominals) < 2:
            return None
        if last_plural and not determiner and pre_modifiers == 0 and len(nominals) >= 2:
            return last_rem, "bare_plural"
        return last_rem, ("plural" if last_plural else "singular")
    if nominal_fallback is not None:
        return nominal_fallback, ("plural" if _PLURAL_DETERMINER.fullmatch(det_word) else "singular")
    return None


def _noun_tail(text: str) -> str | None:
    """Return the tail of a complete pronoun, determined, or compound/plural NP."""
    span = _np_span(text, role="subject")
    return None if span is None else span[0]


def _pp_tail(text: str, *, outer_number: str | None = None) -> str | None:
    """Consume one prepositional phrase ('Prep + NP/nominal') and return its tail."""
    cache = _PARSE_CACHE.get()
    if cache is not None:
        key = ("pp", text, outer_number)
        if key in cache:
            return cache[key]
        res = _pp_tail_uncached(text, outer_number=outer_number)
        cache[key] = res
        return res
    return _pp_tail_uncached(text, outer_number=outer_number)


def _pp_tail_uncached(text: str, *, outer_number: str | None = None) -> str | None:
    prep = _PREPOSITION.match(text)
    if not prep:
        return None
    after = text[prep.end():].lstrip()
    if not after:
        return None
    np_role = "agent" if prep.group().lower() == "by" else "object"
    span = _np_span(after, role=np_role, outer_number=outer_number)
    if span is None and (focus := re.match(r"(?:only|just|merely|solely|even|almost|nearly)\b\s+", after, re.I)):
        span = _np_span(after[focus.end():], role=np_role, outer_number=outer_number)
    if span is not None:
        return span[0]
    word = _PHRASE_TOKEN.match(after)
    if (not word or not any(ch.isalpha() for ch in word.group())
            or _FUNCTION_WORD.fullmatch(word.group()) or _AUXILIARY.fullmatch(word.group())
            or _DETERMINER.fullmatch(word.group()) or _PRONOUN.fullmatch(word.group())
            or (_LEADING_ADVERBS.fullmatch(word.group()) and not _LY_STEM.fullmatch(word.group()))):
        return None
    return after[word.end():].lstrip()


def _passive_by_phrase(text: str) -> bool:
    """Whether a bare participle is licensed as passive by an agent 'by'-phrase."""
    cache = _PARSE_CACHE.get()
    if cache is not None:
        key = ("pb", text)
        if key in cache:
            return cache[key]
        res = _passive_by_phrase_uncached(text)
        cache[key] = res
        return res
    return _passive_by_phrase_uncached(text)


def _passive_by_phrase_uncached(text: str) -> bool:
    text = _SENTENCE_BOUNDARY.split(text, maxsplit=1)[0].split(",", 1)[0]
    stripped = _without_adverbs(text, context="tail")
    part = _NONFINITE_PAST.match(stripped) or _REGULAR_ED.match(stripped) or _SYNCRETIC_PARTICIPLE.match(stripped)
    if not part:
        return False
    after = _without_adverbs(stripped[part.end():].lstrip(), context="tail")
    if particle := _PARTICLE.match(after):
        after = _without_adverbs(after[particle.end():].lstrip(), context="tail")
    elif (stranded := _PREPOSITION.match(after)) and _pp_tail(after) is None:
        after = after[stranded.end():].lstrip()
    if (theme := _np_span(after, role="object")) is not None:
        after = _without_adverbs(theme[0].lstrip(), context="tail")
    while _PREPOSITION.match(after) and not re.match(r"by\b", after, re.I):
        if (nxt := _pp_tail(after)) is None:
            break
        after = _without_adverbs(nxt.lstrip(), context="tail")
    if not (by_prep := re.match(r"by\b", after, re.I)):
        return False
    return not _is_present_participle(after[by_prep.end():].lstrip()) and _pp_tail(after) is not None


def _np_modifier_step(text: str, *, outer_number: str | None = None) -> str | None:
    """Consume a single restrictive PP or reduced-participial post-modifier."""
    rest = _SENTENCE_BOUNDARY.split(text, maxsplit=1)[0].lstrip()
    stripped = _without_adverbs(rest, context="tail") if rest and rest[0] != "," and not _CLAUSE_LINKER.match(rest) else ""
    if not stripped or stripped[0] == "," or _CLAUSE_LINKER.match(stripped):
        return None
    if (pp := _pp_tail(stripped, outer_number=outer_number)) is not None:
        return pp.lstrip()
    pres = _is_present_participle(stripped)
    if not (part := _NONFINITE_PAST.match(stripped) or pres or _REGULAR_ED.match(stripped) or _SYNCRETIC_PARTICIPLE.match(stripped)):
        return None
    after = _without_adverbs(stripped[part.end():].lstrip(), context="tail")
    if _PREDICATIVE_PARTICIPLE.fullmatch(part.group()) and (state := _PREDICATIVE_STATE.match(after)):
        return after[state.end():].lstrip()
    if particle := _PARTICLE.match(after):
        after = _without_adverbs(after[particle.end():].lstrip(), context="tail")
    elif (stranded := _PREPOSITION.match(after)) and _pp_tail(after, outer_number=outer_number) is None:
        after = after[stranded.end():].lstrip()
    if (theme := _np_span(after, role="object", outer_number=outer_number)) is not None:
        return theme[0].lstrip()
    if pres is not None and (word := _PHRASE_TOKEN.match(after)):
        w = word.group()
        if (any(ch.isalpha() for ch in w) and not _LEGACY_MENTION.match(after)
                and not _FUNCTION_WORD.fullmatch(w) and not _AUXILIARY.fullmatch(w)
                and not _DETERMINER.fullmatch(w) and not _PRONOUN.fullmatch(w)
                and not _PREDICATE_ONLY.fullmatch(w) and not _VERBAL_STEM.fullmatch(w)
                and not _FINITE_PRETERITE.fullmatch(w) and not _REGULAR_ED.fullmatch(w)
                and not _NONFINITE_PAST.fullmatch(w) and _is_present_participle(w) is None
                and not (_LEADING_ADVERBS.fullmatch(w) and not _LY_STEM.fullmatch(w))):
            return after[word.end():].lstrip()
    return after


def _after_np_modifier(text: str, *, outer_number: str | None = None) -> str:
    """Advance past chained restrictive post-modifiers on a nominal head."""
    cur = _SENTENCE_BOUNDARY.split(text, maxsplit=1)[0].lstrip()
    while (nxt := _np_modifier_step(cur, outer_number=outer_number)) is not None and nxt != cur:
        cur = nxt
    return cur


def _predicate_tail(text: str, *, context: str = "tail", number: str = "") -> bool:
    """Recognize a lexical predicate position with number agreement."""
    stripped = _without_adverbs(text, context=context)
    token = _PHRASE_TOKEN.match(stripped)
    ok = bool(token and token.group()[0].isalpha() and not _LEGACY_MENTION.match(stripped)
              and (context == "clause" or (not _NONFINITE_PAST.match(stripped)
                                           and _is_present_participle(stripped) is None))
              and not _FUNCTION_WORD.fullmatch(token.group())
              and not _DETERMINER.fullmatch(token.group())
              and not _PRONOUN.fullmatch(token.group())
              and (not _PURE_ADJECTIVE.fullmatch(token.group())
                   or _finite_frame(token.group().lower(), stripped[token.end():].lstrip(), number or None)))
    if not ok:
        return False
    low = token.group().lower()
    if number == "infinitive":
        return not _is_3sg_verb(low) and not _SINGULAR_AUX.fullmatch(low)
    if not number or context == "clause":
        return True
    plural = number == "plural"
    if _SINGULAR_AUX.fullmatch(low):
        return not plural
    if _PLURAL_AUX.fullmatch(low):
        return plural
    if (_AUXILIARY.fullmatch(low) or (_PREDICATE_ONLY.fullmatch(low) and not low.endswith("s"))
            or _FINITE_PRETERITE.fullmatch(low) or _REGULAR_ED.fullmatch(low)
            or _SYNCRETIC_PARTICIPLE.fullmatch(low)):
        return True
    return _finite_frame(low, stripped[token.end():].lstrip(), number)


def _object_qualifier(text: str, *, context: str = "nominal") -> bool:
    """Recognize non-finite or passive-licensed participial NP post-modifiers."""
    stripped = _without_adverbs(text, context="tail")
    if _NONFINITE_PAST.match(stripped) or _is_present_participle(stripped) or _passive_by_phrase(stripped):
        return True
    if context == "finite":
        return False
    part = _REGULAR_ED.match(stripped) or _SYNCRETIC_PARTICIPLE.match(stripped)
    return bool(part and not _after_np_modifier(stripped).strip())


def _has_finite_vp(text: str, *, number: str = "") -> bool:
    """Check whether *text* after a subject NP supplies a finite predicate."""
    cache = _PARSE_CACHE.get()
    if cache is not None:
        key = ("fv", text, number)
        if key in cache:
            return cache[key]
    cur = _SENTENCE_BOUNDARY.split(text, maxsplit=1)[0].lstrip()
    res = False
    while True:
        if _predicate_tail(cur, number=number) and not _object_qualifier(cur, context="finite"):
            res = True
            break
        nxt = _np_modifier_step(cur, outer_number=number or None)
        if nxt is None or nxt == cur:
            break
        cur = nxt
    if cache is not None:
        cache[key] = res
    return res


def _clause_start(text: str) -> str:
    """Read comma-delimited adjuncts before testing subject/predicate order."""
    text = _without_adverbs(text)
    while True:
        prep = _PREPOSITION.match(text)
        sub = None if prep else _SUBORDINATOR.match(text)
        if not prep and not sub:
            break
        phrase, comma, rest = text.partition(",")
        following = rest.lstrip()
        if not comma or not following:
            break
        if prep and prep.group().lower() == "for":
            pp_rem = _pp_tail(phrase)
            if pp_rem is None:
                break
            complete = (not _after_np_modifier(pp_rem).strip()
                        or bool(re.match(r"(?:that|which|who)\b", pp_rem.lstrip(), re.I)))
            if (not complete or _CLAUSE_LINKER.match(following)
                or not (_clause_subject(following) or _pending_predicate(following))):
                break
        elif sub and _LEGACY_MENTION.match(following):
            break
        text = _without_adverbs(following)
    return text


def _clause_head(text: str) -> tuple[str, str]:
    """Return the subject position and the kind of initial coordinator."""
    text = _clause_start(text)
    connector = _COORDINATOR.match(text)
    if connector:
        kind = connector.group(1).lower()
        text = _clause_start(text[connector.end():])
        if kind == "nor":
            auxiliary = _PHRASE_TOKEN.match(text)
            if auxiliary and _AUXILIARY.fullmatch(auxiliary.group()):
                return text[auxiliary.end():].lstrip(), "nor_inv"
        return text, kind
    return text, ""


def _clause_subject(text: str) -> bool:
    """Recognize an independent finite clause in local technical prose."""
    cache = _PARSE_CACHE.get()
    if cache is not None:
        key = ("cs", text)
        if key in cache:
            return cache[key]
    head, kind = _clause_head(text)
    span = _np_span(head, role=("inverted" if kind == "nor_inv" else "subject"))
    if span is None:
        res = False
    else:
        agr = "infinitive" if kind == "nor_inv" else ("plural" if span[1].endswith("plural") else "singular")
        if span[1] == "bare_plural" and not _after_np_modifier(span[0], outer_number=agr).strip():
            res = False
        else:
            res = _has_finite_vp(span[0], number=agr)
    if cache is not None:
        cache[key] = res
    return res


def _pending_predicate(text: str) -> bool:
    if _clause_subject(text):
        return False
    text = _SENTENCE_BOUNDARY.split(text, maxsplit=1)[0]
    start = _clause_start(text)
    if _PREPOSITION.match(start):
        return True
    if connector := _COORDINATOR.match(start):
        start = _clause_start(start[connector.end():])
        if _PREPOSITION.match(start):
            return True
    return (_predicate_tail(start, context="clause") if start != text
            else bool(_PREDICATE_FRAGMENT.match(start)))
