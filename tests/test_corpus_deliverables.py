"""Release gate for the operators and vllm-ascend L4 corpus deliverables.

Validates the live corpus checkouts, their L4 ablation digests, and their rebuilt
routing manifests directly without duplicate staging snapshots.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mindie_knowledge.graded.maintenance import build_routing_index
from mindie_knowledge.graded.repository import GradedRepository
from mindie_knowledge.graded.units import GradedUnit

CORPORA = {
    "operators": {
        "root": Path("/Volumes/data/code/knowledge-operators"),
        "new": "unit-vectorized-bf16-fp32-accumulation-cast-fusion-mutator",
        "l4": 2,
    },
    "vllm-ascend": {
        "root": Path("/Users/maoxx241/code/mindie-transcript-20261004/content"),
        "new": "unit-vllm-ascend-gdn-idle-dp-zero-pad-workspace-mutator",
        "l4": 1,
    },
}


def _require_corpus(name: str) -> dict:
    entry = CORPORA[name]
    if not entry["root"].is_dir():
        pytest.skip(f"external corpus checkout not present: {entry['root']}")
    return entry


@pytest.mark.parametrize("name", sorted(CORPORA))
def test_corpus_contract_and_rebuilt_manifest(name):
    entry = _require_corpus(name)
    root = entry["root"]
    report = GradedRepository(root).validate_corpus()
    assert report["valid"] is True, report["errors"]
    assert report["unit_count"] == 11
    assert report["tiers"]["L4"] == entry["l4"]
    checked = build_routing_index(root, check=True)
    assert checked["ok"] is True, checked["errors"]

    units = {}
    for path in root.rglob("unit-*.md"):
        if any(part in {"docs", "index", "feedback", "tasks"} for part in path.relative_to(root).parts):
            continue
        unit = GradedUnit.from_text(path.read_text(encoding="utf-8"), path.relative_to(root).as_posix())
        units[unit.id] = unit
    mutators = [unit for unit in units.values() if unit.metadata["tier"] == "L4"]
    assert len(mutators) == entry["l4"]
    assert entry["new"] in units
    for unit in mutators:
        assert unit.metadata["category"] == "mutator"
        assert unit.metadata["conditions"]
        assert unit.metadata["triggers"]["error_signatures"]
        assert unit.metadata["triggers"]["keywords"]
        provenance = unit.metadata["provenance"]
        assert provenance["evidence_refs"]
        for ref in provenance["evidence_refs"]:
            if not ref.startswith(("http://", "https://")):
                assert (root / ref).is_file(), f"{unit.id}: missing evidence_ref {ref}"
        if unit.id == entry["new"]:
            locator = provenance["source_locator"]
            evidence = root / locator
            assert evidence.is_file(), f"{unit.id}: missing {locator}"
            assert hashlib.sha256(evidence.read_bytes()).hexdigest() == provenance["source_digest"]
        assert "fallback" in unit.body.lower()

    manifest = json.loads((root / "index/routing-manifest.json").read_text(encoding="utf-8"))
    assert manifest["unit_count"] == 11
    assert manifest["generation"] == report["generation"]
    assert {row["id"] for row in manifest["units"]} == set(units)
    assert {row["id"] for row in manifest["units"] if row["tier"] == "L4"} == {
        unit.id for unit in mutators}


def test_operator_mutator_has_a_distinct_second_family_and_complete_closure():
    root = _require_corpus("operators")["root"]
    second = GradedUnit.from_text(
        (root / "Reduction/unit-vectorized-bf16-fp32-accumulation-cast-fusion-mutator.md").read_text(),
        "Reduction/unit-vectorized-bf16-fp32-accumulation-cast-fusion-mutator.md",
    )
    first = GradedUnit.from_text(
        (root / "normalization/rmsnorm/unit-residual-free-rmsnorm-fastpath.md").read_text(),
        "normalization/rmsnorm/unit-residual-free-rmsnorm-fastpath.md",
    )
    assert {first.id, second.id} == {
        "unit-residual-free-rmsnorm-fastpath",
        "unit-vectorized-bf16-fp32-accumulation-cast-fusion-mutator",
    }
    assert "vectorized_bf16_load_fp32_accumulate" in second.body
    assert "two_stage_splitk_tail_guard" in second.body
    assert "register pressure" in second.body.lower()
    assert "tail" in second.body.lower() and "unaligned" in second.body.lower()
    assert second.metadata["inherits_from"] == [
        "unit-ascend-910b-a3-memory-alignment-and-tiling-axioms",
        "unit-ascend-operator-roofline-and-four-view-measurement",
        "unit-vector-reduction-fp32-accumulator-and-ub-alignment-sop",
    ]


def test_vllm_mutator_has_empty_batch_and_static_shape_fallbacks():
    root = _require_corpus("vllm-ascend")["root"]
    unit = GradedUnit.from_text(
        (root / "0day/Qwen3_5/unit-vllm-ascend-gdn-idle-dp-zero-pad-workspace-mutator.md").read_text(),
        "0day/Qwen3_5/unit-vllm-ascend-gdn-idle-dp-zero-pad-workspace-mutator.md",
    )
    assert unit.metadata["conditions"]["active_batch"] == 0
    assert unit.metadata["conditions"]["graph_capture_shape"] == "dynamic"
    assert "Active batch > 0" in unit.body
    assert "Static graph capture shape" in unit.body
    assert "coordinate_batch_across_dp" in unit.body
    assert "conv_state" in unit.body and "ssm_state" in unit.body
    assert unit.metadata["inherits_from"] == [
        "unit-vllm-ascend-npu-runtime-and-graph-invariants",
        "unit-vllm-ascend-verification-and-state-parity-gates",
        "unit-vllm-ascend-idle-dp-gdn-state",
    ]
