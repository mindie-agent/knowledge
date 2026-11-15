"""Permanent regression suite for compound-NP constituent boundaries and gerund agreement.

[Role]
Verify multi-word technical compound NPs across object, prepositional, passive
agent, fronted-PP, subject, and inverted roles alongside determined gerund
singular agreement, case invariance, and literal code/URI byte preservation.
[Invariants]
All expectations are constructed from independent literal strings and API rules;
singleton and batch normalization outputs match byte-for-byte across wraps.
[Timeouts & Locks]
Pure in-memory string normalization assertions (< 2s); no I/O, locks, or waits.
[Side-Effect Policy]
Side-effect-free unit test module; mutates no files, globals, or environment.
"""

from __future__ import annotations

from itertools import product

import pytest

from mindie_knowledge.distiller_text import normalize_text, normalize_texts

LEGACY_APIS: tuple[tuple[str, str, str], ...] = (
    ("ge_graph", "aclgraph", "N7"),
    ("torchair.scope.npu_stream_switch", "vllm_ascend.utils.npu_stream_switch", "N5"),
    ("Notify_Wait", "EventRecord/EventWait (ACLGraph capture order is explicit)", "N4"),
    ("npu_create_tagged_stream", "npu_stream_switch + RoutedMoEMilestones", "N3"),
    ("limit_core_num", "ACLGraph static block dims / HCCL CCU_SCHED", "N6"),
)


def _check_result(result, source: str, expected: str, applied: tuple[str, ...] = (), preserve: bool = False):
    assert result.text.encode("utf-8") == expected.encode("utf-8"), (source, result)
    assert result.source_text.encode("utf-8") == source.encode("utf-8")
    assert result.preserved_legacy is preserve and result.applied == applied
    if preserve or applied:
        classification = "torchair_legacy_only" if preserve else "aclgraph_adapted_from_torchair"
        assert result.classification == result.applicability == classification
    return result


def _assert_norm_result(source: str, expected: str, applied: tuple[str, ...] = (), preserve: bool = False):
    result = _check_result(normalize_text(source), source, expected, applied, preserve)
    assert normalize_texts((source,)) == (result,)
    return result


def _assert_norm_rows(rows: list[tuple[str, bool]], api: str, target: str, rule: str) -> None:
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
def test_compound_nominals_gerund_agreement_and_case_invariance(api, target, rule, case, gap) -> None:
    lead_legacy = f"Legacy APIs such as {api}, etc.{gap}"
    lead_avoid = f"Avoid allocations, locks, etc.{gap}"
    rows: list[tuple[str, bool]] = []

    for tail, preserve in (
        ("the page table refreshed by the control plane never shrinks.", False),
        ("the device page table refreshed by the host control plane never shrinks.", False),
        ("for the page table refreshed by the control plane, never call aclmdlRICaptureBegin.", True),
        ("for the page table refreshed by the control plane, the workers evict only stale mappings.", False),
        ("for the device descriptor pool refreshed by the host control plane, the workers evict only stale mappings.", False),
        ("the queues replenished by the device runtime service never overflow.", False),
        ("the queues replenished by the local worker team never overflow.", False),
        ("nor does the device page table retain only local addresses.", False),
        ("nor does the host control plane retain only local buffers.", False),
        ("the sharding requires only local metadata.", False),
        ("our profiling records only local timestamps.", False),
        ("their buffering retains only resident pages.", False),
        ("your tracing emits only diagnostic events.", False),
        ("my packing uses only aligned buffers.", False),
        ("this sharding requires only local metadata.", False),
        ("the profiling can record only local timestamps.", False),
        ("these running workers evict only stale mappings.", False),
    ):
        rows.append((lead_legacy + case(tail).replace(" ", gap), preserve))

    for head, preserve in (
        ("the workers polling the page table via", True),
        ("the workers polling the device page table via", True),
        ("the workers inspecting the memory hierarchy via", True),
        ("the workers checking the interconnect topology via", True),
        ("the workers with the kernel translation cache via", True),
        ("the workers polling the page table yield after capture via", False),
        ("the workers polling the device page table yield after capture via", False),
        ("the queues replenished by the control plane via", True),
        ("the queues replenished by the host control plane via", True),
        ("the queues replenished by the device runtime service via", True),
        ("the queues replenished by the device scheduler task via", True),
        ("the queues replenished by the local worker team via", True),
        ("the queues replenished by the control plane retire local pages via", False),
        ("nor does the control plane synchronize through", True),
        ("the workers participating in the profiling via", True),
        ("the running workers in the pool via", True),
        ("the page table in the cluster shrinks after capture via", False),
    ):
        rows.append((lead_avoid + case(head).replace(" ", gap) + f" {api}.", preserve))

    _assert_norm_rows(rows, api, target, rule)

    uri = f"https://[2001:db8::16]:8443/({api})?mode=only#never"
    code = "``LeaseState.IDLE != 0; QueueTag::Ready``"
    for ticks, (lead, clause, preserve) in product(
        ("`", "````"),
        (
            ("Avoid allocations, locks, etc.", "the workers polling the page table via §.", True),
            ("Legacy APIs such as §, etc.", "the profiling records only local timestamps.", False),
        ),
    ):
        prose = case(f"{lead} {clause}").replace(" ", gap)
        source = prose.replace("§", f"{ticks}{api}{ticks}") + f" [runbook]({uri}) {code}"
        expected = source if preserve else prose.replace("§", f"{ticks}{target}{ticks}") + f" [runbook]({uri}) {code}"
        res = _assert_norm_result(source, expected, () if preserve else (rule,), preserve)
        assert res.text.count(uri) == 1 and res.text.count(code) == 1


@pytest.mark.parametrize("api,target,rule", LEGACY_APIS)
@pytest.mark.parametrize("case", [str.lower, str.capitalize, str.upper,
    lambda s: "".join(c.lower() if i % 2 == 0 else c.upper() for i, c in enumerate(s))])
@pytest.mark.parametrize("gap", [" ", "\n", "\r\n"])
def test_np_intransitive_complements_and_fragments(api, target, rule, case, gap) -> None:
    rows = []

    def add(clause, preserve=False, *, fragment=False):
        lead = "Avoid allocations, locks, etc. " if fragment else "Legacy APIs such as §, etc. "
        rows.append((case(lead + clause).replace(" ", gap).replace("§", api), preserve))

    for np in ("latency arbiter", "device latency arbiter", "parity checksum",
               "device parity checksum", "page table", "device page table",
               "memory hierarchy", "device memory hierarchy", "routing digest"):
        for head, verb in (("the workers verifying the", "persist"),
                           ("the replicas with the", "differ"),
                           ("the queues provisioned by the", "suffice")):
            add(f"{head} {np} via §.", True, fragment=True)
            for tail in ("for replay", "during replay", "in capture", "after capture",
                         "within epochs", "until teardown", "onto flash", "briefly"):
                add(f"{head} {np} {verb} {tail} via §.", fragment=True)
            for tail in ("only briefly", "only during replay", "without contention"):
                add(f"{head} {np} {verb} {tail}.")
                add(f"{head} {np} {verb} {tail} via §.", True, fragment=True)
            add(f"{head} {np} discard stale entries via §.", fragment=True)

    for det in ("the", "our", "their", "your", "my", "its", "his", "her", "whose",
                "this", "that", "each", "every", "a", "an"):
        gerund = "interleaving" if det == "an" else "journaling"
        for vp in ("stalls only briefly", "persists only during replay", "changes during capture",
                   "suffices for local streams", "flickers only locally", "captures only local events"):
            add(f"{det} {gerund} {vp}.")
        add(f"{det} {gerund} via §.", True, fragment=True)
        if det != "an":
            for np in ("parity checksum", "device parity checksum"):
                for vp in ("changes only during replay", "stalls only briefly", "persists during capture"):
                    add(f"{det} {np} {vp}.")
                add(f"{det} {np} via §.", True, fragment=True)

    for det in ("the", "our", "their", "your", "my", "its", "his", "her", "whose",
                "this", "that", "each", "every", "these", "those", "both", "many", "several", "few"):
        singular = det in ("this", "that", "each", "every")
        head, verb = ("daemon", "emits") if singular else ("daemons", "emit")
        for modifier in ("", " in the pool", " verifying the parity checksum", " provisioned by the latency arbiter"):
            add(f"{det} polling {head}{modifier} {verb} only local events.")
            add(f"{det} polling {head}{modifier} via §.", True, fragment=True)
    for det in ("these", "those", "both", "many", "several", "few"):
        add(f"{det} reshardings require only local metadata.")
        add(f"{det} reshardings via §.", True, fragment=True)
    add("the running workers in the pool never stall.")
    add("the running workers in the pool via §.", True, fragment=True)

    _assert_norm_rows(rows, api, target, rule)
    sample = rows[:4]
    assert normalize_texts(s for s, _ in reversed(sample))[::-1] == normalize_texts(s for s, _ in sample)
    uri = f"https://[2001:db8::18]:8443/({api})?mode=only#never"
    code = "``EpochStamp.VALID != 0; TicketTag::READY``"
    for ticks in ("`", "````"):
        for clause in ("the journaling stalls only briefly.", "the parity checksum changes only during replay."):
            prose = case("Legacy APIs such as §, etc. " + clause).replace(" ", gap)
            source = prose.replace("§", ticks + api + ticks) + f" [runbook]({uri}) {code}"
            expected = prose.replace("§", ticks + target + ticks) + f" [runbook]({uri}) {code}"
            result = _assert_norm_result(source, expected, (rule,))
            assert result.text.count(uri) == result.text.count(code) == 1


@pytest.mark.parametrize("role,source,tail,number", [
    ("subject", "the journaling stalls only briefly.", "stalls only briefly.", "singular"),
    ("subject", "its journaling persists only during replay.", "persists only during replay.", "singular"),
    ("subject", "my journaling stalls only briefly.", "stalls only briefly.", "singular"),
    ("subject", "this journaling stalls only briefly.", "stalls only briefly.", "singular"),
    ("subject", "the parity checksum changes only during replay.", "changes only during replay.", "singular"),
    ("subject", "the device parity checksum changes during capture.", "changes during capture.", "singular"),
    ("object", "the parity checksum persist during replay.", "persist during replay.", "singular"),
    ("object", "the device parity checksum persist during replay.", "persist during replay.", "singular"),
    ("agent", "the latency arbiter suffice for replay.", "suffice for replay.", "singular"),
    ("agent", "the device latency arbiter suffice for replay.", "suffice for replay.", "singular"),
    ("object", "the page table via ge_graph.", "via ge_graph.", "singular"),
    ("object", "the device page table via ge_graph.", "via ge_graph.", "singular"),
    ("object", "the memory hierarchy via ge_graph.", "via ge_graph.", "singular"),
    ("agent", "the device latency arbiter via ge_graph.", "via ge_graph.", "singular"),
    ("subject", "the running workers in the pool via ge_graph.", "in the pool via ge_graph.", "plural"),
    ("subject", "these reshardings require local metadata.", "require local metadata.", "plural"),
])
@pytest.mark.parametrize("case", [str.lower, str.upper])
def test_np_complement_span_and_number(role, source, tail, number, case) -> None:
    from mindie_knowledge.distiller_text import _np_span

    assert _np_span(case(source), role=role) == (case(tail), number)


BOUNDARY_APIS = (
    ('ge_graph', 'aclgraph', 'N7'),
    ('torchair.scope.npu_stream_switch', 'vllm_ascend.utils.npu_stream_switch', 'N5'),
    ('Notify_Wait', 'EventRecord/EventWait (ACLGraph capture order is explicit)', 'N4'),
    ('npu_create_tagged_stream', 'npu_stream_switch + RoutedMoEMilestones', 'N3'),
    ('limit_core_num', 'ACLGraph static block dims / HCCL CCU_SCHED', 'N6'),
)


def _assert_boundary_rows(rows, api, target, rule):
    direct = normalize_texts(source for source, _ in rows)
    for (source, preserve), result in zip(rows, direct):
        assert result.text == (source if preserve else source.replace(api, target))
        assert result.source_text == source and result.preserved_legacy is preserve
        assert result.applied == (() if preserve else (rule,))
        expected_class = 'torchair_legacy_only' if preserve else 'aclgraph_adapted_from_torchair'
        assert result.classification == result.applicability == expected_class
        assert [(a['rule'], a['count']) for a in result.audit] == [
            ('LEGACY', '1') if preserve else (rule, str(source.count(api)))
        ]
    if rows:
        assert normalize_text(rows[0][0]) == direct[0]
        assert normalize_texts((rows[-1][0],)) == (direct[-1],)
        sample = rows[:4]
        assert normalize_texts(source for source, _ in reversed(sample))[::-1] == direct[:4]


@pytest.mark.parametrize('api,target,rule', BOUNDARY_APIS)
@pytest.mark.parametrize('case', [str.lower, str.capitalize, str.upper, str.title])
@pytest.mark.parametrize('gap', [' ', '\n'])
def test_compositional_nominal_and_finite_boundaries(api, target, rule, case, gap):
    rows = []

    def add(clause, preserve, *, legacy=False):
        lead = 'Legacy APIs such as §, etc. ' if legacy else 'Avoid allocations, locks, etc. '
        rows.append((case(lead + clause).replace(' ', gap).replace('§', api), preserve))

    for noun, pp, head in product(
        ('cache coherence protocol', 'error correction controller', 'branch history register'),
        ('in the rack', 'during recovery', 'on startup', 'for replay'),
        ('the workers inspecting the', 'the replicas with the', 'the queues maintained by the'),
    ):
        add(f'{head} {noun} {pp} via §.', True)
        add(f'{head} {noun} {pp} can drift via §.', False)
    for noun in ('interrupt mailboxes', 'retirement scoreboards', 'deadline watchdogs'):
        for pp in ('in the rack', 'during recovery', 'on startup', 'for replay'):
            add(f'their {noun} {pp} via §.', True)
            add(f'their {noun} {pp} drift during replay via §.', False)
    for noun, head in product(
        ('scoreboard', 'mailbox', 'watchdog', 'buffer', 'filter', 'trigger', 'register'),
        ('the workers inspecting the', 'the replicas with the', 'the queues maintained by the'),
    ):
        add(f'{head} {noun} via §.', True)
        for vp in ('drift during replay', 'falter briefly', 'persist for replay'):
            add(f'{head} {noun} {vp} via §.', False)
            add(f'{head} {noun} {vp} only during recovery.', False, legacy=True)
            add(f'{head} {noun} {vp} only during recovery via §.', True)
    _assert_boundary_rows(rows, api, target, rule)


@pytest.mark.parametrize('api,target,rule', BOUNDARY_APIS)
@pytest.mark.parametrize('case', [str.lower, str.capitalize, str.upper, str.title])
@pytest.mark.parametrize('gap', [' ', '\r\n'])
def test_outer_agreement_across_modifier_chains(api, target, rule, case, gap):
    rows = []
    for subject, plural in (
        ('our checkpointing', False), ('this polling daemon', False),
        ('the device parity checksum', False), ('those polling daemons', True),
    ):
        for modifier in (
            'in the rack', 'inspecting the scoreboard', 'maintained by the watchdog',
            'in the rack inspecting the scoreboard maintained by the watchdog',
        ):
            fragment = f'Avoid allocations, locks, etc. {subject} {modifier} via §.'
            rows.append((case(fragment).replace(' ', gap).replace('§', api), True))
            for stem, inflected, complement in (
                ('falter', 'falters', 'only briefly'), ('drift', 'drifts', 'only during replay'),
                ('record', 'records', 'only diagnostic counters'),
                ('preserve', 'preserves', 'local metadata'), ('change', 'changes', 'cleanly'),
                ('persist', 'persists', 'for replay'),
                ('lower', 'lowers', 'only diagnostic counters'),
            ):
                verb = stem if plural else inflected
                clause = f'Legacy APIs such as §, etc. {subject} {modifier} {verb} {complement}.'
                rows.append((case(clause).replace(' ', gap).replace('§', api), False))
    _assert_boundary_rows(rows, api, target, rule)


@pytest.mark.parametrize('role', ['object', 'agent'])
@pytest.mark.parametrize('outer_number', [None, 'singular', 'plural'])
@pytest.mark.parametrize('case', [str.lower, str.upper])
@pytest.mark.parametrize('noun', ['scoreboard', 'mailbox', 'watchdog', 'rack', 'parity checksum'])
def test_embedded_np_retains_outer_predicate(role, outer_number, case, noun):
    from mindie_knowledge.distiller_text import _np_span

    verb = 'drift' if outer_number == 'plural' else 'falters'
    tail = f'{verb} only during replay.'
    assert _np_span(case(f'the {noun} {tail}'), role=role, outer_number=outer_number) == (
        case(tail), 'singular')
    fragment = 'in the rack via ge_graph.'
    assert _np_span(case(f'the {noun} {fragment}'), role=role, outer_number=outer_number) == (
        case(fragment), 'singular')


@pytest.mark.parametrize('word', [
    'controller', 'protocol', 'mailbox', 'scoreboard', 'watchdog', 'arbiter', 'scheduler',
    'dispatcher', 'allocator', 'compiler', 'latency', 'coherence', 'movement', 'readiness',
    'hierarchy', 'topology', 'table', 'queue', 'rack', 'pool', 'metadata', 'throughput',
])
@pytest.mark.parametrize('number', ['singular', 'plural'])
def test_pure_nominal_does_not_head_finite_vp(word, number):
    from mindie_knowledge.distiller_text import _has_finite_vp

    inflected = word[:-1] + 'ies' if word.endswith('y') else word + ('es' if word.endswith('x') else 's')
    form = inflected if number == 'singular' else word
    for pp in ('in capture', 'during replay', 'on startup', 'for recovery'):
        assert not _has_finite_vp(f'{form} {pp} via ge_graph.', number=number)


@pytest.mark.parametrize('word', ['drift', 'stall', 'change', 'record', 'buffer', 'filter', 'trigger', 'register'])
def test_determined_homographs_are_nominal_heads(word):
    from mindie_knowledge.distiller_text import _np_span

    for role in ('subject', 'object', 'agent'):
        assert _np_span(f'the {word} in the rack.', role=role) == ('in the rack.', 'singular')


@pytest.mark.parametrize('verb', ['falters', 'jitters', 'lingers', 'differs', 'recovers'])
@pytest.mark.parametrize('modifier', [
    'in the rack', 'inspecting the scoreboard', 'maintained by the watchdog',
    'in the rack inspecting the scoreboard maintained by the watchdog',
])
@pytest.mark.parametrize('tail', ['under contention', 'for replay'])
def test_singular_verbal_suffix_before_bare_pp(verb, modifier, tail):
    source = f'Avoid allocations, locks, etc. our checkpointing {modifier} {verb} {tail} via ge_graph.'
    _assert_boundary_rows([(source, False)], 'ge_graph', 'aclgraph', 'N7')


@pytest.mark.parametrize('api,target,rule', BOUNDARY_APIS)
@pytest.mark.parametrize('case', [str.lower, str.capitalize, str.upper, str.title])
def test_determined_operation_and_ly_heads(api, target, rule, case):
    rows = []
    for head in (
        'read', 'reset', 'split', 'cast', 'broadcast', 'set', 'cut', 'put', 'hit',
        'run', 'burst', 'cost', 'build', 'load', 'store', 'send', 'pass', 'call',
        'step', 'drain', 'flush', 'sync', 'launch', 'issue', 'fetch', 'write',
        'copy', 'move', 'probe', 'scan', 'check', 'test', 'dump', 'swap', 'trap',
        'wait', 'lock', 'unlock', 'reply', 'supply', 'assembly', 'anomaly',
        'family', 'ally', 'tally', 'rally', 'monopoly', 'power supply', 'transport reply',
    ):
        for det, modifier in (('the', ''), ('a', ' in the pipeline'),
                              ('our', ' issued by the transport daemon')):
            np = f'{det} {head}{modifier}'
            rows.extend((
                (case(f'Avoid allocations, locks, etc. {np} via §.').replace('§', api), True),
                (case(f'Legacy APIs such as §, etc. {np} requires only local metadata.').replace('§', api), False),
            ))
        for suffix, vp in ((' request', 'requires'), (' requests', 'require')):
            source = case(f'Legacy APIs such as §, etc. the {head}{suffix} {vp} only local metadata.')
            rows.append((source.replace('§', api), False))
    for head in ('reads', 'resets', 'splits', 'replies', 'supplies', 'families', 'assemblies'):
        rows.extend((
            (case(f'Avoid allocations, locks, etc. the {head} via §.').replace('§', api), True),
            (case(f'Legacy APIs such as §, etc. the {head} require only local metadata.').replace('§', api), False),
        ))
    for head in ('the Δqueue', 'the x86::descriptor', 'the briefly'):
        rows.append((case(f'Avoid allocations, locks, etc. {head} via §.').replace('§', api), True))
    _assert_boundary_rows(rows, api, target, rule)


@pytest.mark.parametrize('api,target,rule', BOUNDARY_APIS)
@pytest.mark.parametrize('case', [str.lower, str.capitalize, str.upper, str.title])
def test_medial_adjective_constituents_across_roles(api, target, rule, case):
    rows = []
    for noun in (
        'host virtual address', 'device physical address', 'task ready queue',
        'kernel shared memory', 'stream local buffer', 'node global table',
        'cluster remote latch', 'host direct pointer', 'device contiguous buffer',
        'task atomic register', 'kernel async channel', 'stream dynamic descriptor',
        'node static buffer', 'cluster linear table', 'host rotary register',
        'device sparse queue', 'task dense table', 'kernel pinned memory',
        'stream paged memory', 'node cached address', 'cluster coherent channel',
        'host private buffer', 'device public queue', 'task internal table',
        'kernel external descriptor', 'stream logical address', 'node native channel',
        'cluster primary queue', 'host secondary table', 'device active latch',
        'task pending queue', 'kernel valid pointer', 'stream free memory',
        'node raw descriptor', 'host local virtual address', 'local device address',
        'local virtual address',
    ):
        for head, vp in (
            ('the', 'requires'), ('the workers inspecting the', 'require'),
            ('the replicas with the', 'require'), ('the queues managed by the', 'require'),
        ):
            np = f'{head} {noun}'
            rows.extend((
                (case(f'Avoid allocations, locks, etc. {np} via §.').replace('§', api), True),
                (case(f'Legacy APIs such as §, etc. {np} {vp} only local metadata.').replace('§', api), False),
                (case(f'Avoid allocations, locks, etc. {np} can recover locally via §.').replace('§', api), False),
            ))
    _assert_boundary_rows(rows, api, target, rule)


@pytest.mark.parametrize('api,target,rule', BOUNDARY_APIS)
@pytest.mark.parametrize('case', [str.lower, str.capitalize, str.upper, str.title])
def test_open_class_verbs_in_strong_frames(api, target, rule, case):
    rows = [(case('Legacy APIs such as §, etc. the kernel complies with streams only.').replace('§', api), False)]
    for base, inflected in (
        ('enqueue', 'enqueues'), ('dequeue', 'dequeues'), ('signal', 'signals'),
        ('offload', 'offloads'), ('splice', 'splices'), ('hash', 'hashes'),
        ('scrub', 'scrubs'), ('multiplex', 'multiplexes'), ('debounce', 'debounces'),
        ('quarantine', 'quarantines'), ('checkpoint', 'checkpoints'), ('graft', 'grafts'),
        ('ready', 'readies'), ('free', 'frees'),
    ):
        for subject, verb in (('the transport daemon', inflected),
                              ('those transport daemons', base),
                              ('our reindexing', inflected)):
            for modifier in ('', ' in the enclosure', ' inspecting the host virtual address',
                             ' in the enclosure inspecting the device physical address managed by the task ready queue'):
                for tail in ('the pending packets only during startup', 'some local metadata',
                             'only diagnostic counters', 'only briefly', 'cleanly'):
                    source = case(f'Legacy APIs such as §, etc. {subject}{modifier} {verb} {tail}.')
                    rows.append((source.replace('§', api), False))
    _assert_boundary_rows(rows, api, target, rule)


@pytest.mark.parametrize('role', ['subject', 'object', 'agent'])
@pytest.mark.parametrize('case', [str.lower, str.upper])
def test_complete_nominal_heads_and_unambiguous_vp_spans(role, case):
    from mindie_knowledge.distiller_text import _np_span

    for noun in ('read', 'split', 'reply', 'supply', 'assembly', 'anomaly',
                 'host virtual address', 'device physical address', 'task ready queue',
                 'host local virtual address', 'local device address', 'local virtual address'):
        for tail in ('via ge_graph.', 'enqueues the pending packets.', 'reboots during startup.'):
            assert _np_span(case(f'the {noun} {tail}'), role=role, outer_number='singular') == (
                case(tail), 'singular')
    for noun in ('reads', 'splits', 'replies', 'supplies'):
        assert _np_span(case(f'the {noun} in the rack.'), role=role) == (
            case('in the rack.'), 'plural')


@pytest.mark.parametrize('number', ['singular', 'plural'])
def test_weak_frames_keep_nominal_safeguards_and_productive_events(number):
    from mindie_knowledge.distiller_text import _has_finite_vp

    for base, singular in (('reboot', 'reboots'), ('reconnect', 'reconnects'),
                           ('resync', 'resyncs'), ('failover', 'failovers'),
                           ('coalesce', 'coalesces'), ('quiesce', 'quiesces'),
                           ('degrade', 'degrades'), ('recover', 'recovers'),
                           ('harden', 'hardens'), ('soften', 'softens'),
                           ('deepen', 'deepens'), ('loosen', 'loosens')):
        form = singular if number == 'singular' else base
        for pp in ('during startup', 'for replay', 'after capture', 'under contention'):
            assert _has_finite_vp(f'{form} {pp}.', number=number)
    for word in ('mailboxes', 'enclosure', 'crossbar', 'sequencer', 'seqlock', 'futex',
                 'protocol', 'controller', 'checksum', 'address', 'queue', 'latch',
                 'token', 'kitchen', 'specimen', 'oxygen'):
        assert not _has_finite_vp(f'{word} in the rack via ge_graph.', number=number)
    for word in ('them', 'us', 'me', 'him', 'ourselves', 'someone', 'nothing',
                 'movement', 'readiness', 'latency', 'coherence', 'ownership', 'topology'):
        form = word + 's' if number == 'singular' else word
        assert not _has_finite_vp(f'{form} the pending packets.', number=number)
