"""Tier-0 contracts for three-stack CANNBot/Triton/ACLGraph knowledge distillation.

[Role]
Verify offline and live ingestion of the 53 CANNBot-DSL operators, K3/DS41 groups,
33 Triton-Ascend rules, 5 MFU gates, TorchAir-to-ACLGraph normalization rules,
`mindie-graded-unit/1` schema compatibility, zero `SKILL.md` emission, strict
boolean boundaries before I/O, and non-swallowing `TimeoutError` propagation.
[Invariants]
Live-corpus checks skip cleanly when the read-only reference checkouts are absent
(e.g., on isolated remote CI nodes); all offline checks run deterministically.
[Timeouts & Locks]
Bounded reads of pinned catalog snapshots and local files (< 2s); no locks.
[Side-Effect Policy]
Read-only except `tmp_path` JSONL output verification.
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import fields, replace
import inspect
from pathlib import Path
import re

import pytest

from mindie_knowledge import cannbot_aclgraph_distiller as dist
from mindie_knowledge import distiller_sources as sources
from mindie_knowledge.cannbot_aclgraph_distiller import (
    NORMALIZATION_RULES,
    STATIC_OPERATORS,
    distill_live,
    distill_static,
    distill_units,
    normalize_text,
    scan_live,
    stack_counts,
    static_ingestion,
    write_units,
)
from mindie_knowledge.knowledge_unit import KnowledgeUnit, KnowledgeUnitError, validate_units

REPO_ROOT = Path(__file__).resolve().parents[1]
REF_ROOT = Path("/Users/maoxx241/code/refs")
VLLM_ROOT = Path("/Users/maoxx241/code/vllm-ascend")
BAD_BOOLS = ("false", "true", None, 0, 1, [], {}, 0.0, 1.0, (), set(), object())


def test_offline_distillation_counts_and_stacks() -> None:
    units = distill_static()
    assert len(units) == 110
    counts = stack_counts(units)
    assert counts == {
        "ascendc": 3,
        "cannbot_dsl": 59,
        "framework_aclgraph": 7,
        "triton_ascend": 41,
    }
    operators = [unit for unit in units if unit.unit_id.startswith("unit-cannbot-dsl-op-")]
    rules = [unit for unit in units if unit.unit_id.startswith("unit-triton-ascend-rule-")]
    gates = [unit for unit in units if unit.unit_id.startswith("unit-triton-ascend-mfu-gate-")]
    assert len(operators) == 53 and len(rules) == 33 and len(gates) == 5
    assert Counter(u.conditions["catalog_classification"] for u in operators) == {
        "full_sample_physics": 5,
        "structural_schema_only": 48,
    }
    assert len({unit.unit_id for unit in units}) == len(units)
    assert validate_units(units) == units
    first = units[0]
    assert first.id == first.unit_id and first.tokens == first.token_count
    assert first.source_repo and first.source_commit and first.source_path
    assert "adaptation_notes" in first.provenance


def test_live_ingestion_and_provenance(tmp_path: Path) -> None:
    if not (REF_ROOT / "cannbot-dsl/catalog/operators.json").exists():
        pytest.skip("live cannbot reference corpus unavailable on this host")
    live = scan_live(REF_ROOT, VLLM_ROOT)
    assert live.ok and live.live_verified and not live.degraded_offline
    assert len(live.operators) == 53
    assert len(live.k3) == 10 and len(live.ds41) == 10
    assert len(live.triton_rules) == 33
    assert live.has_fixpipe_doc
    assert live.hardware_constants["ub_simd"] == 253952
    assert live.hardware_constants["ub_simt"] == 221184
    assert live.hardware_constants["ssbuf_mix"] == 3072
    assert "ACLGraphWrapper" in live.aclgraph_symbols
    assert "super_kernel_optimize" in live.aclgraph_symbols
    assert "207008" in live.aclgraph_symbols
    assert len(live.knowledge_files) >= 4
    assert static_ingestion().operator_sources == live.operator_sources
    assert static_ingestion().operator_tests == live.operator_tests

    units = distill_units(live)
    assert len(units) == 110
    assert len(distill_units(live, include_aclgraph=False)) == 103
    cards = [unit for unit in units if unit.unit_id.startswith("unit-cannbot-dsl-op-")]
    assert len(cards) == 53
    assert all(any(name in unit.body for unit in cards) for name in live.operators)
    repos = {
        "cannbot-skills": REF_ROOT / "cannbot-skills",
        "cannbot-dsl": REF_ROOT / "cannbot-dsl",
        "cannbot-knowledge": REF_ROOT / "cannbot-knowledge",
        "vllm-ascend": VLLM_ROOT,
    }
    for unit in units:
        root = repos[unit.source_repo]
        assert (root / unit.source_path).exists(), (unit.unit_id, unit.source_path)
        assert unit.source_commit == live.source_commits[unit.source_repo]
        assert unit.provenance["content_sha256"] == live.content_hashes[f"{unit.source_repo}/{unit.source_path}"]

    target = write_units(units, tmp_path)
    assert target.name == "units.jsonl" and len(target.read_text(encoding="utf-8").splitlines()) == 110


def test_normalization_engine_rewrites_torchair_claims() -> None:
    assert len(NORMALIZATION_RULES) == 7
    text = (
        'exe_mode: "ge_graph"; SuperKernel does not support aclgraph; '
        "npu_create_tagged_stream; Notify_Wait; torchair.scope.npu_stream_switch; limit_core_num"
    )
    result = normalize_text(text)
    assert result.preserved_legacy and result.text == text
    result = normalize_text(text.replace("SuperKernel does not support aclgraph; ", ""))
    assert set(result.applied) == {"N1", "N3", "N4", "N5", "N6"}
    assert result.applicability == "aclgraph_adapted_from_torchair"
    assert "ge_graph" not in result.text
    assert "aclgraph" in result.text
    assert result.audit and all("rule" in row for row in result.audit)
    native = normalize_text("ACLGraphWrapper replays a captured NPUGraph")
    assert native.applied == () and native.applicability == "aclgraph_native"
    assert len(STATIC_OPERATORS) == 53


def test_write_units_emits_jsonl_and_zero_skill(tmp_path: Path) -> None:
    target = write_units(distill_static(), tmp_path)
    assert target.name == "units.jsonl"
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 110
    assert not (tmp_path / "SKILL.md").exists()
    assert not any(path.name == "SKILL.md" for path in tmp_path.rglob("SKILL.md"))


def test_static_ingestion_matches_live_cardinality() -> None:
    live = static_ingestion()
    assert live.ok and len(live.operators) == 53
    counts = Counter(unit.authoring_stack for unit in distill_static())
    assert sum(counts.values()) == 110


def test_distiller_provenance_and_unit_validation() -> None:
    units = dist.distill_static()
    assert len(units) == 110
    for unit in units:
        assert len(unit.source_commit) == 40
        assert len(unit.provenance["content_sha256"]) == 64
        assert len(unit.body.encode("utf-8")) <= unit.token_count
        assert unit.source_path and not unit.source_path.startswith(f"{unit.source_repo}/")
    legacy = dist.normalize_text("SuperKernel 仅支持 ge_graph 模式，不支持 aclgraph。")
    assert legacy.preserved_legacy and legacy.text == "SuperKernel 仅支持 ge_graph 模式，不支持 aclgraph。"
    assert legacy.classification == "torchair_legacy_only"
    assert "仅支持 aclgraph" not in legacy.text
    kernel = dist.normalize_text("Use 248 KiB UB for a SIMD kernel.")
    assert kernel.applicability == "operator_kernel_agnostic"
    adapted = dist.normalize_text(
        'exe_mode: "ge_graph"; SuperKernel does not support aclgraph; Notify_Wait'
    )
    assert adapted.preserved_legacy and adapted.text == adapted.source_text
    with pytest.raises(KnowledgeUnitError):
        KnowledgeUnit(
            "unit-xyz", "t", "L1", "axiom", "ascendc",
            "operator_kernel_agnostic", "long body", token_count=1,
            provenance={
                "source_repo": "r",
                "source_path": "a.md",
                "source_commit": "0" * 40,
                "content_sha256": "0" * 64,
            },
        )
    unit = KnowledgeUnit(
        "unit-xyz", "t", "L1", "axiom", "ascendc",
        "operator_kernel_agnostic", "汉字",
        provenance={
            "source_repo": "r",
            "source_path": "a.md",
            "source_commit": "0" * 40,
            "content_sha256": "0" * 64,
        },
    )
    assert unit.token_count == len("汉字".encode("utf-8"))


def test_graded_unit_schema_key_compatibility() -> None:
    reference = (
        REPO_ROOT
        / "corpus/knowledge-operators/_common/unit-ascend-aclgraph-bucket-padding-and-varlen-axioms.md"
    )
    assert reference.exists()
    front = reference.read_text(encoding="utf-8").split("---", 2)[1]
    keys = {line.split(":", 1)[0] for line in front.splitlines() if re.match(r"^[a-z_]+:", line)}
    unit = dist.distill_static()[0].to_dict()
    assert keys <= set(unit)
    assert {"source_locator", "source_digest"} <= set(unit["provenance"])
    assert unit["schema"] == "mindie-graded-unit/1" and unit["revision"] >= 1


def test_timeout_is_not_swallowed(monkeypatch) -> None:
    def timeout(**kwargs):
        raise TimeoutError("audit injected source timeout")

    monkeypatch.setattr(dist, "scan_live", timeout)
    with pytest.raises(TimeoutError):
        dist.distill_units()
    monkeypatch.setattr(dist, "scan_live", timeout)
    with pytest.raises(TimeoutError):
        dist.distill_units(allow_offline=True)


def test_distiller_strict_boolean_parameters_and_dataclass_fields(tmp_path: Path) -> None:
    offline = sources.static_ingestion()
    records = [offline, normalize_text("Use ge_graph.")]
    calls = [
        (sources.scan_live, (), {}),
        (sources.static_ingestion, (), {}),
        (dist.distill_live, (), {}),
        (dist.distill_units, (offline,), {}),
    ]

    def _bool_params(fn):
        return [
            p.name for p in inspect.signature(fn).parameters.values()
            if "bool" in str(p.annotation) or isinstance(p.default, bool)
        ]

    for bad in BAD_BOOLS:
        for fn, args, defaults in calls:
            for flag in _bool_params(fn):
                with pytest.raises((ValueError, sources.DistillError), match=flag):
                    fn(*args, **dict(defaults, **{flag: bad}))
        for record in records:
            for f in fields(record):
                if f.type is bool or f.type == "bool":
                    with pytest.raises((ValueError, sources.DistillError)):
                        replace(record, **{f.name: bad})

    for flag in (False, True):
        assert sources.static_ingestion(flag).degraded_offline is flag
        assert len(dist.distill_units(offline, include_aclgraph=flag)) == (110 if flag else 103)
    with pytest.raises(sources.DistillError):
        sources.scan_live(tmp_path, tmp_path, allow_offline=False)
    assert sources.scan_live(tmp_path, tmp_path, allow_offline=True).degraded_offline is True
    assert len(distill_live(tmp_path, tmp_path, allow_offline=True)) == 110

    # Verify AST inventory of boolean parameters and dataclass fields across distiller modules.
    distiller_files = [
        REPO_ROOT / "mindie_knowledge" / name
        for name in (
            "knowledge_unit.py",
            "distiller_sources.py",
            "distiller_lexicon.py",
            "distiller_clause.py",
            "distiller_text.py",
            "cannbot_aclgraph_distiller.py",
        )
    ]
    parameters, dataclasses = set(), set()
    for path in distiller_files:
        module = ".".join(path.relative_to(REPO_ROOT).with_suffix("").parts)
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            members = node.body if isinstance(node, ast.ClassDef) else [node]
            owner = node.name + "." if isinstance(node, ast.ClassDef) else ""
            for item in members:
                if isinstance(item, ast.AnnAssign) and "bool" in ast.unparse(item.annotation):
                    dataclasses.add((module, owner[:-1], item.target.id))
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                args = item.args.posonlyargs + item.args.args
                defaults = [None] * (len(args) - len(item.args.defaults)) + item.args.defaults
                for arg, default in zip(args + item.args.kwonlyargs, defaults + item.args.kw_defaults):
                    if (arg.annotation and "bool" in ast.unparse(arg.annotation)) or (
                        isinstance(default, ast.Constant) and isinstance(default.value, bool)
                    ):
                        parameters.add((module, owner + item.name, arg.arg))
    exercised = {
        (fn.__module__, fn.__qualname__, flag)
        for fn, _, _ in calls for flag in _bool_params(fn)
    }
    assert parameters == exercised
    exercised_fields = {
        (type(r).__module__, type(r).__name__, f.name)
        for r in records for f in fields(r)
        if f.type is bool or "bool" in str(f.type)
    }
    assert dataclasses == exercised_fields


def test_ingestion_rejects_invalid_flags_before_io_or_branching(monkeypatch) -> None:
    def touched(*args, **kwargs):
        raise AssertionError("invalid boolean reached I/O or delegated work")

    monkeypatch.setattr(sources, "Path", touched)
    monkeypatch.setattr(dist, "scan_live", touched)
    monkeypatch.setattr(dist, "_base_units", touched)
    for bad in BAD_BOOLS:
        for fn, options in (
            (sources.scan_live, {"allow_offline": bad}),
            (sources.static_ingestion, {"degraded_offline": bad}),
            (dist.distill_live, {"allow_offline": bad}),
            (dist.distill_units, {"allow_offline": bad}),
            (dist.distill_units, {"include_aclgraph": bad}),
        ):
            with pytest.raises(sources.DistillError, match="must be bool"):
                fn(**options)
