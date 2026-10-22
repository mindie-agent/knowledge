"""Technical boundaries used by the real ReMe tokenizer adapter."""
from mindie_knowledge.retrieval import query_tokens, token_counts, tokens


def test_versions_stay_whole_and_chinese_remains_searchable():
    result = tokens('torch==2.10.0+cpu 显存泄漏')
    assert '2.10.0+cpu' in result
    assert not {'2', '10', '0'}.intersection(result)
    assert {'显存', '存泄', '泄漏'}.issubset(result)


def test_qualified_api_preserves_components_without_substrings():
    result = tokens('torch_npu.npu_rms_norm')
    assert {'torch_npu.npu_rms_norm', 'torch_npu', 'npu_rms_norm', 'rms', 'norm'}.issubset(result)
    assert 'pu_rm' not in result


def test_frequency_index_retains_repeated_api_aliases_and_overlapping_cjk_bigrams():
    counts = token_counts('torch_npu.npu_rms_norm torch_npu.npu_rms_norm torch_npu 显存泄漏 显存泄漏 哈哈哈')
    assert counts == {
        'torch_npu.npu_rms_norm': 2, 'torch_npu': 3, 'torch': 3, 'npu': 3,
        'npu_rms_norm': 2, 'rms': 2, 'norm': 2,
        '显存': 2, '存泄': 2, '泄漏': 2, '哈哈': 2,
    }


def test_ascend_camel_pascal_snake_and_chip_subword_tokenization():
    sample = (
        'DataCopyPad PipeBarrier SetFlag WaitFlag MatmulImpl '
        'ACLGraph GatedDeltaNet aclrtSynchronizeStream '
        'Ascend910B Ascend910B4 npu_fused_infer_attention_score'
    )
    expanded = set(tokens(sample))
    counts = token_counts(sample)
    assert counts == {term: tokens(sample).count(term) for term in counts}
    for expected_group in (
        {'datacopypad', 'data', 'copy', 'pad'},
        {'pipebarrier', 'pipe', 'barrier'},
        {'setflag', 'set', 'flag'},
        {'waitflag', 'wait', 'flag'},
        {'matmulimpl', 'matmul', 'impl'},
        {'aclgraph', 'acl', 'graph'},
        {'gateddeltanet', 'gated', 'delta', 'net'},
        {'aclrtsynchronizestream', 'aclrt', 'synchronize', 'stream'},
        {'ascend910b', 'ascend', '910b'},
        {'ascend910b4', 'ascend', '910b4'},
        {'npu_fused_infer_attention_score', 'npu', 'fused', 'infer', 'attention', 'score'},
    ):
        assert expected_group.issubset(expanded)


def test_vllm_ascend_and_cann_compound_identifiers_expand_to_subwords():
    sample = (
        'npugraph_ex torchair modelslim msmodelslim dspark dflash xlite '
        'eplb mrv2 hixl lmcache mooncake sfa_v1 dsa_v1 mla_v1 fa3_v1 '
        'qli_v2 quarot teacache msmemscope msprof msprobe serving_cast tensor_cast'
    )
    expanded = set(tokens(sample))
    counts = token_counts(sample)
    assert counts == {term: tokens(sample).count(term) for term in counts}
    for expected_group in (
        {'npugraph_ex', 'npugraph', 'npu', 'graph', 'ex'},
        {'torchair', 'torch', 'air'},
        {'modelslim', 'model', 'slim'},
        {'msmodelslim', 'ms', 'modelslim', 'model', 'slim'},
        {'dspark', 'ds', 'spark'},
        {'dflash', 'df', 'flash'},
        {'xlite', 'xl', 'lite'},
        {'eplb', 'ep', 'lb'},
        {'mrv2', 'mr', 'v2'},
        {'hixl', 'hi', 'xl'},
        {'lmcache', 'lm', 'cache'},
        {'mooncake', 'moon', 'cake'},
        {'sfa_v1', 'sfa', 'v1'},
        {'dsa_v1', 'dsa', 'v1'},
        {'mla_v1', 'mla', 'v1'},
        {'fa3_v1', 'fa3', 'fa', 'v1'},
        {'qli_v2', 'qli', 'v2'},
        {'quarot', 'qua', 'rot'},
        {'teacache', 'tea', 'cache'},
        {'msmemscope', 'ms', 'memscope', 'mem', 'scope'},
        {'msprof', 'ms', 'prof'},
        {'msprobe', 'ms', 'probe'},
        {'serving_cast', 'serving', 'cast'},
        {'tensor_cast', 'tensor', 'cast'},
    ):
        assert expected_group.issubset(expanded)


def test_bilingual_query_tokens_expand_cjk_domain_concepts_and_strip_filler_bigrams():
    q = '在昇腾上跑 vllm-ascend 遇到 旋转位置编码 维度不匹配或者精度不对应该怎么排查？'
    expanded = set(query_tokens(q))
    assert {'rope', 'rotary', 'precision', 'bisect', 'mismatch'}.issubset(expanded)
    assert not {'怎么', '应该', '遇到', '或者', '排查'}.intersection(expanded)

    q_op = 'AscendC 算子 非对齐搬运 UB Bank冲突 和 核间同步 死锁 怎么解决'
    expanded_op = set(query_tokens(q_op))
    assert {'datacopypad', 'alignment', 'conflict', 'setflag', 'waitflag', 'deadlock'}.issubset(expanded_op)
    assert not {'怎么', '解决', '算子'}.intersection(expanded_op)


def test_graded_routing_index_bilingual_natural_language_recall_and_noise_immunity():
    from mindie_knowledge.graded.index import RoutingIndex
    from mindie_knowledge.graded.units import GradedUnit

    def make_unit(uid: str, title: str, keywords: list[str], body: str) -> GradedUnit:
        return GradedUnit.from_parts(
            {
                "schema": "mindie-graded-unit/1",
                "id": uid,
                "tier": "L2",
                "category": "sop",
                "evidence_strength": "reproduced_empirical",
                "domain_path": "vllm_ascend/runtime",
                "title": title,
                "inherits_from": [],
                "conditions": {"framework": ["vllm-ascend"]},
                "triggers": {"error_signatures": [], "keywords": keywords},
                "provenance": {
                    "source_type": "pr",
                    "source_locator": "vllm-project/vllm-ascend@abcdef1",
                    "evidence_refs": ["pr:1"],
                },
                "revision": 1,
            },
            body,
        )

    units = [
        make_unit(
            "unit-rope-precision-sop",
            "Interleaved RoPE layout and rotary precision bisect SOP",
            ["interleaved_rope_npu_op", "rotary", "precision", "bisect"],
            "Align RoPE cos/sin cache layout and bisect rotary precision mismatch.",
        ),
        make_unit(
            "unit-aclgraph-bucket-sop",
            "ACLGraph capture invalidation and piecewise bucket sizing SOP",
            ["ACLGraph", "capture", "invalidation", "piecewise", "bucket"],
            # Include an incidental Chinese word ("报错") in one unit to verify
            # conversational filler in unrelated queries never hijacks ranking.
            "Prevent ACLGraph capture invalidation across padded batch buckets (避免报错).",
        ),
        make_unit(
            "unit-eagle-spec-decode-sop",
            "EAGLE3 and MTP speculative decoding draft proposer SOP",
            ["AscendEagleProposer", "eagle3", "speculative", "draft", "mtp"],
            "Configure EAGLE3 draft proposer and tree verification in vLLM-Ascend.",
        ),
        make_unit(
            "unit-eplb-moe-balance-sop",
            "MoE Expert Parallel EPLB dynamic load balancing SOP",
            ["eplb", "balancer", "dyntra_lb", "moe", "expert"],
            "Enable SwiftBalancer and DyntraLB for MoE expert load balancing.",
        ),
        make_unit(
            "unit-ascendc-sync-hazard-sop",
            "AscendC SetFlag/WaitFlag sync audit and pipe hazard elimination SOP",
            ["SetFlag", "WaitFlag", "PipeBarrier", "hazard", "deadlock"],
            "Audit SetFlag/WaitFlag branch symmetry to eliminate pipe hazards and deadlocks.",
        ),
    ]
    index = RoutingIndex(units)

    cases = [
        ("vllm-ascend 旋转位置编码 维度不匹配或者精度不对怎么排查", "unit-rope-precision-sop"),
        ("开启图模式 捕获失效 动态分桶怎么调", "unit-aclgraph-bucket-sop"),
        ("投机采样 草稿模型 树状验证 报错怎么配置", "unit-eagle-spec-decode-sop"),
        ("MoE 专家并行 负载均衡 动态热点路由策略", "unit-eplb-moe-balance-sop"),
        ("AscendC 核间同步 死锁 流水线踩踏 报错怎么解决", "unit-ascendc-sync-hazard-sop"),
    ]
    for query_text, expected_id in cases:
        res = index.query(query_text, include_unknown=True, limit=3)
        assert res["cards"], f"Expected cards for query: {query_text}"
        assert res["cards"][0]["id"] == expected_id, (
            f"Top-1 mismatch for {query_text!r}: got {res['cards'][0]['id']}, expected {expected_id}"
        )

    # Reverse direction: an English query matches a unit whose triggers/body are in Chinese
    cn_unit = make_unit(
        "unit-cn-shmem-sop",
        "SHMEM 对称内存与单边通信排查规程",
        ["对称内存", "单边通信", "链路闪断"],
        "在多机环境下排查对称内存与单边通信超时。",
    )
    bi_index = RoutingIndex([*units, cn_unit])
    rev_res = bi_index.query("shmem symmetric rma link", include_unknown=True, limit=3)
    assert rev_res["cards"] and rev_res["cards"][0]["id"] == "unit-cn-shmem-sop"

    # BM25F header boost: matching title/triggers outranks incidental body mention
    header_unit = make_unit(
        "unit-header-match",
        "SuperKernel scope binary fusion SOP",
        ["SuperKernel"],
        "Apply binary fusion scope around decode ops.",
    )
    body_only_unit = make_unit(
        "unit-body-only-match",
        "General runtime launch checklist",
        ["launch"],
        "Check environment variables before launch; SuperKernel is optional.",
    )
    bm25f_index = RoutingIndex([body_only_unit, header_unit])
    bm25f_res = bm25f_index.query("SuperKernel", include_unknown=True, limit=2)
    assert bm25f_res["cards"][0]["id"] == "unit-header-match"
