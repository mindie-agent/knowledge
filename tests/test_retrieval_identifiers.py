"""Regression for a real published RMSNorm experience missed by its basename."""
from pathlib import Path

from mindie_knowledge.markdown import Document
from mindie_knowledge.retrieval import lexical_search, tokens


def test_operator_components_find_qualified_api_without_substring_matching():
    docs = [
        Document(layer="experience", title="Operator check", content=body,
                 slug=name, path=Path(name + ".md"), uri=name)
        for name, body in [
            ("rms", "torch_npu.npu_rms_norm(x, w, epsilon=1e-6)"),
            ("sum", "torch_npu.npu_sum(x)"),
        ]
    ]
    for query in ("torch_npu.npu_rms_norm", "npu_rms_norm", "rms_norm"):
        hits = lexical_search(query, docs, limit=2)
        assert hits[0].uri == "rms"
    assert len(lexical_search("rms_norm", docs, limit=2)) == 1
    assert lexical_search("pu_rm", docs, limit=2) == []
    assert lexical_search("mystery", docs, limit=2) == []


def test_versions_stay_whole_and_chinese_remains_searchable():
    result = tokens("torch==2.10.0+cpu 显存泄漏")
    assert "2.10.0+cpu" in result
    assert not {"2", "10", "0"}.intersection(result)
    assert {"显存", "存泄", "泄漏"}.issubset(result)
