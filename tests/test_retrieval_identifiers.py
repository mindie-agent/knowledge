"""Technical boundaries used by the real ReMe tokenizer adapter."""
from mindie_knowledge.retrieval import token_counts, tokens


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
