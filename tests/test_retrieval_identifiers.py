"""Technical boundaries used by the real ReMe tokenizer adapter."""
from mindie_knowledge.retrieval import tokens


def test_versions_stay_whole_and_chinese_remains_searchable():
    result = tokens('torch==2.10.0+cpu 显存泄漏')
    assert '2.10.0+cpu' in result
    assert not {'2', '10', '0'}.intersection(result)
    assert {'显存', '存泄', '泄漏'}.issubset(result)


def test_qualified_api_preserves_components_without_substrings():
    result = tokens('torch_npu.npu_rms_norm')
    assert {'torch_npu.npu_rms_norm', 'torch_npu', 'npu_rms_norm', 'rms', 'norm'}.issubset(result)
    assert 'pu_rm' not in result
