"""Deterministic CANNBot 3-stack and TorchAir-to-ACLGraph corpus distiller.

[Role]
Distill exactly 110 graded knowledge units (19 foundational cards, 53 operator
cards, 33 Triton-Ascend rule cards, and 5 MFU-gate cards) from content-verified
upstream sources with per-repository commits, repo-relative paths, and SHA-256
content digests.
[Invariants]
TimeoutError is re-raised before any OSError handler. Offline ingestion is
explicit and marked degraded; it never claims live verification. Operator cards
preserve source/test paths and CI exclusions; rule cards preserve raw
applicability, trigger, and reference paths. Every live unit resolves to an
existing upstream file.
[Timeouts & Locks]
Bounded reads of known reference files only; no recursion over large trees.
[Side-Effect Policy]
Read-only scans. Writing is opt-in through `write_units` and emits JSONL only.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from .distiller_sources import (
    CORPUS_CONTRACT, DEFAULT_REFS_ROOT, DEFAULT_VLLM_ROOT, HARDWARE_PATHS,
    KNOWLEDGE_PATHS, MFU_GATES, PINNED_COMMITS, STATIC_DS41, STATIC_K3,
    STATIC_OPERATORS, TRITON_RULES, DistillError, LiveIngestion, TritonRuleRow,
    VLLM_PATHS, operator_classification, scan_live, static_ingestion,
)
from .distiller_text import (
    NORMALIZATION_RULES, NormalizationResult, NormalizationRule, normalize_text,
    normalize_texts,
)
from .knowledge_unit import (
    AUTHORING_STACKS, KnowledgeUnit, KnowledgeUnitError, validate_units,
    write_units as _write_units,
)

SOURCE_COMMIT = PINNED_COMMITS["cannbot-dsl"]
_COMMIT_HEX_RE = re.compile(r"[0-9a-f]{40}\Z")
_FIXPIPE = "ops/ascendc-api-best-practices/references/api-fixpipe-1c2v.md"
_INDEX = "ops/triton-latency-optimizer/references/Index.md"
_MFU = "ops/triton-mfu-analyzer/SKILL.md"
_STAGE = "ops/cannbot-dsl-op-design/references/stage-cost-model.md"
_SKILL_SK = "model/model-infer-superkernel/SKILL.md"
_SKILL_MS = "model/model-infer-multi-stream/SKILL.md"
_MHC = "knowledge/model/inference/concepts/modules/mhc.md"
_SQCQ = "knowledge/runtime/concepts/stream_sqcq_async_mechanism.md"
_ACL = "vllm_ascend/compilation/acl_graph.py"
_EXPERTS = "vllm_ascend/ops/fused_moe/shared_experts.py"
_INDEX_META_RE = re.compile(r"[。.]?\s*\*\*本优化点.*\*\*\s*$")
_LOCAL_REF_NOTE_RE = re.compile(r"\s*（KB\s*暂未收录，保留本地）\s*$")
_RULE_COMPANION_NOTES: dict[str, str] = {
    "cv_fusion": "Companion guides: cv_fusion_pingpong.md, cv_fusion_tiling.md.",
    "flash_attention_optimization": "FA refinement of rule 28 (companion CV-fusion guides apply).",
    "mla_paged_attention_optimization": "MLA refinement of rules 28-29.",
}
_TERMINAL_CADENCES: dict[str, str] = {
    "ir_analysis_workflow": "Mandatory end-of-round pass: inspect lowered IR after rules 1-30.",
    "autotune": "Terminal pass after IR review: apply @triton.autotune to tunable constexprs.",
    "block_size_scaling": "Terminal fallback when rule 32 is skipped or rejected: scale single-axis BLOCK.",
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_for(live: LiveIngestion, repo: str, path: str, body: str) -> tuple[str, str]:
    digest = live.content_hashes.get(f"{repo}/{path}", "")
    if digest:
        return digest, "live_source_content"
    return _sha256(body), ("static_snapshot_body_hash" if live.degraded_offline else "body_hash")


def _unit(unit_id: str, title: str, tier: str, category: str, stack: str,
          applicability: str, body: str, live: LiveIngestion, *,
          repo: str, path: str, anchor: str = "",
          conditions: Mapping[str, Any] | None = None,
          inherits: Iterable[str] = (), evidence: str = "reference_doc",
          adaptation_notes: str = "") -> KnowledgeUnit:
    if not path or path.startswith(f"{repo}/"):
        raise DistillError(f"{unit_id}: source_path must be repo-relative, got {path!r}")
    if live.live_verified and f"{repo}/{path}" not in live.content_hashes:
        raise DistillError(f"{unit_id}: no verified content hash for {repo}/{path}")
    commit = live.source_commits.get(repo, PINNED_COMMITS.get(repo, ""))
    if not _COMMIT_HEX_RE.fullmatch(commit):
        raise DistillError(f"{unit_id}: missing full commit for {repo}")
    digest, verification = _hash_for(live, repo, path, body)
    return KnowledgeUnit(
        unit_id=unit_id, title=title, tier=tier, category=category,
        authoring_stack=stack, graph_runtime_applicability=applicability,
        body=body, conditions=dict(conditions or {}), inherits_from=tuple(inherits),
        evidence_strength=evidence,
        provenance={
            "source_repo": repo, "source_path": path, "source_commit": commit,
            "content_sha256": digest, "source_anchor": anchor,
            "verification": verification, "adaptation_notes": adaptation_notes or "source-derived",
        },
        token_count=0, adaptation_notes=adaptation_notes,
    )


def _base_units(live: LiveIngestion) -> list[KnowledgeUnit]:
    c = live.hardware_constants
    u = lambda *args, **kw: _unit(*args, live=live, **kw)
    skill_runs = [(path, normalize_text(text)) for path, text in live.skill_texts]
    legacy_runs = [result for _, result in skill_runs if result.preserved_legacy]
    if live.live_verified and not legacy_runs:
        raise DistillError("ingested SuperKernel skill text lost its legacy negation")
    skill_note = "; ".join(
        f"{Path(path).parent.name}:{result.classification}" for path, result in skill_runs
    )
    return [
        u("unit-ascendc-3510-fixpipe-and-ub-budget",
          "AscendC DAV_3510 1C:2V Fixpipe and SRAM budget", "L1", "axiom",
          "ascendc", "operator_kernel_agnostic",
          f"DAV_3510 pairs 1 AIC with 2 AIVs (1C:2V). Software-usable UB is {c['ub_simd']} B "
          f"(248 KiB) per AIV with the top 8 KiB system reservation already deducted, so do not "
          f"subtract 8 KiB a second time. Per-core SRAM capacities are L1 = 512 KiB, "
          f"L0A = 64 KiB, L0B = 64 KiB, and L0C = {c['l0c_3510']} B (256 KiB). On DAV_2201, "
          f"L0C is {c['l0c_2201']} B (128 KiB) while 192 KiB is the UB capacity rather than L0C. "
          "Direct L0C->UB Fixpipe transfer requires DAV_3510, AIC issue ownership, an explicit "
          "subBlockId lane target, and a valid dualDstCtl mode (0 for single-lane or explicit "
          "dual calls, 1 for even-M split, 2 for N%32 split).",
          conditions={"arch": ["2201", "3510"]}, evidence="hardware_spec",
          repo="cannbot-skills", path=_FIXPIPE, anchor="fixpipe-1c2v",
          adaptation_notes="SRAM capacity and Fixpipe routing rules derived from hardware spec"),
        u("unit-ascendc-2201-to-3510-memcopy-delta",
          "AscendC DAV_2201 to DAV_3510 memory-transfer delta", "L1", "axiom", "ascendc",
          "operator_kernel_agnostic",
          "DAV_3510 removes direct L1->GM and GM->L0A/L0B DMA paths, so Cube operands stage "
          "through GM->L1 followed by L1->L0A/L0B. DAV_3510 adds direct UB<->L1 transfers, "
          "L0C->UB Fixpipe routing, NDDMA, and dedicated SSBUF scratchpad space. DAV_2201 "
          "retains L1->GM and GM->L0A/L0B while lacking UB<->L1, L0C->UB, and SSBUF. Validate "
          "every transfer edge against the target architecture before lowering.",
          conditions={"arch": ["2201", "3510"]}, evidence="hardware_spec",
          repo="cannbot-dsl", path="docs/zh/guide/appendix.md", anchor="migration"),
        u("unit-ascendc-regbase-cube-only-roofline",
          "AscendC RegBase architecture and Cube-only roofline", "L1", "axiom",
          "ascendc", "operator_kernel_agnostic",
          f"DAV_3510 executes Vector instructions from registers (RegBase), whereas DAV_2201 is "
          f"memory-based (MemBase) without RegBase dual-issue. Advertised chip peaks sum Cube "
          f"and Vector throughput: on Ascend 950PR-32c, BF16 reaches {c['bf16_total']} TFLOPS "
          f"combined versus {c['bf16_cube']} TFLOPS Cube-only (a 12.5% gap). Always bind Cube "
          "roofline ceilings to frequency-consistent Cube-only throughput rather than combined "
          "or whitepaper-rounded totals.",
          conditions={"dtype": ["bf16", "fp16"]}, evidence="hardware_spec",
          repo="cannbot-dsl", path="docs/zh/guide/hardware-model.md", anchor="peaks"),
        u("unit-cannbot-dsl-53-operator-catalog",
          "CANNBot-DSL 53-operator catalog", "L2", "measurement", "cannbot_dsl",
          "operator_kernel_agnostic",
          f"The CANNBot-DSL 0.8.2 catalog declares {len(live.operators)} operators. Each "
          "operator card records its declared source path, optional test path, network group "
          "membership, and any temporary CI exclusion reason. Listing an operator in "
          "catalog/operators.json inventories declared coverage; it does not by itself certify "
          "numerical equivalence, a compiled native binary, or hardware-calibrated latency.",
          conditions={"catalog_size": len(live.operators)},
          repo="cannbot-dsl", path="catalog/operators.json", anchor="catalog"),
        u("unit-cannbot-dsl-k3-ds41-network-groups",
          "CANNBot-DSL K3 and DS41 network operator groups", "L2", "measurement",
          "cannbot_dsl", "operator_kernel_agnostic",
          "Upstream CANNBot-DSL groups model-specific operators under net/k3/network.json and "
          "net/ds41/network.json. The K3 group covers block_attn_res_update_rms_norm, the "
          "flash_kda family, fused_recurrent_kda_snapshot, fused_recurrent_kda_replayssm, "
          "mega_recurrent_kda, commit_recurrent_kda, and quant_mla_prolog. The DS41 group "
          "covers mixed_quant_sparse_flash_mla, quant_lightning_indexer, "
          "quant_sparse_lightning_indexer, indexer_prologue_qw, indexer_prologue_k, engram_gate, "
          "attn_epilogue, mega_mla, mega_full_index, and mega_compressor. Network membership "
          "records topology grouping rather than end-to-end verification.",
          conditions={"groups": ["k3", "ds41"]},
          repo="cannbot-dsl", path="net/k3/network.json", anchor="k3+ds41"),
        u("unit-cannbot-dsl-frontier-mhc-gdn2-moonep",
          "CANNBot-DSL frontier operators: mHC, GDN2, and MoonEP", "L2", "measurement",
          "cannbot_dsl", "operator_kernel_agnostic",
          "In mHC, HcPre executes a [T, hc_mult*D] x [hc_mult*D, (hc_mult+2)*hc_mult] Cube "
          "split-K projection fused with RMS normalization, Sinkhorn balancing, and pre-mixing "
          "while conserving total MACs across split_k; HcPost is a pure-Vector reduction across "
          "hc_mult streams with no Sinkhorn pass. GDN2 stages recurrent state and layout "
          "preparation. MoonEP comprises five communication operators (plan, prefetch_weight, "
          "reduce_grad, dispatch, and combine) whose AIV versus CCU_SCHED execution mode "
          "directly governs AIV core contention.",
          conditions={"frontier": ["mhc_hcpre_cv_splitk", "mhc_hcpost", "gdn2_prepare", "moonep"]},
          repo="cannbot-knowledge", path=_MHC, anchor="hcpre-hcpost"),
        u("unit-cannbot-dsl-simd-simt-ub-ssbuf",
          "CANNBot-DSL SIMD/SIMT UB and SSBUF capacity gates", "L1", "axiom", "cannbot_dsl",
          "operator_kernel_agnostic",
          f"On DAV_3510, software-usable UB per AIV is {c['ub_simd']} B (248 KiB) in SIMD mode; "
          f"SIMT mode reserves 32 KiB for data cache and budgets at most {c['ub_simt']} B "
          f"(216 KiB). Dedicated SSBUF capacity on DAV_3510 is {c['ssbuf_mix']} B for mixed "
          f"Cube-Vector kernels and {c['ssbuf_pure']} B for pure kernels. DAV_2201 provides no "
          "SSBUF and must reject SIMT schedules fail-closed. These SRAM limits are hardware "
          "invariants across all authoring stacks.",
          conditions={"arch": "3510", "vf_mode": ["simd", "simt"]}, evidence="hardware_spec",
          repo="cannbot-dsl", path="docs/zh/guide/compute.md", anchor="simt-ub"),
        u("unit-cannbot-dsl-ringbuffer-vf-fusion-lowering",
          "CANNBot-DSL RingBuffer auto-sync, VF fusion, and AscendC lowering", "L2", "sop",
          "cannbot_dsl", "operator_kernel_agnostic",
          "CANNBot-DSL lowers Python AST through CANNIR (MLIR), Vector-Function (VF) fusion with "
          "RingBuffer automatic synchronization, generated AscendC (.asc), and BiSheng targeting "
          "dav-3510. Keep CANNBOTDSL_AUTO_BUFFER_SYNC enabled in production builds because "
          "disabling automatic synchronization silently corrupts tensor data instead of raising "
          "a compile error. Set CANNBOTDSL_DUMP_ASCENDC=1 to inspect generated AscendC source.",
          conditions={"lowering": "python_ast_to_ascendc"},
          repo="cannbot-dsl", path="docs/zh/guide/compiler-options.md", anchor="autosync"),
        u("unit-cannbot-dsl-stage-cost-recurrence-law",
          "CANNBot-DSL steady-state recurrence and finite-slot schedule law", "L1", "axiom",
          "cannbot_dsl", "operator_kernel_agnostic",
          "For loop-carried recurrent state with cycle latency L across d >= 1 advance steps, "
          "steady-state initiation interval satisfies tau_steady >= max(max_pipe_load, "
          "max_cycle(sum_latency / sum_distance)). Applying the affine bound first + (N - 1)*tau "
          "requires a feasible periodic schedule with sufficient buffer credits. Finite-slot "
          "ring buffers follow exact max-plus completion recurrences: three 100 us stages over "
          "N = 4 steps with Q = 2 slots complete in 700 us rather than the 750 us affine "
          "estimate. Here N counts sequential advances of one state owner rather than "
          "independent barrier waves.",
          conditions={"op_class": "stateful_recurrent"},
          repo="cannbot-skills", path=_STAGE, anchor="tau-law"),
        u("unit-triton-ascend-33-point-lowering-catalog",
          "Triton-Ascend 33-rule latency optimization catalog", "L2", "sop", "triton_ascend",
          "operator_kernel_agnostic",
          f"The Triton-Ascend optimization catalog defines {len(TRITON_RULES)} ordered rules. "
          "Evaluate rules 1-30 sequentially by matching each kernel against the rule's "
          "applicability scope and trigger condition before loading its reference guide. "
          "Rules 31 (IR analysis), 32 (Autotune), and 33 (static Block Size Scaling fallback "
          "when rule 32 is skipped or rejected) execute as terminal passes at the end of an "
          "optimization round.",
          conditions={"rules": len(TRITON_RULES)}, repo="cannbot-skills", path=_INDEX, anchor="index"),
        u("unit-triton-ascend-cv-pingpong-gather-paths",
          "Triton-Ascend Cube-Vector overlap metrics and gather-to-dot routing", "L2", "sop",
          "triton_ascend", "operator_kernel_agnostic",
          "Falling back to GM intermediates or omitting Cube-Vector ping-pong buffering "
          "serializes AIC and AIV execution. Report both the observed overlap ratio "
          "(aic_busy + aiv_busy) / wall_time and the theoretical overlap ceiling "
          "(aic_busy + aiv_busy) / max(aic_busy, aiv_busy) without clamping the denominator to "
          "force a > 1.5 trigger. For random gather feeding tl.dot, choose among direct "
          "gather_out_to_ub, in-kernel L2-contiguous staging when the per-core working set fits "
          "in L2, or device_gather_workspace with a two-kernel HBM round trip.",
          conditions={"hazards": ["cv_serialism", "random_gather_to_dot"]},
          repo="cannbot-skills", path=_INDEX, anchor="rules-4-28-29"),
        u("unit-triton-ascend-mfu-five-gate-regime",
          "Triton-Ascend five-gate MFU validation and cache/HBM regime tagging", "L1",
          "measurement", "triton_ascend", "operator_kernel_agnostic",
          "Valid Triton-Ascend MFU reporting requires passing five gates: total input bytes "
          "within budget, a traceable reference Model with FLOPs > 0, error-free ModelNew NPU "
          "execution, single-run latency within the profiling budget, and whole-operator "
          "effective bandwidth (whole-op traffic / whole-op wall time) within the active memory "
          "service ceiling. Tag L2-cache-resident and HBM-bound regimes separately, and "
          "distinguish empirical measurements from caller-supplied assumptions.",
          conditions={"gates": list(MFU_GATES)}, evidence="reference_doc",
          repo="cannbot-skills", path=_MFU, anchor="five-gates"),
        u("unit-cannbot-knowledge-stream-sqcq-event-model",
          "Ascend stream SQ/CQ queuing, capture fan-out, and event model", "L1", "axiom",
          "framework_aclgraph", "aclgraph_native",
          "Ascend stream execution dispatches asynchronously through hardware Submission and "
          "Completion Queues (SQ/CQ), where queue depth and cross-stream event waits govern "
          "replay ordering. Graph capture allocates per-batch and per-subgraph stream resources: "
          "error code 207008 indicates runtime stream/SQ/CQ exhaustion, whereas "
          "`Alloc sq cq fail` is a distinct legacy HDK/CANN defect resolved in HDK >= 25.5.1. "
          "Event synchronization overhead (~1.5 us) and the 2,048-entry queue capacity are "
          "runtime model defaults rather than immutable architectural constants.",
          conditions={"runtime": ["stream", "sqcq", "event"]}, evidence="reference_doc",
          repo="cannbot-knowledge", path=_SQCQ, anchor="sqcq-event"),
        u("unit-aclgraph-wrapper-piecewise-full-updatable",
          "ACLGraphWrapper, BreakableACLGraphWrapper, and UpdatableGraph", "L2", "sop",
          "framework_aclgraph", "aclgraph_native",
          "In vLLM-Ascend, ACLGraphWrapper dispatches PIECEWISE, FULL, or FULL_DECODE_ONLY "
          "modes and caches one NPUGraph instance per BatchDescriptor. UpdatableGraph replays "
          "in-place parameter updates on update_stream. Before FULL replay, the runtime "
          "synchronizes the stream when enable_enpu is false and is_draft_eagle "
          "(is_draft_model and use_eagle) is false, so a main model with use_eagle=True still "
          "synchronizes. PIECEWISE mode splits capture around uncapturable operators and "
          "inserts stream joins at piece boundaries.",
          conditions={"backend": "aclgraph"}, repo="vllm-ascend", path=_ACL,
          anchor="wrapper"),
        u("unit-aclgraph-superkernel-dcci-207008",
          "ACLGraph SuperKernel dcci configuration and 207008 triage", "L2", "sop",
          "framework_aclgraph", "aclgraph_native",
          "vLLM-Ascend enables SuperKernel optimization after ACLGraph capture via "
          "super_kernel_optimize({'dcci_after_kernel_end': ['.*']}). Eligibility requires "
          "static decode shapes, fully capturable operators, and zero host-synchronizing "
          "dynamic tiling, with graph mode validated before applying the SuperKernel launch "
          "floor. Treat error 207008 (stream/SQ/CQ resource exhaustion) separately from "
          "`Alloc sq cq fail` (legacy HDK < 25.5.1 driver failure) during triage.",
          conditions={"backend": "aclgraph", "phase": "decode"}, repo="vllm-ascend",
          path=_ACL, anchor="superkernel"),
        u("unit-aclgraph-multistream-a1-a5",
          "ACLGraph A1-A5 evidence-graded multi-stream diagnostics", "L3", "workflow",
          "framework_aclgraph", "aclgraph_native",
          "Multi-stream overlap diagnostics grade five failure modes (A1-A5) by trace evidence. "
          "A1 flags same-stream collapse directly from identical observed stream IDs. "
          "A2 inspects distinct streams with <= 5% overlap and confirms a premature event wait "
          "only when binding event delay and producer-consumer causality are observed; "
          "non-binding release events remain inconclusive. "
          "A3 diagnoses AIC/AIV core oversubscription, attributing Vector contention to HCCL "
          "AIV expansion only when a live HCCL collective is active and re-solving shared AIV "
          "waves for the CCU_SCHED speedup estimate. "
          "A4 evaluates shared memory-bandwidth contention plus cross-stream event overhead. "
          "A5 flags capture-stream or pool hazards only when an explicit capture root pairs "
          "with an unjoined branch or aliased buffer, since a non-zero stream ID alone is benign.",
          conditions={"backend": "aclgraph"}, repo="vllm-ascend", path=_ACL, anchor="a1-a5"),
        u("unit-aclgraph-shared-experts-milestones",
          "ACLGraph shared-experts multi-stream synchronization milestones", "L3", "workflow",
          "framework_aclgraph", "aclgraph_native",
          "AscendSharedExperts schedules shared-expert computation on a dedicated secondary "
          "stream via npu_stream_switch. Omitting a required multi-stream synchronization "
          "milestone raises a fail-fast RuntimeError instead of silently recording a premature "
          "event, while FusedMC2 without fine-grained events falls back to a coarse conditional "
          "stage wait or skip. When an upstream producer fails to stage its milestone, stream "
          "overlap collapses openly rather than masking an early EventRecord.",
          conditions={"module": "AscendSharedExperts"}, repo="vllm-ascend",
          path=_EXPERTS, anchor="milestones"),
        u("unit-torchair-to-aclgraph-normalization",
          "TorchAir/ge_graph to ACLGraph semantic normalization rules", "L2", "sop",
          "framework_aclgraph", "aclgraph_adapted_from_torchair",
          "Before applying any of the seven positive normalization rules, any sentence carrying "
          "Chinese or English restriction, exclusion, contrast, or negation preserves legacy "
          "claims verbatim as torchair_legacy_only, including 'SuperKernel does not support "
          "aclgraph'. Positive legacy constructs (exe_mode ge_graph, npu_create_tagged_stream, "
          "Notify_Wait, torchair npu_stream_switch, limit_core_num, and neutral ge_graph "
          "references) normalize to vLLM-Ascend equivalents, while pure kernel SRAM rules "
          f"remain runtime-agnostic. Ingested skill classifications: {skill_note}.",
          conditions={"rules": len(NORMALIZATION_RULES),
                      "skill_classifications": tuple(result.classification for _, result in skill_runs),
                      "legacy_preserved": bool(legacy_runs)},
          repo="cannbot-skills", path=_SKILL_SK, anchor="normalization",
          adaptation_notes="negation-preserving rewrite; original text retained"),
        u("unit-torchair-ge-only-superkernel-claim",
          "Preserved legacy TorchAir/GE-only SuperKernel claim", "L1", "axiom",
          "framework_aclgraph", "torchair_legacy_only",
          "The pinned skill states verbatim: 'SuperKernel 仅支持 ge_graph 模式，不支持 aclgraph。' "
          "This historical TorchAir/GE constraint is retained untouched as a "
          "torchair_legacy_only normalization input; it is neither rewritten into a "
          "contradictory statement nor applied to vLLM-Ascend, which supports post-capture "
          "SuperKernel optimization via super_kernel_optimize.",
          conditions={"legacy": True}, repo="cannbot-skills", path=_SKILL_SK,
          anchor="legacy-claim", adaptation_notes="verbatim legacy claim preserved"),
    ]


def _operator_units(live: LiveIngestion) -> list[KnowledgeUnit]:
    units = []
    for index, name in enumerate(live.operators, start=1):
        coverage = operator_classification(name)
        groups = [group for group in ("k3", "ds41")
                  if name in (live.k3 if group == "k3" else live.ds41)] or ["common"]
        source = live.operator_sources.get(name, "")
        test = live.operator_tests.get(name, "")
        ci = live.operator_ci.get(name, "")
        source_clause = f"declared at `{source}`" if source else "declared in `catalog/operators.json`"
        test_clause = f"test `{test}`" if test else "no dedicated test path declared"
        ci_clean = ci.strip().rstrip(".")
        if not ci_clean:
            ci_sentence = "CI status: active (no temporary exclusion listed)."
        elif ci_clean.lower().startswith("temporary"):
            ci_sentence = f"{ci_clean[0].upper() + ci_clean[1:]}."
        else:
            ci_sentence = f"CI exclusion: {ci_clean}."
        body = (
            f"CANNBot-DSL catalog operator {index}/{len(live.operators)} `{name}` is "
            f"{source_clause} ({test_clause}; network group: {'/'.join(groups)}). "
            f"{ci_sentence} "
            "Lowering pipeline: Python AST -> CANNIR (MLIR) -> VF fusion and RingBuffer "
            "auto-sync -> AscendC (.asc) -> BiSheng dav-3510. "
            f"Math-model classification: {coverage['catalog_classification']} "
            f"(derivation coverage: {coverage['derivation_coverage']}); latency estimates "
            "remain analytic priors pending hardware calibration."
        )
        units.append(_unit(
            f"unit-cannbot-dsl-op-{_slug(name)}", f"CANNBot-DSL operator card: {name}",
            "L1", "measurement", "cannbot_dsl", "operator_kernel_agnostic", body, live,
            conditions={"operator": name, "network": tuple(groups), "source": source,
                        "test": test, "ci_excluded": bool(ci), **coverage},
            inherits=("unit-cannbot-dsl-53-operator-catalog",),
            repo="cannbot-dsl", path="catalog/operators.json", anchor=f"operator={name}",
            adaptation_notes="source/test/CI metadata preserved from operators.json",
        ))
    return units


def _format_rule_trigger(slug: str, raw_trigger: str) -> str:
    cleaned = _INDEX_META_RE.sub("", raw_trigger.strip()).strip().rstrip("。.")
    if not cleaned:
        return "offline snapshot (inspect Index.md in live corpus)"
    companion = _RULE_COMPANION_NOTES.get(slug, "")
    return f"{cleaned}. {companion}".strip() if companion else f"{cleaned}."


def _rule_cadence(slug: str) -> str:
    return _TERMINAL_CADENCES.get(
        slug,
        "Evaluate rules 1-30 in order; load the reference only on trigger match.",
    )


def _rule_units(live: LiveIngestion) -> list[KnowledgeUnit]:
    by_number = {row.number: row for row in live.triton_rules}
    units = []
    for rule in TRITON_RULES:
        row = by_number.get(rule.number)
        if row is None:
            raise DistillError(f"missing Triton rule row {rule.number}")
        raw_title = row.title.strip()
        title_text = rule.title if (not raw_title or raw_title == f"rule-{rule.number}") else raw_title
        applies = row.applicability.strip().rstrip("。.") or "offline snapshot (inspect Index.md in live corpus)"
        trigger_text = _format_rule_trigger(rule.slug, row.trigger)
        raw_ref = row.reference.strip().rstrip("。.")
        ref = _LOCAL_REF_NOTE_RE.sub(" (local ref)", raw_ref) if raw_ref else f"{_INDEX}#rule-{rule.number}"
        body = (
            f"Rule {rule.number}/{len(TRITON_RULES)} (`{rule.slug}`, {rule.category}). "
            f"Applies when: {applies}. "
            f"Trigger: {trigger_text} "
            f"Reference: {ref}. "
            f"{_rule_cadence(rule.slug)}"
        )
        units.append(_unit(
            f"unit-triton-ascend-rule-{rule.number:02d}-{_slug(rule.slug)}",
            f"Triton-Ascend rule {rule.number}/{len(TRITON_RULES)}: {title_text}", "L2", "sop",
            "triton_ascend", "operator_kernel_agnostic", body, live,
            conditions={"triton_rule": rule.number, "slug": rule.slug,
                        "applicability": row.applicability, "trigger": row.trigger},
            inherits=("unit-triton-ascend-33-point-lowering-catalog",),
            repo="cannbot-skills", path=_INDEX, anchor=f"rule={rule.number}",
            adaptation_notes="trigger and reference path preserved from Index.md",
        ))
    return units


def _gate_units(live: LiveIngestion) -> list[KnowledgeUnit]:
    descriptions = {
        "input_budget_ok": "total input tensor bytes fit within the configured memory budget",
        "model_ref_valid": "the PyTorch reference Model traces cleanly with positive FLOPs (FLOPs > 0)",
        "implementation_runs": "the candidate ModelNew kernel executes on the NPU without runtime errors",
        "single_run_time_budget_ok": "single-invocation latency stays within the profiling time budget",
        "effective_bw_within_hbm": "whole-operator effective bandwidth stays within the active memory service ceiling",
    }
    return [
        _unit(
            f"unit-triton-ascend-mfu-gate-{_slug(gate)}", f"Triton-Ascend MFU gate: {gate}",
            "L1", "measurement", "triton_ascend", "operator_kernel_agnostic",
            f"Triton-Ascend MFU validation gate `{gate}` requires that {descriptions[gate]}. "
            "Credible MFU comparisons require all five gates to pass under one consistent "
            "byte-accounting scope and a matching L2-cache or HBM regime tag. Record whether "
            "the gate was computed analytically, assumed by the caller, or measured empirically.",
            live, conditions={"mfu_gate": gate},
            inherits=("unit-triton-ascend-mfu-five-gate-regime",),
            repo="cannbot-skills", path=_MFU, anchor=f"gate={gate}",
            adaptation_notes="five-gate validation contract derived from triton-mfu-analyzer/SKILL.md",
        )
        for gate in MFU_GATES
    ]


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:CORPUS_CONTRACT["slug_max_chars"]] or "item"


def distill_units(live: LiveIngestion | None = None, *,
                  include_aclgraph: bool = True,
                  source_commit: str | None = None,
                  allow_offline: bool = False) -> tuple[KnowledgeUnit, ...]:
    """Distill all authored stacks plus framework_aclgraph in one pass."""
    for name, value in (("include_aclgraph", include_aclgraph), ("allow_offline", allow_offline)):
        if not isinstance(value, bool):
            raise DistillError(f"{name} must be bool")
    ingestion = live
    if isinstance(ingestion, Mapping):
        ingestion = LiveIngestion(**dict(ingestion))
    if ingestion is None:
        try:
            ingestion = scan_live(allow_offline=allow_offline)
        except TimeoutError:
            raise
        except (DistillError, OSError):
            if not allow_offline:
                raise
            ingestion = static_ingestion(degraded_offline=True)
    if source_commit and source_commit != ingestion.source_commits["cannbot-dsl"]:
        raise DistillError("source_commit override does not match DSL provenance")
    units = _base_units(ingestion)
    if not include_aclgraph:
        units = [unit for unit in units if unit.authoring_stack != "framework_aclgraph"]
    units.extend(_operator_units(ingestion))
    units.extend(_rule_units(ingestion))
    units.extend(_gate_units(ingestion))
    records = validate_units(units)
    base_expected = CORPUS_CONTRACT["expected_base_units"]
    acl_expected = CORPUS_CONTRACT["expected_aclgraph_units"]
    core_expected = base_expected - (0 if include_aclgraph else acl_expected)
    expected = core_expected + len(STATIC_OPERATORS) + len(TRITON_RULES) + len(MFU_GATES)
    if len(records) != expected:
        raise KnowledgeUnitError(f"expected {expected} units, got {len(records)}")
    stacks = {unit.authoring_stack for unit in records}
    required_stacks = set(AUTHORING_STACKS) - (set() if include_aclgraph else {"framework_aclgraph"})
    if not required_stacks.issubset(stacks):
        raise KnowledgeUnitError(f"missing authoring stacks: {sorted(set(AUTHORING_STACKS) - stacks)}")
    return records


def distill_static() -> tuple[KnowledgeUnit, ...]:
    return distill_units(static_ingestion())


def distill_live(refs_root: str | Path | None = None,
                 vllm_root: str | Path | None = None, *,
                 allow_offline: bool = False) -> tuple[KnowledgeUnit, ...]:
    if not isinstance(allow_offline, bool):
        raise DistillError("allow_offline must be bool")
    return distill_units(scan_live(refs_root, vllm_root, allow_offline=allow_offline),
                         allow_offline=allow_offline)


def stack_counts(units: Iterable[KnowledgeUnit]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for unit in units:
        counts[unit.authoring_stack] = counts.get(unit.authoring_stack, 0) + 1
    return dict(sorted(counts.items()))


def write_units(units: Iterable[KnowledgeUnit], directory: str | Path) -> Path:
    target = _write_units(units, directory)
    if target.name == "SKILL.md" or any(path.name == "SKILL.md" for path in target.parent.iterdir()):
        raise KnowledgeUnitError("distiller must never emit a Harness SKILL.md")
    return target


__all__ = [
    "DEFAULT_REFS_ROOT", "DEFAULT_VLLM_ROOT", "DistillError", "LiveIngestion",
    "NORMALIZATION_RULES", "NormalizationResult", "NormalizationRule",
    "SOURCE_COMMIT", "STATIC_DS41", "STATIC_K3", "STATIC_OPERATORS",
    "TritonRuleRow", "distill_live", "distill_static", "distill_units",
    "normalize_text", "normalize_texts", "scan_live", "stack_counts",
    "static_ingestion", "write_units",
]
