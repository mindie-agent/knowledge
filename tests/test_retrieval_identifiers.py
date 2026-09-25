"""Regression for a real published RMSNorm experience missed by its basename."""
from pathlib import Path

from mindie_knowledge.markdown import Document
from mindie_knowledge.retrieval import tokens
from retrieval_oracle import lexical_search, lexical_search_streaming


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


def test_streaming_lexical_search_matches_the_loaded_variant():
    docs = [
        Document(layer="experience", title=f"Case {i}",
                 content=f"Body {i} about npu_rms_norm and device mapping.",
                 slug=str(i), path=Path(f"{i}.md"), uri=str(i))
        for i in range(50)
    ]
    for query in ("rms_norm", "device mapping", "nothing here"):
        loaded = [(h.uri, h.score) for h in lexical_search(query, docs, limit=7)]
        streamed = [
            (h.uri, h.score)
            for h in lexical_search_streaming(query, lambda: iter(docs), limit=7)
        ]
        assert streamed == loaded
