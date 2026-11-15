"""Regression contracts for polarity preservation, abbreviations, code spans, and URI opacity.

[Role]
Verify sentence-scoped restrictive polarity (English and Chinese), soft wraps,
abbreviation continuations (`e.g.`, `etc.`, `no.`), markdown emphasis replies,
code-span literal protection, and RFC-compliant URI opacity across `normalize_text`
and `normalize_texts`.
[Invariants]
All oracle expectations are independent string literals; singleton and batch
normalization match byte-for-byte across line-ending styles.
[Timeouts & Locks]
Pure in-memory string normalization (< 3s); no I/O, locks, or subprocesses.
[Side-Effect Policy]
Side-effect-free unit test module; mutates no files, globals, or environment.
"""

from __future__ import annotations

import pytest

from mindie_knowledge.distiller_text import normalize_text, normalize_texts

LEGACY_APIS = (
    ("ge_graph", "aclgraph", "N7"),
    ("torchair.scope.npu_stream_switch", "vllm_ascend.utils.npu_stream_switch", "N5"),
    ("Notify_Wait", "EventRecord/EventWait (ACLGraph capture order is explicit)", "N4"),
    ("npu_create_tagged_stream", "npu_stream_switch + RoutedMoEMilestones", "N3"),
    ("limit_core_num", "ACLGraph static block dims / HCCL CCU_SCHED", "N6"),
)

ENGLISH_POLARITY = (
    "only", "exclusive", "exclusively", "sole", "solely", "limited to", "restricted to", "specific to",
    "but not", "except", "without", "not", "n't", "never", "no", "neither", "nor", "unsupported", "unavailable",
    "incompatible", "cannot", "can't", "doesn't", "does not", "is not", "aren't", "are not", "fails", "forbidden", "prohibited",
)
CHINESE_POLARITY = (
    "仅", "只", "唯", "独", "专有", "专属", "限于", "局限于", "不支持", "不可", "不能", "无法",
    "未支持", "不兼容", "禁用", "除外", "而非", "而不是", "但", "却",
)
EXTENDED_POLARITY = (
    "avoid", "avoids", "avoided", "avoiding", "non-functional", "non-support",
    "non-compatible", "non-api_2", "unless", "instead of", "instead\t of", "rather than",
    "do not", "does not", "did not", "cannot", "can't", "can’t", "don't", "don’t",
    "doesn't", "doesn’t", "shouldn't", "shouldn’t", "mustn't", "mustn’t",
    "disallow", "disallows", "disallowed", "disallowing", "deprecated",
    "避免", "不要", "切勿", "请勿", "勿", "禁止", "严禁", "不受支持", "不适合",
    "不宜", "不得", "不应", "除非", "代替", "替代",
)

URI_TEMPLATES = (
    "http://{a}.example.test/{a}?backend={a}#{a}",
    "https://[2001:db8::1]:8443/{a}?backend={a}#torchair",
    "https://user:pass@example.test:443/docs/({a})?q=({a})#{a}",
    "mailto:alice@example.test?subject={a}&body={a}",
    "custom+v1.-://server:9000/{a}?root=/{a}#{a}",
    "urn:example:{a}:version1", "x:{a}", "file:///docs/{a}",
    "data:text/plain,{a}", "git+ssh://user@host/{a}",
    "https://example.test/only/no?unsupported={a}#not",
)


def _assert_norm_result(source, expected, applied=(), preserve=False, *, check_batch=False):
    result = normalize_text(source)
    assert result.text.encode() == expected.encode(), (source, result)
    assert result.source_text.encode() == source.encode()
    assert result.preserved_legacy is preserve and result.applied == applied
    if preserve or applied:
        classification = "torchair_legacy_only" if preserve else "aclgraph_adapted_from_torchair"
        assert result.classification == result.applicability == classification
    if check_batch:
        assert normalize_texts((source,)) == (result,)
    return result


@pytest.mark.parametrize("text", [
    "supports only ge_graph", "only available in ge_graph", "ge_graph-only",
    "does not support aclgraph", "not supported in aclgraph", "仅在 ge_graph 下可用",
    "只支持 ge_graph", "仅限 ge_graph", "不支持 aclgraph", "TorchAir-only",
])
def test_restrictive_legacy_text_preserved(text):
    r = normalize_text("SuperKernel " + text)
    assert r.preserved_legacy and r.text == r.source_text and not r.applied


@pytest.mark.parametrize("backend", ["ge_graph", "TorchAir", "GE 图", "aclgraph",
                                     "torch.npu.npu_fusion_attention"])
def test_sentence_polarity_is_independent_of_word_order(backend):
    for token in ENGLISH_POLARITY + CHINESE_POLARITY:
        for text in (f"{backend} supports SuperKernel, {token} selected mode.",
                     f"{token} selected mode: SuperKernel uses {backend}.",
                     f"SuperKernel {backend} {token} selected mode。"):
            result = normalize_text(text)
            assert result.text == result.source_text == text
            assert result.preserved_legacy and not result.applied
            assert result.classification == "torchair_legacy_only"


@pytest.mark.parametrize("mention", [
    "ge_graph", "TorchAir", "GE 图", "aclgraph",
    "torch.npu.npu_fusion_attention", "npu_create_tagged_stream", "Notify_Wait", "limit_core_num",
])
def test_all_polarity_families_and_normalized_symbols(mention):
    for token in EXTENDED_POLARITY:
        for text in (f"{token} {mention} for this selected mode.",
                     f"This mode: {mention}, {token} the alternative.",
                     f"Neutral text; {mention} {token} this mode。"):
            result = normalize_text(text)
            assert result.text == result.source_text == text
            assert result.preserved_legacy and not result.applied
            assert result.classification == "torchair_legacy_only"


@pytest.mark.parametrize("text", [
    "SuperKernel is supported in ge_graph but not in aclgraph.",
    "ge_graph is the only supported backend for SuperKernel.",
    "SuperKernel 在 ge_graph 中可用，aclgraph 下不支持。",
    "SuperKernel is exclusive to ge_graph.",
    "SuperKernel isn’t supported in aclgraph; ge_graph is available.",
    "TorchAir’s backend can’t execute the selected graph.",
    "torch.npu.npu_fusion_attention is supported only in legacy graph mode.",
    "The graph is unavailable in torchair.scope.npu_stream_switch.",
    "Use ge_graph, but migrate the supported cases to aclgraph.",
    "Avoid ge_graph for SuperKernel.", "SuperKernel with ge_graph should be avoided.",
    "The ge_graph path is non-functional.", "The ge_graph backend has non-support for this feature.",
    "Use ge_graph unless the graph is dynamic.", "Unless capture is disabled, avoid ge_graph.",
    "Use aclgraph instead of ge_graph.", "Use aclgraph rather than ge_graph.",
    "避免使用 ge_graph。", "不要使用 ge_graph。", "勿使用 ge_graph。", "禁止使用 ge_graph。",
    "SuperKernel 在 ge_graph 下不受支持。", "ge_graph 不适合此图。", "除非使用 ge_graph 才能运行。",
    "使用 aclgraph 代替 ge_graph。", "应使用 aclgraph 而非 ge_graph。",
    "Run SuperKernel without ge_graph.", "No ge_graph execution is permitted.",
    "Never use ge_graph for SuperKernel.", "SuperKernel is unsupported in ge_graph.",
    "ge_graph cannot execute this SuperKernel.", "Do not use ge_graph.", "Don't use ge_graph.",
    "Use every backend except ge_graph.", "Neutral statement; avoid ge_graph for SuperKernel.",
    "中性陈述；避免使用 ge_graph。", "Avoid torchair.scope.npu_stream_switch for this graph.",
    "Use npu_stream_switch instead of npu_create_tagged_stream.",
])
def test_natural_prohibitions_and_contrasts_remain_verbatim(text):
    result = normalize_text(text)
    assert result.text == result.source_text == text
    assert result.preserved_legacy and not result.applied


@pytest.mark.parametrize("text,expected", [
    ('exe_mode: "ge_graph"', 'exe_mode: "aclgraph"'),
    ("SuperKernel is supported in ge_graph.", "SuperKernel is supported in aclgraph."),
    ("ge_graph supports SuperKernel.", "aclgraph supports SuperKernel."),
    ("SuperKernel 在 ge_graph 中可用。", "SuperKernel 在 aclgraph 中可用。"),
    ("Use torchair.scope.npu_stream_switch.", "Use vllm_ascend.utils.npu_stream_switch."),
    ("No allocation is needed. Use ge_graph.", "No allocation is needed. Use aclgraph."),
    ("Only one buffer is needed。使用 ge_graph。", "Only one buffer is needed。使用 aclgraph。"),
    ("Use ge_graph for the soleus and notional examples.",
     "Use aclgraph for the soleus and notional examples."),
    ("Use ge_graph.", "Use aclgraph."),
    ("Never allocate; use ge_graph.", "Never allocate; use aclgraph."),
    ("使用 ge_graph。", "使用 aclgraph。"),
])
def test_positive_normalization_and_unrelated_sentence_scope(text, expected):
    result = normalize_text(text)
    assert result.text == expected and result.source_text == text
    assert result.applied and not result.preserved_legacy


@pytest.mark.parametrize("wrap", ["\n", "\r\n"])
def test_soft_wraps_preserve_complete_restrictive_input(wrap):
    texts = [
        "Avoid\nge_graph for SuperKernel.", "Use ge_graph\nunless the graph is dynamic.",
        "避免\n使用 ge_graph。", "不要使用\nge_graph。", "ge_graph\n不受支持。",
        "使用 aclgraph 代替\nge_graph。", "Avoid legacy APIs, e.g.\nNotify_Wait.",
        "Use ge_graph. Neutral; avoid\nNotify_Wait in this scope. Use limit_core_num.",
    ]
    for phrase in ("instead of", "rather than", "do not", "does not", "did not",
                   "limited to", "restricted to", "specific to"):
        texts.append("Use the selected API " + phrase.replace(" ", "\n") + " ge_graph.")
    for text in texts:
        text = text.replace("\n", wrap)
        result = normalize_text(text)
        assert result.text == result.source_text == text
        assert result.preserved_legacy and not result.applied


@pytest.mark.parametrize("abbreviation", ["e.g.", "i.e.", "vs.", "cf.", "etc.", "fig.",
                                          "no.", "al.", "approx.", "min.", "max."])
def test_abbreviations_keep_restriction_and_api_in_one_clause(abbreviation):
    for abbr in (abbreviation, abbreviation.upper()):
        neutral = f"Avoid tensor.{abbr} Use ge_graph."
        assert normalize_text(neutral).text == neutral.replace("ge_graph", "aclgraph")
        for mention in ("ge_graph", "torchair.scope.npu_stream_switch", "Notify_Wait",
                        "torch.npu.npu_fusion_attention", "npu_create_tagged_stream"):
            label = abbr + (" 2:" if abbreviation == "no." else "")
            text = f"Use ge_graph. Neutral; avoid legacy APIs, {label} {mention}. Use limit_core_num."
            result = normalize_text(text)
            assert result.text == result.source_text == text
            assert result.preserved_legacy and not result.applied


@pytest.mark.parametrize("boundary", [".\n", ";\n", "\n\n", "\r\n \t\r\n",
                                      "\n- ", "\n* ", "\n+ ", "\n• ", "\n1. ", "\r\n2) "])
def test_paragraph_list_and_sentence_boundaries_remain_independent(boundary):
    text = f"Avoid allocation{boundary}Use ge_graph."
    result = normalize_text(text)
    assert result.text == text.replace("ge_graph", "aclgraph")
    assert result.source_text == text and not result.preserved_legacy


@pytest.mark.parametrize("mention,target,rule", LEGACY_APIS)
@pytest.mark.parametrize("case", [str.lower, str.upper, str.title])
@pytest.mark.parametrize("wrap", [" ", "\n", "\r\n"])
@pytest.mark.parametrize("variant", ["labels", "connectors"])
def test_abbreviation_and_sentence_scope_variants(mention, target, rule, case, wrap, variant):
    etc, no = case("etc."), case("no")
    boundaries = ("\n\n", "\r\n \t\r\n", "\n- ", "\n* ", "\n+ ", "\n• ", "\n1) ", "\r\n2. ")
    if variant == "labels":
        samples = [
            (template.format(a=case(abbr), w=wrap, m=mention), preserve)
            for abbr in ("sec.", "ref.", "secs.", "refs.", "figs.", "nos.", "ch.", "eq.", "no.")
            for template, preserve in (
                ("Do not use the backend in {a}{w}2: {m}.", True),
                ("Avoid the legacy API documented in {a}{w}2: {m}.", True),
                ("Use configuration {a}{w}2 with {m}.", False),
                ("Avoid tensor.{a}{w}Use {m}.", False),
            )
        ]
        samples += [
            (f"Do not use legacy APIs, {etc}{wrap}{mention}.", True),
            (f"Avoid {mention}, {etc}, in capture.", True),
            (f"Use {mention}, {etc}{wrap}不受支持。", True),
            (f"Use {mention}, {etc}{wrap}and buffers, {etc}{wrap}不受支持。", True),
            (f"Avoid tensor.{etc}{wrap}Use {mention}.", False),
            (f"{no} {mention} capture is supported.", True),
            (f"Support for {mention}: {no}.", True),
            (f"Support for {mention}: {no}.{wrap}Use buffers.", True),
            (f"Use {mention}. Support for buffers: {no}.", False),
        ]
        samples += [
            (f"Avoid allocations, locks, {etc}{wrap}{s} {mention}。", False)
            for s in ("Use", "Enable", "Prefer", "Apply", "In", "The", "This", "When", "If", "使用")
        ]
        samples += [
            (f"Avoid allocations, {etc}{b}{mention}.", False)
            for b in ("\n\n", "\r\n\r\n", "\n- ", "; ", "。")
        ]
    else:
        counts = ("1 buffer is allocated", "2 buffers are allocated", "12 streams run", "2048 events remain")
        samples = [
            (f"Support for {mention}{d} {no}.{wrap}{count}.", True)
            for count in counts
            for d in (":", "：", "?", "？", "=", "—", "--", ";", "；", ",", "，", " is", " was", " are", " were")
        ]
        samples += [(f"{mention} {no}.{b}{count}.", True) for count in counts for b in (*boundaries, wrap)]
        samples += [(f"Support for {mention}: {no}.{b}2 buffers are allocated.", True) for b in boundaries]
        samples += [
            (f"Is {mention} supported? {no}.{wrap}1 buffer is allocated.", True),
            (f"{no}.{wrap}2 uses {mention}.", False),
            (f"{no}.{wrap}2 buffers are allocated for {mention}.", False),
            (f"Use {mention} with configuration {no}.{wrap}2.", False),
            (f"Configuration {no}.{wrap}2 is supported with {mention}.", False),
            (f"Avoid the API in {no}.{wrap}2 with {mention} in use.", True),
        ]
        connectors = (
            "例如 比如 诸如 如 包括 包含 含 尤其是 尤其 特别是 特别 即 以及 及 和 与 或 或者 还有 乃至 甚至".split()
            + "such as|including|especially|particularly|specifically|namely|for example|for instance|e.g.|i.e.|and|or|nor|as well as".split("|")
        )
        samples += [
            (f"Do not use legacy APIs, {etc}{wrap}{case(c).replace(' ', wrap)} {mention}。", True)
            for c in connectors
        ]
        samples += [(f"Do not use legacy APIs, {etc}{wrap}{c}{mention}。", True) for c in ("如", "如：", "包括", "及")]
        samples += [(f"Do not use legacy APIs, {etc}{b}例如 {mention}。", False) for b in (*boundaries, "; ", "。")]
        samples += [
            (f"Avoid allocations, locks, {etc}{wrap}{s} {mention}.", False)
            for s in (
                "CPU kernels use", "GPU kernels use", "API selection uses", "NPU streams use",
                "HCCL collectives use", "A backend uses", "I/O threads use", "DMA transfers use",
                "BF16 kernels use", "USE", "使用", "如果启用则使用",
            )
        ]
        samples += [
            (f"Use {mention}, {etc}{wrap}{predicate}", True)
            for predicate in ("不受支持。", "is unsupported.", "IS unsupported.", "ARE unavailable.",
                              "Unsupported.", "FORBIDDEN.", "Cannot run.")
        ]
        samples += [
            (f"Do not use legacy APIs, {etc}{wrap}{m}.", True)
            for m in (mention, "aclgraph", "GE 图", "TorchAir", "torch.npu.npu_fusion_attention")
        ]
        samples += [(f"Avoid tensor.{a}{wrap}Use {mention}.", False) for a in (etc, no + ".", case("sec."))]
    for text, preserve in samples:
        if preserve:
            text = "Use ge_graph. " + text + " Use limit_core_num.\t"
        expected = text if preserve else text.replace(mention, target)
        _assert_norm_result(text, expected, () if preserve else (rule,), preserve)


@pytest.mark.parametrize("api,target,rule", LEGACY_APIS)
@pytest.mark.parametrize("case", [str.lower, str.title, str.upper])
@pytest.mark.parametrize("gap", [" ", "\n", "\r\n"])
def test_number_labels_use_immediate_context(api, target, rule, case, gap):
    no = case("no.") + gap + "2"
    for prefix in ("For capture, ", "Mode: ", "捕获时，", "注意："):
        for suffix in (f" ({api}).", f": {api}.", f" with {api} in use."):
            text = f"Use ge_graph. {prefix}avoid configuration {no}{suffix} Use limit_core_num.\t"
            _assert_norm_result(text, text, preserve=True)
        for phrase in (f"use {api} with configuration {no}.",
                       f"use configuration {no} with {api}.",
                       f"configuration {no} is supported with {api}."):
            text = prefix + phrase
            _assert_norm_result(text, text.replace(api, target), (rule,))
    for delimiter in (":", "?", "；", "=", "—", ",", " is"):
        for boundary in (gap, "\n\n", "\r\n \t\r\n"):
            text = f"Support for {api}{delimiter} {case('no.')}{boundary}2 buffers are allocated."
            _assert_norm_result(text, text, preserve=True)


@pytest.mark.parametrize("api,target,rule", LEGACY_APIS)
@pytest.mark.parametrize("case", [str.lower, str.title, str.upper])
@pytest.mark.parametrize("gap", [" ", "\n", "\r\n"])
def test_etc_predicates_and_independent_subjects(api, target, rule, case, gap):
    etc = case("etc.")
    for tail in ("never work in capture.", "only work in legacy mode.",
                 "no longer supported.", "still unsupported.", "always fail in capture.",
                 "exclusively run in legacy mode.", "also cannot run."):
        for capitalize in (str.lower, str.capitalize, str.upper):
            text = f"Use ge_graph. Legacy APIs such as {api}, {etc}{gap}"
            text += capitalize(tail).replace(" ", gap) + " Use limit_core_num."
            _assert_norm_result(text, text, preserve=True)
    for subject in ("CPU kernels use", "GPU kernels use", "I/O threads use", "The runtime uses",
                    "2D kernels use", "3D convolutions use",
                    "64-bit pointers use", "16x16 tiles use", "8-bit kernels can use"):
        text = f"Avoid allocations, locks, {etc}{gap}{subject} {api}."
        _assert_norm_result(text, text.replace(api, target), (rule,))
        text = f"Legacy APIs such as {api}, {etc}{gap}{subject} {api}."
        _assert_norm_result(text, text.replace(api, target), (rule,))
    text = f"Legacy APIs such as {api}, {etc}{gap}Only CPU kernels need buffers."
    _assert_norm_result(text, text.replace(api, target), (rule,))
    text = f"Avoid allocations, {etc}{gap}Only CPU kernels use {api}."
    _assert_norm_result(text, text, preserve=True)
    for tail in (api + ".", "2: " + api + ".", case("no.") + gap + "2: " + api + "."):
        text = f"Do not use legacy APIs, {etc}{gap}{tail}"
        _assert_norm_result(text, text, preserve=True)


@pytest.mark.parametrize("api,target,rule", LEGACY_APIS)
@pytest.mark.parametrize("case", [str.lower, str.title, str.upper])
@pytest.mark.parametrize("gap", [" ", "\n", "\r\n"])
@pytest.mark.parametrize("emphasis", ["*", "**", "***", "_", "__", "___"])
def test_markdown_negative_answers(api, target, rule, case, gap, emphasis):
    for inner in ("", " ", gap):
        for answer in (emphasis + inner + case("no.") + inner + emphasis,
                       emphasis + inner + case("no") + inner + emphasis + "."):
            for delimiter in ("?", "？", ";", ":"):
                text = f"Use ge_graph. Is {api} supported{delimiter}{gap}{answer}"
                text += gap + "2 buffers are allocated. Use limit_core_num.\t"
                _assert_norm_result(text, text, preserve=True)
            for boundary in ("\n\n", "\r\n \t\r\n", "\n- ", "\n1. "):
                text = f"Is {api} supported?{boundary}{answer} 2 buffers are allocated."
                _assert_norm_result(text, text.replace(api, target), (rule,))


@pytest.mark.parametrize("api,target,rule", LEGACY_APIS)
@pytest.mark.parametrize("ticks", ["`", "``", "```", "`````"])
def test_negative_reply_code_spans_are_literals(api, target, rule, ticks):
    for answer in ("No.", "**No.**", "__NO.__", "***no***.", "_No_."):
        for delimiter in ("?", ":", ";"):
            text = f"Is {api} supported{delimiter} {ticks}{answer}{ticks} 2 buffers are allocated."
            _assert_norm_result(text, text.replace(api, target), (rule,))
        for gap in (" ", "\n", "\r\n"):
            text = f"{ticks}Is {api} supported?{gap}{answer}{ticks}"
            _assert_norm_result(text, text.replace(api, target), (rule,))
        text = f"Is {ticks}{api}{ticks} supported? {answer} 2 buffers are allocated."
        _assert_norm_result(text, text, preserve=True)
    text = f"Avoid {ticks}{api}{ticks}."
    _assert_norm_result(text, text, preserve=True)
    if len(ticks) > 1:
        text = f"Is {api} supported? {ticks}literal ` **No.**{ticks}"
        _assert_norm_result(text, text.replace(api, target), (rule,))
    text = f"{ticks}literal\n\nIs {api} supported? **No.** 2 buffers. {ticks}"
    _assert_norm_result(text, text, preserve=True)


@pytest.mark.parametrize("api,target,rule", LEGACY_APIS)
@pytest.mark.parametrize("template", URI_TEMPLATES)
@pytest.mark.parametrize("wrapper", ["See {u}.", "See ({u}).", "[guide]({u})",
                                     "[guide](<{u}>)", "<{u}>"])
def test_uris_are_opaque_to_rules_and_polarity(api, target, rule, template, wrapper):
    uri = template.format(a=api)
    link = wrapper.format(u=uri)
    _assert_norm_result(link, link)
    text = f"Use {api}. {link} Use {api}."
    expected = f"Use {target}. {link} Use {target}."
    result = _assert_norm_result(text, expected, (rule,))
    assert result.audit[0]["count"] == "2"
    text = f"Use {api} with {link}"
    _assert_norm_result(text, f"Use {target} with {link}", (rule,))
    text = f"Avoid {api}. {link}"
    _assert_norm_result(text, text, preserve=True)


def test_uri_boundaries_preserve_rule_order_and_audit_counts():
    uri = "https://[::1]:8443/(ge_graph)?x=Notify_Wait#torchair.scope.npu_stream_switch"
    source = f"(ge_graph) [ge_graph]({uri})<{uri}> ge_graph"
    expected = f"(aclgraph) [aclgraph]({uri})<{uri}> aclgraph"
    result = _assert_norm_result(source, expected, ("N7",), check_batch=True)
    assert result.audit[0]["count"] == "3"
    uri = "urn:example:exe_mode%3A%22ge_graph%22:Notify_Wait:limit_core_num"
    source = f'exe_mode: "ge_graph"; {uri}; Notify_Wait; limit_core_num; ge_graph'
    expected = (f'exe_mode: "aclgraph"; {uri}; EventRecord/EventWait '
                "(ACLGraph capture order is explicit); ACLGraph static block dims / HCCL CCU_SCHED; aclgraph")
    result = _assert_norm_result(source, expected, ("N1", "N4", "N6", "N7"))
    assert [r["count"] for r in result.audit] == ["1"] * 4
    uri = "https://example.test/SuperKernel%20does%20not%20support%20aclgraph"
    _assert_norm_result(f"Use ge_graph ({uri}).", f"Use aclgraph ({uri}).", ("N7",))
    text = f"SuperKernel does not support aclgraph. See {uri}."
    _assert_norm_result(text, text, preserve=True)
    text = "Avoid allocations https://example.test/docs. Use ge_graph."
    _assert_norm_result(text, text.replace("ge_graph", "aclgraph"), ("N7",))
    for uri in ("https://example.test/docs)ge_graph?x=Notify_Wait",
                "urn:example:ge_graph)torchair.scope.npu_stream_switch",
                "https://example.test/a!$&'()*+,;=ge_graph#Notify_Wait"):
        for link in (uri, "<" + uri + ">"):
            _assert_norm_result(f"Use ge_graph. {link}", f"Use aclgraph. {link}", ("N7",))


@pytest.mark.parametrize("api,target,rule", LEGACY_APIS)
@pytest.mark.parametrize("case", [str.lower, str.title, str.upper])
@pytest.mark.parametrize("gap", [" ", "\n", "\r\n"])
def test_numbered_nouns_own_relative_and_label_continuations(api, target, rule, case, gap):
    for noun in ("configuration", "config", "option", "item", "case", "mode", "rule", "step",
                 "section", "issue", "entry", "setting", "parameter", "variant", "choice",
                 "reference", "revision"):
        for delimiter in (" ", ": ", " is "):
            label = noun + delimiter + case("no.") + gap + "17"
            for relative, positive in ((f"where {api} is enabled", "where buffers are allocated"),
                                       (f"when {api} is enabled", "when buffers are allocated"),
                                       (f"whose kernels use {api}", "whose buffers are allocated"),
                                       (f"which uses {api}", "which allocates buffers"),
                                       (f"that uses {api}", "that allocates buffers"),
                                       (f"in which {api} is enabled", "in which buffers are allocated")):
                text = f"Use ge_graph. For capture, avoid {label} {relative}. Use limit_core_num.\t"
                _assert_norm_result(text, text, preserve=True)
                text = f"Mode: use {api} with {label} {positive}."
                _assert_norm_result(text, text.replace(api, target), (rule,))
            for tail in (f" ({api}).", f": {api}.", f" with {api} in use."):
                text = f"Avoid {label}{tail}"
                _assert_norm_result(text, text, preserve=True)
    for boundary in ("\n\n", "\n2. ", "\n- "):
        text = f"Avoid configuration {case('no.')}{boundary}17 where {api} is enabled."
        _assert_norm_result(text, text.replace(api, target), (rule,))
    for label in (f"Is {api} supported?", f"Support for {api}:"):
        text = label + gap + case("no.") + gap + "17 buffers are allocated."
        _assert_norm_result(text, text, preserve=True)


@pytest.mark.parametrize("api,target,rule", LEGACY_APIS)
@pytest.mark.parametrize("ticks", ["`", "````", "`````", "```````"])
@pytest.mark.parametrize("gap", [" ", "\n", "\r\n"])
def test_code_masks_polarity_and_internal_sentence_marks(api, target, rule, ticks, gap):
    for payload in ("No", "NO", "no", "*No*", "**No**", "_No_", "Avoid", "unsupported",
                    "never allowed", "only", "不支持"):
        for period in ("", "."):
            for delimiter in (":", "=", " is"):
                text = f"Support for {api}{delimiter}{gap}{ticks}{payload}{period}{ticks}."
                _assert_norm_result(text, text.replace(api, target), (rule,))
    text = f"{ticks}Avoid{gap}{api}. Never supported.{ticks}"
    _assert_norm_result(text, text.replace(api, target), (rule,))
    text = f"Avoid {ticks}{api}. No.{ticks}."
    _assert_norm_result(text, text, preserve=True)
    for punctuation in ("?", "!", ".", ";"):
        text = f"Avoid the literal {ticks}No{punctuation}{ticks}: {api}."
        _assert_norm_result(text, text, preserve=True)


@pytest.mark.parametrize("api,target,rule", LEGACY_APIS)
@pytest.mark.parametrize("tail", ["?", "!", ".", ";", ",", ":"])
@pytest.mark.parametrize("wrapper", ["<{u}>", "[guide]({u})", "[nested [guide]](<{u}>)",
                                     "[nested [guide]]({u})", "[soft\n wrap]({u})",
                                     "[escaped \\] label]({u})", "[even \\\\[nested]]({u})"])
def test_explicit_uri_endpoints_keep_internal_punctuation(api, target, rule, tail, wrapper):
    for template in URI_TEMPLATES:
        uri = template.format(a=api) + tail
        link = wrapper.format(u=uri)
        text = f"Use ge_graph. Avoid the API documented at {link}: {api}. Use limit_core_num."
        _assert_norm_result(text, text, preserve=True)
        text = f"Use {api} with {link}."
        result = _assert_norm_result(text, f"Use {target} with {link}.", (rule,))
        assert [r["count"] for r in result.audit] == ["1"]
        for outside in (".", "?", "!", ";"):
            text = f"Avoid allocations {link}{outside} Use {api}."
            _assert_norm_result(text, text.replace(f"Use {api}", f"Use {target}"), (rule,))
    text = f"Avoid allocations https://example.test/docs?backend={api}. Use {api}."
    _assert_norm_result(text, text.replace(f"Use {api}", f"Use {target}"), (rule,))
