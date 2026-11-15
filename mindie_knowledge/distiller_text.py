"""Semantic TorchAir/GE -> ACLGraph normalization that preserves negations.

[Role]
Rewrite only positively asserted legacy vocabulary, preserve the original
text, and classify each passage as aclgraph_native, adapted-from-TorchAir,
runtime-agnostic kernel knowledge, or a preserved TorchAir-only constraint.
[Invariants]
A Chinese or English legacy negation such as "SuperKernel 仅支持 ge_graph 模式，
不支持 aclgraph。" is preserved verbatim and classified torchair_legacy_only.
No substitution may turn "仅支持 ge_graph，不支持 aclgraph" into a self-
contradictory "仅支持 aclgraph，不支持 aclgraph". Pure kernel SRAM rules are
operator_kernel_agnostic regardless of the ACLGraph runtime vocabulary.
URI spans retain their exact bytes during every positive substitution.
[Timeouts & Locks]
Pure regex/string processing; no I/O, locks, waits, or subprocesses.
[Side-Effect Policy]
No side effects; the input string is never mutated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .distiller_lexicon import _PARSE_CACHE
from .distiller_clause import (
    _LEGACY_MENTION, _HARD_BREAK, _SENTENCE_BOUNDARY, _PREDICATE_WORD, _PREDICATE_ONLY,
    _ADVERB_WORD, _LEADING_ADVERBS, _LY_PREFIX, _LY_STEM, _LY_VERB_FORM, _FUNCTION_WORD,
    _PREPOSITION, _SUBORDINATOR, _DETERMINER, _PLURAL_DETERMINER, _SINGULAR_DETERMINER,
    _PRONOUN, _PLURAL_PRONOUN, _AUXILIARY, _SINGULAR_AUX, _PLURAL_AUX, _PHRASE_TOKEN,
    _NONFINITE_PAST, _FINITE_PRETERITE, _REGULAR_ED, _SYNCRETIC_PARTICIPLE, _FINITE_ING,
    _PRESENT_PARTICIPLE, _PREDICATIVE_PARTICIPLE, _PREDICATIVE_STATE, _PARTICLE, _COORDINATOR,
    _CLAUSE_LINKER, _DEGREE_INTENSIFIER, _DEGREE_COMPLEMENT, _NUMERIC_SPECIFIER,
    _PURE_ADJECTIVE, _VERBAL_STEM, _CLAUSE_VERB, _MASS_HEAD, _VERBAL_ER, _NOMINAL_HEAD,
    _IRREGULAR_PLURAL, _SINGULAR_S, _INITIALISM_PLURAL, _PREDICATE_FRAGMENT, _is_plural_nominal,
    _is_3sg_verb, _degree_end, _without_adverbs, _is_present_participle, _base_from_3sg,
    _vp_complement, _np_span, _noun_tail, _pp_tail, _passive_by_phrase, _np_modifier_step,
    _after_np_modifier, _predicate_tail, _object_qualifier, _has_finite_vp, _clause_start,
    _clause_head, _clause_subject, _pending_predicate, _imperative_clause,
)



@dataclass(frozen=True, slots=True)
class NormalizationRule:
    rule_id: str
    pattern: str
    replacement: str
    applicability: str
    note: str


NORMALIZATION_RULES: tuple[NormalizationRule, ...] = (
    NormalizationRule("N1", r'exe_mode\s*:\s*["\']ge_graph["\']', 'exe_mode: "aclgraph"',
                      "aclgraph_adapted_from_torchair", "ACLGraph replays NPUGraph inside vLLM-Ascend"),
    NormalizationRule("N2", r"(?i)super\s*kernel\s+does\s+not\s+support\s+aclgraph",
                      "ACLGraph supports SuperKernel via aclgraph.super_kernel_optimize",
                      "aclgraph_adapted_from_torchair", "SuperKernel is ACLGraph-native in vLLM-Ascend"),
    NormalizationRule("N3", r"npu_create_tagged_stream", "npu_stream_switch + RoutedMoEMilestones",
                      "aclgraph_adapted_from_torchair", "tagged streams become explicit npu_stream_switch at milestones"),
    NormalizationRule("N4", r"(?i)Notify_Wait", "EventRecord/EventWait (ACLGraph capture order is explicit)",
                      "aclgraph_adapted_from_torchair", "ACLGraph capture order is explicit; never blame GE pass reordering"),
    NormalizationRule("N5", r"torchair\.scope\.npu_stream_switch", "vllm_ascend.utils.npu_stream_switch",
                      "aclgraph_adapted_from_torchair", "ACLGraph uses the vLLM-Ascend stream switch helper"),
    NormalizationRule("N6", r"limit_core_num", "ACLGraph static block dims / HCCL CCU_SCHED",
                      "aclgraph_adapted_from_torchair", "core limiting is expressed through captured block dims"),
    NormalizationRule("N7", r"ge_graph", "aclgraph", "aclgraph_adapted_from_torchair",
                      "positive GE graph vocabulary is normalized to ACLGraph"),
)

_RESTRICTIVE_POLARITY = re.compile(
    r"\b(?:only|exclusive|exclusively|sole|solely|limited\s+to|restricted\s+to|"
    r"specific\s+to|but|except|without|not|never|no|neither|nor|unsupported|"
    r"unavailable|incompatible|cannot|fails?|forbidden|prohibited|rarely|seldom|rejected|broken|"
    r"avoid(?:s|ed|ing)?|non-[a-z0-9_]+|unless|instead\s+of|rather\s+than|"
    r"(?:do|does|did)\s+not|(?:can|don|doesn|shouldn|mustn)['’]t|"
    r"disallow(?:s|ed|ing)?|deprecated)\b|n['’]t\b|"
    r"仅|只|唯|独|专有|专属|限于|局限于|不支持|不可|不能|无法|未支持|"
    r"不兼容|不适用|禁用|除外|而非|而不是|但|却|"
    r"避免|不要|切勿|请勿|勿|禁止|严禁|不受支持|不适合|不宜|不得|不应|除非|代替|替代", re.I)
_ABBREVIATION = re.compile(
    r"(?<![\w.])(?:e\.g|i\.e|vs|cf|figs?|secs?|refs?|nos|ch|eq|al|approx|min|max)\.(?!\w)", re.I)
_NUMBER_ABBREVIATION = re.compile(r"(?<![\w.])no\.(?=\s*\d)", re.I)
_NUMBER_CONTEXT_BOUNDARY = re.compile(r"\.(?!\w)|[。!！]|" + _HARD_BREAK)
_ANSWER_CONTEXT = re.compile(r"(?:[:：?？=—;；,，]|--|\b(?:is|was|are|were))\s*$", re.I)
_NUMBER_LABEL = re.compile(
    r"\b(?:configurations?|configs?|options?|items?|cases?|modes?|rules?|steps?|"
    r"sections?|issues?|entries|entry|settings?|parameters?|variants?|choices?|"
    r"references?|examples?|figures?|tables?|chapters?|versions?|revisions?)"
    r"\s*(?:(?:[:：]|is|was|are|were)\s*)?$", re.I)
_CARDINAL_CLAUSE = re.compile(
    r"\s*\d+\s+(?!(?:is|was|are|were|uses?|with|for|in|of|on|at|from|to)\b)"
    r"(?:[a-z][\w'-]*\s+){1,4}(?:is|are|was|were|has|have|had|can|could|will|would|"
    r"must|should|run|exist|remain|use|execute|operate|work|need|require)\b", re.I)
_ETC_ABBREVIATION = re.compile(r"(?<![\w.])etc\.(?!\w)", re.I)
_CONTINUATION = re.compile(
    r"(?:such\s+as|including|especially|particularly|specifically|namely|"
    r"for\s+example|for\s+instance|and|or|nor|as\s+well\s+as)\b|"
    r"(?:e\.g\.|i\.e\.)(?!\w)|例如|比如|诸如|包括|包含|尤其是?|特别是?|"
    r"以及|或者|还有|乃至|甚至|含|即|及|和|与|或|如(?=\s|[:：,，]|[a-z_])", re.I)
_SOFT_SPACE = r"[ \t]*(?:\r?\n[ \t]*)?"
_MARKDOWN_NO = re.compile(
    r"(?<![\w\\*_])(?P<em>\*{1,3}|_{1,3})(?![*_])" + _SOFT_SPACE +
    r"no" + _SOFT_SPACE + r"(?:\." + _SOFT_SPACE + r"(?P=em)|(?P=em)\.)(?![\w*_])", re.I)
_NEGATIVE_REPLY = re.compile(r"[?？;；](?=" + _SOFT_SPACE + r"no\.(?!\w))", re.I)
_INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)(?:(?!" + _HARD_BREAK + r").)*?(?<!`)\1(?!`)", re.S)
_URI = re.compile(r"(?<![\w.+-])[a-zA-Z][a-zA-Z0-9+.-]*:[a-zA-Z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
_KERNEL_TOKENS = ("UB", "SIMD", "SIMT", "SSBUF", "SRAM", "L0C", "L0A", "L0B",
                  "Fixpipe", "AscendC", "RingBuffer", "buffer")
_ACL_TOKENS = ("ACLGraph", "NPUGraph", "super_kernel_optimize", "aclgraph",
               "join_after_forward", "npu_stream_switch")


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    text: str
    applied: tuple[str, ...]
    applicability: str
    audit: tuple[Mapping[str, str], ...] = ()
    source_text: str = ""
    classification: str = ""
    preserved_legacy: bool = False
    details: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.preserved_legacy, bool):
            raise ValueError("preserved_legacy must be bool")
        if not self.source_text:
            object.__setattr__(self, "source_text", self.text)
        if not self.classification:
            object.__setattr__(self, "classification", self.applicability)
        if self.applicability not in ("aclgraph_native", "aclgraph_adapted_from_torchair",
                                      "operator_kernel_agnostic", "torchair_legacy_only"):
            raise ValueError(f"invalid applicability: {self.applicability!r}")


def _number_abbreviation(match: re.Match[str]) -> str:
    following = match.string[match.end():]
    number, hard_break = re.match(r"\s*\d+", following), re.search(_HARD_BREAK, following)
    before = _NUMBER_CONTEXT_BOUNDARY.split(match.string[:match.start()])[-1]
    if hard_break and hard_break.start() < number.end():
        return match.group()
    if _NUMBER_LABEL.search(before):
        return "__abbr__"
    return match.group() if (_ANSWER_CONTEXT.search(before) or _CARDINAL_CLAUSE.match(following)) else "__abbr__"


def _reply_context(text: str) -> str:
    """Read emphasis as presentation after literal spans have been masked."""
    return _NEGATIVE_REPLY.sub(" ", _MARKDOWN_NO.sub("no.", text))


def _code_context(match: re.Match[str]) -> str:
    mentions = _LEGACY_MENTION.findall(match.group())
    return " " + (" ".join(mentions) if mentions else "__code__") + " "


def _link_destination(text: str, start: int) -> bool:
    """Whether the URI follows a balanced Markdown label's ]( opener."""
    if text[max(0, start - 2):start] != "](" or (start - 2 - len(text[:start - 2].rstrip("\\"))) % 2:
        return False
    floor = max((m.end() for m in re.finditer(_HARD_BREAK, text[:start - 2])), default=0)
    depth = 1
    for index in range(start - len("](") - 1, floor - 1, -1):
        if text[index] not in "[]" or (index - len(text[:index].rstrip("\\"))) % 2:
            continue
        depth += 1 if text[index] == "]" else -1
        if depth == 0:
            return True
    return False


def _uri_parts(text: str) -> list[str]:
    """Alternate prose/URI slices, balancing enclosing prose/link brackets."""
    parts, end = [], 0
    while match := _URI.search(text, end):
        stop, brackets = match.end(), []
        explicit = text[match.start()-1:match.start()] == "<" and text[stop:stop+1] == ">"
        wrapper = {"(": ")", "[": "]"}.get(text[match.start()-1:match.start()])
        for index in range(match.start(), stop):
            char = text[index]
            if char in "([":
                brackets.append(char)
            elif char in ")]":
                if brackets and brackets[-1] == {")": "(", "]": "["}[char]:
                    brackets.pop()
                elif char == wrapper:
                    stop = index
                    explicit = char == ")" and _link_destination(text, match.start())
                    break
        if not explicit:
            stop = match.start() + len(text[match.start():stop].rstrip(".,;:!?"))
        parts.extend((text[end:match.start()], text[match.start():stop]))
        end = stop
    return parts + [text[end:]]


def _capitalized_api_label(text: str) -> bool:
    """Recognize an explicit capitalized introducer and a bare API label."""
    first = _PHRASE_TOKEN.match(text)
    if not first or not first.group()[0].isupper() or not any(
            p.fullmatch(first.group()) for p in (_DETERMINER, _PREPOSITION, _SUBORDINATOR)):
        return False
    rest = text[first.end():].strip()
    return bool(_LEGACY_MENTION.match(rest) and re.fullmatch(r"[a-z_][\w.]*[。!?！？]?", rest, re.I))


def _is_legacy_claim(text: str) -> bool:
    if not _LEGACY_MENTION.search(text):
        return False
    scoped = _INLINE_CODE.sub(_code_context, text)
    scoped = _ABBREVIATION.sub("__abbr__", scoped)
    for match in _NUMBER_ABBREVIATION.finditer(scoped):
        following = scoped[match.end():]
        reference = re.match(r"\s*\d+\s*[:：]", following)
        if reference and not re.search(_HARD_BREAK, following[:reference.end()]):
            before = _SENTENCE_BOUNDARY.split(scoped[:match.start()])[-1]
            target = _SENTENCE_BOUNDARY.split(following)[0]
            if _RESTRICTIVE_POLARITY.search(before) and _LEGACY_MENTION.search(target):
                return True
    scoped = _reply_context(_NUMBER_ABBREVIATION.sub(_number_abbreviation, scoped))
    if not _RESTRICTIVE_POLARITY.search(scoped) or not _LEGACY_MENTION.search(scoped):
        return False
    token = _PARSE_CACHE.set({}) if _PARSE_CACHE.get() is None else None
    try:
        parts, clause, end = [], "", 0
        for match in _ETC_ABBREVIATION.finditer(scoped):
            before = scoped[end:match.start()]
            parts.append(before)
            clause = _SENTENCE_BOUNDARY.split(clause + before)[-1]
            end = match.end()
            following = scoped[end:].lstrip()
            closes_sentence = False
            if not _LEGACY_MENTION.match(following):
                cjk_start = re.match(r"[\u3400-\u9fff]", following)
                pending_subject = bool(
                    _LEGACY_MENTION.search(clause) and not _RESTRICTIVE_POLARITY.search(clause)
                )
                predicate = pending_subject and bool(cjk_start or _pending_predicate(following))
                if not predicate:
                    if _clause_subject(following):
                        closes_sentence = True
                    elif not _CONTINUATION.match(following):
                        if _capitalized_api_label(following):
                            closes_sentence = True
                        else:
                            head, _ = _clause_head(following)
                            if cjk_start or _imperative_clause(head):
                                fragment = (
                                    not head or re.match(r"for\b", _clause_start(following), re.I)
                                    or _noun_tail(head) is not None or _NONFINITE_PAST.match(head)
                                    or _REGULAR_ED.match(head) or _SYNCRETIC_PARTICIPLE.match(head)
                                    or _is_present_participle(head) is not None or _passive_by_phrase(head)
                                )
                                closes_sentence = not fragment
            parts.append("." if closes_sentence else "__abbr__")
            clause = "" if closes_sentence else clause + "__abbr__"
        scoped = "".join(parts) + scoped[end:]
    finally:
        if token is not None:
            _PARSE_CACHE.reset(token)
    return any(_LEGACY_MENTION.search(sentence) and _RESTRICTIVE_POLARITY.search(sentence)
               for sentence in _SENTENCE_BOUNDARY.split(scoped))


def normalize_text(text: str) -> NormalizationResult:
    """Apply guarded normalization while retaining the original claim."""
    source = str(text)
    parts = _uri_parts(source)
    if _is_legacy_claim("".join(part if i % 2 == 0 else "__uri__" for i, part in enumerate(parts))):
        return NormalizationResult(
            text=source, applied=(), applicability="torchair_legacy_only",
            audit=({"rule": "LEGACY", "note": "preserved restrictive or negative legacy claim", "count": "1"},),
            source_text=source, classification="torchair_legacy_only", preserved_legacy=True,
            details={"reason": "sentence polarity forbids backend substitution"})
    applied, audit = [], []
    for rule in NORMALIZATION_RULES:
        count = 0
        for i in range(0, len(parts), 2):
            parts[i], matches = re.subn(rule.pattern, rule.replacement, parts[i])
            count += matches
        if count:
            applied.append(rule.rule_id)
            audit.append({"rule": rule.rule_id, "note": rule.note, "count": str(count)})
    current = "".join(parts)
    if applied:
        return NormalizationResult(current, tuple(applied), "aclgraph_adapted_from_torchair",
                                   tuple(audit), source_text=source, classification="aclgraph_adapted_from_torchair")
    lowered = current.lower()
    if any(token.lower() in lowered for token in _ACL_TOKENS):
        applicability = "aclgraph_native"
    elif any(token.lower() in lowered for token in _KERNEL_TOKENS) or "torchair" not in lowered:
        applicability = "operator_kernel_agnostic"
    else:
        applicability = "torchair_legacy_only"
    return NormalizationResult(current, (), applicability, (), source_text=source,
                               classification=applicability, details={"reason": "no positive legacy vocabulary matched"})


def normalize_texts(texts: Iterable[str]) -> tuple[NormalizationResult, ...]:
    return tuple(normalize_text(text) for text in texts)


__all__ = [
    "NORMALIZATION_RULES", "NormalizationResult", "NormalizationRule",
    "normalize_text", "normalize_texts",
]
