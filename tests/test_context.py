"""Quoted Markdown preserves conditions, tables, fences and source positions."""
from mindie_knowledge.context import document_context, structured_excerpt


def assert_spans(raw, evidence):
    lines = raw.splitlines()
    for span in evidence["spans"]:
        source = "\n".join(lines[span["line_start"] - 1:span["line_end"]])
        source = source[span["column_start"] - 1:]
        quoted = evidence["text"][span["excerpt_start"]:span["excerpt_end"]]
        assert source.startswith(quoted)


def test_table_header_and_selected_row_keep_positions():
    raw = "# NPU constants\n\n| SoC | FP16 TFLOPS | Basis |\n|---|---|---|\n" + "| older | 1 | declared |\n" * 20 + "| Ascend910B4 | 245.76 | declared, unverified snapshot |\n"
    evidence = structured_excerpt(raw, "Ascend910B4", max_chars=400)
    assert "FP16 TFLOPS" in evidence["text"] and "245.76" in evidence["text"]
    assert evidence["section"] == ["NPU constants"]
    assert_spans(raw, evidence)


def test_clipped_fence_does_not_invent_complete_code():
    raw = "# Triton\n\n## Kernel case\n```python\n" + "ordinary = 1\n" * 50 + "unique_ub_spill = tl.load(ptr)\n" + "ordinary = 2\n" * 50 + "```\n"
    evidence = structured_excerpt(raw, "unique_ub_spill", max_chars=160)
    assert "unique_ub_spill" in evidence["text"] and evidence["code_clipped"]
    assert evidence["language"] == "python" and evidence["section"] == ["Triton", "Kernel case"]
    assert len(evidence["text"]) <= 160
    assert_spans(raw, evidence)


def test_distant_recorded_condition_is_retained_without_an_applicability_verdict():
    raw = "# Replay\n\n## Conditions\n- soc: Ascend910B4\n\n## Observation\n" + "background\n" * 20 + "ACLGraph output differs. Cause unknown.\n"
    evidence = structured_excerpt(raw, "ACLGraph", max_chars=500)
    assert "Ascend910B4" in evidence["text"] and "Cause unknown" in evidence["text"]
    assert_spans(raw, evidence)
    assert "applies" not in evidence
    context = document_context(raw, {"execution_mode": "graph", "cann": "unknown"})
    assert {row["field"] for row in context["conditions"]} == {"soc", "execution_mode"}


def test_long_matching_block_reserves_space_for_source_condition():
    raw = "# Graph\n\n## Conditions\n- execution_mode: graph\n\n## Observation\n" + "background\n" * 20
    raw += "unique_padding_failure " + "measured observation " * 100
    evidence = structured_excerpt(raw, "unique_padding_failure", max_chars=200)
    assert "unique_padding_failure" in evidence["text"]
    assert "execution_mode: graph" in evidence["text"]
    assert len(evidence["text"]) <= 200
    assert_spans(raw, evidence)
