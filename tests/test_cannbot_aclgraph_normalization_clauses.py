"""Compositional clause-ownership, participle, adverb, and predicate normalization tests.

[Role]
Verify subjectless pending predicates after `etc.`, technical numeric/dtype
subjects, `-ly` verb vs adverb disambiguation, coordinated predicates, sentence
adverbs with finite past/present clauses, recipient passives, fronted PPs, and
case invariance across `normalize_text` and `normalize_texts`.
[Invariants]
All oracle expectations are independent string literals; singleton and batch
normalization match byte-for-byte across wraps and casing permutations.
[Timeouts & Locks]
Pure in-memory string normalization (< 3s); no I/O, locks, or subprocesses.
[Side-Effect Policy]
Side-effect-free unit test module; mutates no files, globals, or environment.
"""

from __future__ import annotations

from itertools import product

import pytest

from mindie_knowledge.distiller_text import normalize_text, normalize_texts

LEGACY_APIS = (
    ("ge_graph", "aclgraph", "N7"),
    ("torchair.scope.npu_stream_switch", "vllm_ascend.utils.npu_stream_switch", "N5"),
    ("Notify_Wait", "EventRecord/EventWait (ACLGraph capture order is explicit)", "N4"),
    ("npu_create_tagged_stream", "npu_stream_switch + RoutedMoEMilestones", "N3"),
    ("limit_core_num", "ACLGraph static block dims / HCCL CCU_SCHED", "N6"),
)


def _check_result(result, source, expected, applied=(), preserve=False):
    assert result.text.encode() == expected.encode(), (source, result)
    assert result.source_text.encode() == source.encode()
    assert result.preserved_legacy is preserve and result.applied == applied
    if preserve or applied:
        classification = "torchair_legacy_only" if preserve else "aclgraph_adapted_from_torchair"
        assert result.classification == result.applicability == classification
    return result


def _assert_norm_result(source, expected, applied=(), preserve=False, *, check_batch=False):
    result = _check_result(normalize_text(source), source, expected, applied, preserve)
    if check_batch:
        assert normalize_texts((source,)) == (result,)
    return result


def _assert_norm_rows(rows, api, target, rule):
    singles = normalize_texts(source for source, _ in rows)
    for (source, preserve), result in zip(rows, singles):
        expected = source if preserve else source.replace(api, target)
        _check_result(result, source, expected, () if preserve else (rule,), preserve)
        assert [(a["rule"], a["count"]) for a in result.audit] == [
            ("LEGACY", "1") if preserve else (rule, str(source.count(api)))
        ]
    if rows:
        assert normalize_text(rows[0][0]) == singles[0]
        assert normalize_texts((rows[-1][0],)) == (singles[-1],)


@pytest.mark.parametrize("api,target,rule", LEGACY_APIS)
@pytest.mark.parametrize("case", [str.lower, str.capitalize, str.upper])
@pytest.mark.parametrize("gap", [" ", "\n", "\r\n"])
def test_pending_predicates_have_modifiers_but_no_subject(api, target, rule, case, gap):
    for tail in ("never really work", "only ever execute", "no longer fully supported",
                 "still completely unsupported", "always immediately fail",
                 "only partially allowed", "never reliably vectorize",
                 "rarely asynchronously dispatch", "seldom successfully enqueue",
                 "not yet entirely compiled", "only strictly serialized",
                 "still currently unavailable", "still generally prohibited",
                 "still unexpectedly rejected", "still totally broken",
                 "no longer remotely portable", "only very occasionally specialize",
                 "only deliberately apply buffers", "never readily comply with capture constraints"):
        text = f"Use ge_graph. Legacy APIs such as {api}, etc.{gap}"
        text += case(tail).replace(" ", gap) + ". Use limit_core_num.\t"
        _assert_norm_result(text, text, preserve=True)
    for tail in ("only CPU kernels fail", "only the dispatcher stalls", "only they fail",
                 "only 4K tensors fail", "only supported kernels fail",
                 "only 32-bit fused kernel operators fail"):
        text = f"Legacy APIs such as {api}, etc.{gap}{case(tail).replace(' ', gap)}."
        _assert_norm_result(text, text.replace(api, target), (rule,))
    text = f"Legacy APIs such as {api}, etc.{gap}{case('always fully execute')}."
    _assert_norm_result(text, text.replace(api, target), (rule,))


@pytest.mark.parametrize("api,target,rule", LEGACY_APIS)
@pytest.mark.parametrize("case", [str.lower, str.title, str.upper])
@pytest.mark.parametrize("gap", [" ", "\n", "\r\n"])
def test_technical_subjects_do_not_depend_on_verb_whitelists(api, target, rule, case, gap):
    for subject in ("2D kernels", "3D convolutions", "64-bit pointers", "16x16 tiles",
                    "4K tensors", "FP16 buffers", "INT8 streams", "32-bit operators",
                    "8-bit ops", "2 layers", "FP32 matrices", "8x8 inputs", "4 outputs",
                    "BF16 graphs", "1D modes", "INT4 paths", "2D executions",
                    "3 launches", "32x8 fused kernels", "12-bit kernel operators"):
        for predicate in ("execute using", "remain valid with", "rely on", "adopt",
                          "enable", "call", "invoke", "interoperate with",
                          "dispatch through", "can independently specialize for", "comply with"):
            clause = case(f"{subject} {predicate}").replace(" ", gap)
            text = f"Avoid allocations, locks, etc.{gap}{clause} {api}."
            _assert_norm_result(text, text.replace(api, target), (rule,))
    for tail in (api, "17: " + api, "2D kernels, " + api, "FP16 buffers with " + api):
        text = f"Avoid legacy APIs, etc.{gap}{tail}."
        _assert_norm_result(text, text, preserve=True)


@pytest.mark.parametrize("api,target,rule", LEGACY_APIS)
@pytest.mark.parametrize("case", [str.lower, str.capitalize, str.upper])
@pytest.mark.parametrize("gap", [" ", "\n", "\r\n"])
def test_bare_predicates_cannot_be_technical_np_modifiers(api, target, rule, case, gap):
    rows = []
    for (verb, head), specifier, ending in product(
        (("run", "kernels"), ("support", "kernels"), ("use", "buffers"),
         ("call", "operators"), ("enable", "streams"), ("compile", "graphs")),
        ("", "2D ", "FP16 "),
        ("quickly.", "directly, and retain capture.", "safely;", "only.",
         "compiled for capture.", "used in decode.", "captured by runtime."),
    ):
        tail = case(f"never {verb} {specifier}{head} {ending}").replace(" ", gap)
        text = f"Use ge_graph. Legacy APIs such as {api}, etc.{gap}{tail} Use limit_core_num."
        rows.append((text, True))
    for modifier in ("only", "never", "always", "directly"):
        tail = case(f"{modifier} support kernels compiled for capture.").replace(" ", gap)
        rows.append((f"Legacy APIs such as {api}, etc.{gap}{tail}", modifier in ("only", "never")))
    _assert_norm_rows(rows, api, target, rule)


@pytest.mark.parametrize("api,target,rule", LEGACY_APIS)
@pytest.mark.parametrize("case", [str.lower, str.capitalize, str.upper])
@pytest.mark.parametrize("gap", [" ", "\n", "\r\n"])
def test_ly_predicates_keep_determined_and_qualified_objects(api, target, rule, case, gap):
    rows = []
    for (verb, head), determiner, specifier, ending in product(
        (("apply", "buffers"), ("multiply", "tensors"), ("supply", "inputs"), ("imply", "graphs")),
        ("the", "these", "our", "their"), ("", "2D ", "BF16 "),
        ("", " quickly", " compiled for capture", " only"),
    ):
        predicate = f"{verb} {determiner} {specifier}{head}"
        tail = case("never " + predicate + ending).replace(" ", gap)
        rows.append((f"Legacy APIs such as {api}, etc.{gap}{tail}.", True))
        clause = case("2D kernels " + predicate + " via").replace(" ", gap)
        suffix = case(ending).replace(" ", gap)
        rows.append((f"Avoid allocations, locks, etc.{gap}{clause} {api}{suffix}.", ending == " only"))
    for predicate in ("rely on the 2D buffers", "comply with these FP16 operators"):
        tail = case("never " + predicate + " quickly").replace(" ", gap)
        rows.append((f"Legacy APIs such as {api}, etc.{gap}{tail}.", True))
    _assert_norm_rows(rows, api, target, rule)


@pytest.mark.parametrize("api,target,rule", LEGACY_APIS)
@pytest.mark.parametrize("case", [str.lower, str.capitalize, str.upper])
@pytest.mark.parametrize("gap", [" ", "\n", "\r\n"])
def test_productive_adverbs_retain_independent_subjects(api, target, rule, case, gap):
    rows = []
    for modifier, subject, (predicate, ending) in product(
        ("typically", "normally", "explicitly", "only"),
        ("kernels", "2D kernels", "the custom kernels", "these 2D operators", "our FP16 kernels",
         "NPU kernels", "I/O threads", "32-bit fused kernel operators", "compiled kernels"),
        (("run", " quickly"), ("apply these 2D buffers", " quickly"),
         ("supply their FP16 inputs", ""), ("multiply our tensors compiled for capture", "")),
    ):
        tail = case(f"{modifier} {subject} {predicate}").replace(" ", gap)
        suffix = case(ending).replace(" ", gap)
        rows.append((f"Legacy APIs such as {api}, etc.{gap}{tail}{suffix}.", False))
        rows.append((f"Avoid allocations, locks, etc.{gap}{tail} via {api}{suffix}.", modifier == "only"))
    _assert_norm_rows(rows, api, target, rule)


@pytest.mark.parametrize("api,target,rule", LEGACY_APIS)
@pytest.mark.parametrize("case", [str.lower, str.capitalize, str.upper])
@pytest.mark.parametrize("gap", [" ", "\n", "\r\n"])
def test_coordinated_ly_predicates_and_terminal_adverbs(api, target, rule, case, gap):
    rows = []
    for verb, adv, connector in product(
        ("apply", "reapply", "resupply", "premultiply"), ("quickly", "rapidly", "normally"),
        ("and", "or", "but", "nor", "yet", "while", "because", "if", "unless", "as"),
    ):
        ending = (f"{connector} retain capture" if connector in ("and", "or", "but", "nor", "yet")
                  else f"{connector} the streams run")
        tail = case(f"never {verb} the buffers {adv} {ending}.").replace(" ", gap)
        rows.append((f"Use ge_graph. Legacy APIs such as {api}, etc.{gap}{tail} Use limit_core_num.", True))
        tail = case(f"only the kernels {verb} the buffers {adv} {ending}.").replace(" ", gap)
        rows.append((f"Legacy APIs such as {api}, etc.{gap}{tail}", False))
        if connector in ("and", "or", "yet"):
            tail = case(f"2D kernels {verb} the buffers {adv} {ending} via").replace(" ", gap)
            rows.append((f"Avoid allocations, locks, etc.{gap}{tail} {api}.", False))
    for predicate, connector in product(
        ("apply", "reapply quickly", "resupply", "premultiply", "rely on buffers",
         "comply with streams", "reply to operators"), ("and", "or", "yet"),
    ):
        for prefix, ending in (("never", "retain capture"), ("always", "never allocate tensors")):
            tail = case(f"{prefix} {predicate} {connector} {ending}.").replace(" ", gap)
            rows.append((f"Legacy APIs such as {api}, etc.{gap}{tail}", True))
        tail = case(f"2D kernels {predicate} {connector} retain capture via").replace(" ", gap)
        rows.append((f"Avoid allocations, locks, etc.{gap}{tail} {api}.", False))
    for connector in ("and", "or"):
        rows.append((f"Legacy APIs such as {api}, etc. Never rely on {api} {connector} allocate buffers.", True))
    _assert_norm_rows(rows, api, target, rule)


@pytest.mark.parametrize("api,target,rule", LEGACY_APIS)
@pytest.mark.parametrize("case", [str.lower, str.capitalize, str.upper])
@pytest.mark.parametrize("gap", [" ", "\n", "\r\n"])
def test_sentence_adverbs_keep_finite_past_and_present_clauses(api, target, rule, case, gap):
    rows = []
    for adverb, predicate in product(
        ("normally", "typically", "usually", "locally", "automatically", "manually",
         "actively", "cautiously", "independently", "constantly", "repeatedly", "seemingly",
         "reliably", "visibly", "outwardly", "unwisely", "quickly", "explicitly", "simply"),
        ("applied these buffers", "multiplied 2D tensors", "supplied capture streams",
         "reapplied our buffers", "resupplied the inputs", "premultiplied matrices",
         "relied on these buffers", "complied with these streams", "replied to these operators",
         "apply these buffers", "reapply the buffers", "have supplied inputs", "are applying buffers"),
    ):
        tail = case(f"{adverb} the 2D kernels {predicate}").replace(" ", gap)
        rows.append((f"Legacy APIs such as {api}, etc.{gap}{tail} only.", False))
        rows.append((f"Avoid allocations, locks, etc.{gap}{tail} via {api}.", False))
    for predicate in ("applies these buffers", "reapplies inputs", "resupplies streams",
                      "relies on buffers", "replies to operators", "complies with streams",
                      "applied buffers", "relied on streams", "applying buffers", "relying on streams"):
        tail = case(predicate).replace(" ", gap)
        rows.append((f"Legacy APIs such as {api}, etc.{gap}{tail} only.", True))
        subject = "the kernel is " if "lying" in predicate else "the kernel "
        clause = case("quickly " + subject + predicate).replace(" ", gap)
        rows.append((f"Legacy APIs such as {api}, etc.{gap}{clause} only.", False))
    _assert_norm_rows(rows, api, target, rule)


@pytest.mark.parametrize("api,target,rule", LEGACY_APIS)
@pytest.mark.parametrize("case", [str.lower, str.capitalize, str.upper])
@pytest.mark.parametrize("gap", [" ", "\n", "\r\n"])
def test_passive_and_active_object_qualifiers(api, target, rule, case, gap):
    rows = []
    for verb, qualifier in product(
        ("apply", "reapply", "resupply", "premultiply"),
        ("allocated via capture", "pinned on NPU", "pinned to streams", "compiled in capture",
         "supplied by runtime", "reapplied for capture", "chosen", "frozen safely",
         "using streams", "applying inputs", "supplying streams", "relying on streams"),
    ):
        tail = case(f"never {verb} the buffers {qualifier}.").replace(" ", gap)
        rows.append((f"Legacy APIs such as {api}, etc.{gap}{tail}", True))
        tail = case(f"2D kernels {verb} the buffers {qualifier} via").replace(" ", gap)
        rows.append((f"Avoid allocations, locks, etc.{gap}{tail} {api}.", False))
    for prep in ("on", "with", "to", "into", "in", "for", "from", "by", "at", "upon", "via",
                 "through", "over", "under", "across", "within", "without"):
        tail = case(f"never apply {prep} the buffers and retain capture.").replace(" ", gap)
        rows.append((f"Legacy APIs such as {api}, etc.{gap}{tail}", True))
    _assert_norm_rows(rows, api, target, rule)


@pytest.mark.parametrize("api,target,rule", LEGACY_APIS)
@pytest.mark.parametrize("case", [str.lower, str.capitalize, str.upper])
@pytest.mark.parametrize("gap", [" ", "\n", "\r\n"])
def test_modifiers_qualifiers_and_coordination_compose(api, target, rule, case, gap):
    rows = []

    def add(tail, preserve, prefix="Legacy APIs such as {a}, etc."):
        rows.append((prefix.format(a=api) + gap + case(tail).replace(" ", gap), preserve))

    for sentence, adv, focus, qualifier in product(
        ("perhaps", "maybe", "otherwise", "meanwhile", "however", "indeed"),
        ("simply", "amply", "deeply", "sharply", "triply"),
        ("only", "directly", "merely"), ("now supplied by runtime", "already applying inputs"),
    ):
        obj = f"{focus} the buffers {qualifier} {adv} yet retain capture."
        add(f"{sentence} never apply {obj}", True)
        add(f"{sentence} the kernels applied {obj}", False)
        add(f"{sentence} always supply {obj.replace('retain capture', 'never allocate tensors')}", True)
    for verb, ending in product(
        ("apply", "supply", "reply", "multiply", "rely", "comply", "imply", "fly", "ally",
         "bully", "rally", "tally", "dally", "sally", "ply", "reapply", "misapply", "overapply",
         "underapply", "resupply", "oversupply", "undersupply", "premultiply", "overfly",
         "outfly", "noncomply"), (".", " and retain capture.", " yet retain capture."),
    ):
        add(f"never {verb} simply{ending}", True)
        add(f"only the kernels {verb} amply{ending}", False)
    for adv, connector in product(("steeply", "cheaply", "crisply"),
                                  ("and", "or", "but", "nor", "yet", ", and", ", yet")):
        add(f"never apply the buffers {adv} {connector} retain capture.", True)
    for modifier, predicate in product(
        ("quickly", "briefly", "simply"),
        ("applied only these buffers", "reply only to the operators", "replied directly to operators",
         "worked", "worked just in capture", "have replied merely to the operators"),
    ):
        add(f"{modifier} the kernels {predicate} only.", False)
    for qualifier in ("not yet initialized in capture", "currently frozen", "previously chosen",
                      "still applying inputs", "quickly supplied by runtime"):
        add(f"never apply the buffers {qualifier} yet retain capture.", True)
    _assert_norm_rows(rows, api, target, rule)


@pytest.mark.parametrize("api,target,rule", LEGACY_APIS)
@pytest.mark.parametrize("case", [str.lower, str.capitalize, str.upper, str.title])
@pytest.mark.parametrize("gap", [" ", "\n", "\r\n"])
def test_clause_ownership_composes_with_recipient_passives(api, target, rule, case, gap):
    rows = []

    def add(tail, preserve, prefix="Legacy APIs such as {a}, etc."):
        rows.append((prefix.format(a=api) + gap + case(tail).replace(" ", gap), preserve))

    for participle, modifier, focus, adjunct in product(
        ("given", "assigned", "allocated", "offered", "sent", "taught", "told", "lent"),
        ("already", "very recently"), ("only", "just", "merely"),
        ("", " by the scheduler", " in capture mode"),
    ):
        obj = f"the operators {modifier} {participle} {focus} these buffers{adjunct}"
        add(f"indeed, never, ever supply {obj} so they remain idle.", True)
        add(f"but fortunately, perhaps, the kernels supply {obj} so they remain idle.", False)
        add(f"for the kernels were {modifier} {participle} {focus} these buffers{adjunct}.", False)
    for coordinator, predicate in product(
        ("and", "or", "nor", "but", "for", "yet", "so"),
        ("applied only these buffers", "assigned only these buffers to the stream",
         "assigned only these buffers in capture", "allocated only these buffers in capture mode",
         "have given only these buffers to the stream", "were given only these buffers"),
    ):
        add(f"{coordinator} fortunately, perhaps, the kernels {predicate}.", False)
    for lead, degree, verb in product(
        ("for the kernels in capture mode,", "for the kernels assigned buffers by the scheduler,",
         "for the kernels allocated buffers in capture mode,", "in capture mode, indeed,",
         "fortunately, perhaps,", "never, ever,"),
        ("so directly", "so fast", "so rarely"), ("apply", "resupply", "premultiply"),
    ):
        add(f"{lead} never {degree} {verb} the buffers so they remain idle.", True)
        add(f"{lead} the kernels {degree} {verb} the buffers only.", False)
    for participle in ("taken", "chosen", "written", "driven", "hidden", "broken",
                       "spoken", "stolen", "frozen", "proven", "shown", "known", "seen",
                       "done"):
        add(f"indeed, never apply the buffers already {participle} in capture.", True)
        add(f"perhaps, the kernels have {participle} only these buffers.", False)
    for auxiliary in ("do", "can", "should"):
        add(f"nor {auxiliary} the kernels apply buffers only.", False)
        text = "Avoid allocations, locks, etc." + gap
        text += case(f"nor {auxiliary} the kernels apply buffers via").replace(" ", gap)
        rows.append((text + " " + api + ".", True))
    for predicate in ("have been idle", "have gone offline"):
        add(f"perhaps, the kernels {predicate} only in capture.", False)
    for head in ("for legacy buffers via", "for the kernels in capture mode via",
                 "the kernels given buffers via", "and the kernels given buffers via"):
        rows.append(("Avoid allocations, locks, etc." + gap +
                     case(head).replace(" ", gap) + " " + api + ".", True))
    for boundary in ("; ", "! ", "\n\n", "\r\n \r\n", "\n- ", "\n2. "):
        tail = case("indeed, never supply kernels assigned only these buffers.").replace(" ", gap)
        rows.append((f"Legacy APIs such as {api}, etc.{boundary}{tail}", False))
        text = "Avoid allocations, locks, etc." + boundary
        text += case("fortunately, perhaps, 2D kernels supply the operators assigned just buffers so they remain idle via").replace(" ", gap)
        rows.append((text + " " + api + ".", False))
    _assert_norm_rows(rows, api, target, rule)
    for ticks, lead, preserve in product(("`", "````"),
                                         ("indeed, never", "but perhaps, the kernels"), (True, False)):
        uri = f"https://[::1]:8443/{api}?q=only#never"
        body = f"Legacy APIs such as {ticks}{{a}}{ticks}, etc. "
        body += case(f"{lead} supply the operators given just buffers so they remain idle.").replace(" ", gap)
        if not preserve:
            body = ticks + "Never, ever supply {a} so they remain idle!" + ticks
        expected_preservation = preserve and "never" in lead
        source = body.format(a=api) + f" [guide]({uri}) <{uri}>"
        expected = source if expected_preservation else body.format(a=target) + f" [guide]({uri}) <{uri}>"
        result = _assert_norm_result(source, expected, () if expected_preservation else (rule,), expected_preservation)
        assert result.text.count(uri) == 2
        assert [(a["rule"], a["count"]) for a in result.audit] == [
            ("LEGACY", "1") if expected_preservation else (rule, "1")
        ]


@pytest.mark.parametrize("api,target,rule", LEGACY_APIS)
@pytest.mark.parametrize(
    "case",
    [
        str.lower,
        str.capitalize,
        str.upper,
        lambda s: "".join(c.lower() if i % 2 == 0 else c.upper() for i, c in enumerate(s)),
    ],
)
@pytest.mark.parametrize("gap", [" ", "\n", "\r\n"])
def test_compositional_constituents_and_case_invariance(api, target, rule, case, gap):
    rows = []

    def add(tail, preserve, prefix="Legacy APIs such as {a}, etc."):
        rows.append((prefix.format(a=api) + gap + case(tail).replace(" ", gap), preserve))

    for part, agent in product(
        ("assigned", "loaned", "leased", "rented", "delegated", "initialized"),
        ("", " by the dispatcher", " during replay"),
    ):
        theme = "" if part == "initialized" else " just these buffers"
        add(f"for the kernels {part}{theme}{agent}, never call aclmdlRICaptureBegin.", True)
        add(f"for the kernels {part}{theme}{agent}, the streams assign only local buffers.", False)
    add("for the kernels entrusted with just these buffers by the runtime, never call aclmdlRICaptureBegin.", True)
    for noun, qual in (
        ("threads", "woken by the scheduler"),
        ("kernels", "forgiven by the scheduler"),
        ("workflows", "undertaken during setup"),
        ("streams", "withdrawn from capture"),
        ("launches", "foreseen by the compiler"),
        ("operators", "spoken of during setup"),
        ("kernels", "proven correct"),
        ("kernels", "in capture mode forgiven by the scheduler"),
        ("kernels", "chosen for replay entrusted with these buffers by the runtime"),
    ):
        rows.append(("Avoid allocations, locks, etc." + gap + case(f"the {noun} {qual} via").replace(" ", gap) + f" {api}.", True))
        rows.append(("Avoid allocations, locks, etc." + gap + case(f"2D {noun} {qual} interoperate with").replace(" ", gap) + f" {api}.", False))
        add(f"the {noun} {qual} never dispatch asynchronously.", False)
    for degree, verb in product(
        ("so very cautiously", "so remarkably very carefully", "so extraordinarily fast", "so fairly early"),
        ("apply", "resupply", "premultiply"),
    ):
        add(f"never {degree} {verb} the buffers so they remain idle.", True)
        add(f"the kernels {degree} {verb} the buffers only.", False)
    for coord, chain in product(
        ("but", "for", "yet", "so", "and", "or", "nor"),
        ("surprisingly,", "quite frankly,", "in capture mode, admittedly,"),
    ):
        add(f"{coord}, {chain} the kernels leased only these buffers to the allocator.", False)
    for aux in ("do", "should", "would", "must", "could"):
        add(f"nor, indeed, {aux} the kernels dispatch only these buffers.", False)
        rows.append(("Avoid allocations, locks, etc." + gap + case(f"nor, indeed, {aux} the kernels dispatch through").replace(" ", gap) + f" {api}.", True))
    _assert_norm_rows(rows, api, target, rule)
    for ticks, (clause, preserve) in product(
        ("`", "````"),
        (
            ("for the kernels loaned just these buffers, never call aclmdlRICaptureBegin.", True),
            ("yet, quite frankly, the kernels leased only these buffers to the allocator.", False),
        ),
    ):
        uri = f"https://[::1]:8443/{api}?mode=only#never"
        body = f"Legacy APIs such as {ticks}{{a}}{ticks}, etc." + gap + case(clause).replace(" ", gap)
        source = body.format(a=api) + f" [guide]({uri}) <{uri}>"
        expected = source if preserve else body.format(a=target) + f" [guide]({uri}) <{uri}>"
        res = _assert_norm_result(source, expected, () if preserve else (rule,), preserve)
        assert res.text.count(uri) == 2


@pytest.mark.parametrize("api,target,rule", LEGACY_APIS)
@pytest.mark.parametrize(
    "case",
    [
        str.lower,
        str.capitalize,
        str.upper,
        lambda s: "".join(c.lower() if i % 2 == 0 else c.upper() for i, c in enumerate(s)),
    ],
)
@pytest.mark.parametrize("gap", [" ", "\n", "\r\n"])
def test_compound_nominals_and_case_invariance(api, target, rule, case, gap):
    lead_l = f"Legacy APIs such as {api}, etc.{gap}"
    lead_a = f"Avoid allocations, locks, etc.{gap}"
    uri = f"https://[::1]:8443/({api})?q=only#never"
    rows = [
        (lead_l + case(t).replace(" ", gap), p)
        for t, p in (
            ("for the completion queues drained by the host firmware, never call aclmdlRICaptureBegin.", True),
            ("descriptors never advance.", False),
            ("for the doorbell registers drained by the device driver, the controller retires only local pages.", False),
            ("semaphores chosen for replay never advance.", False),
            ("the descriptors in the ring overwritten by the host firmware never advance.", False),
            ("the descriptors in our routing chosen by the device driver never advance.", False),
            ("the descriptors so really slowly retire only these pages.", False),
            ("never so really quite efficiently invalidate the pages so they remain resident.", True),
            ("but, as documented, the descriptors retire only these pages.", False),
            ("nor, once armed, does the host runtime retire only local pages.", False),
        )
    ] + [
        (lead_a + case(h).replace(" ", gap) + f" {api}.", p)
        for h, p in (
            ("the descriptors invalidated by the host firmware via", True),
            ("the controllers polling the completion queues via", True),
            ("prefer", False),
            ("the completion queues invalidated by the device driver retire pages via", False),
            ("the descriptors in the ring advance via", False),
            ("nor, as expected, do the descriptors retire pages via", True),
        )
    ]
    _assert_norm_rows(rows, api, target, rule)
    clause = case("but, as documented, the descriptors retire only these pages.").replace(" ", gap)
    res = _assert_norm_result(
        f"Legacy APIs such as `{api}`, etc.{gap}{clause} [u]({uri})",
        f"Legacy APIs such as `{target}`, etc.{gap}{clause} [u]({uri})",
        (rule,),
        False,
    )
    assert res.text.count(uri) == 1
