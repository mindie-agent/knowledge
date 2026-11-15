---
category: axiom
conditions:
  accelerator: ascend
  cann: '>=9.0,<10'
  deployment:
  - colocated
  - pd_disaggregated
  execution_mode: aclgraph
  framework:
  - vllm-ascend>=0.10,<0.12
  soc:
  - Ascend910B
  - Ascend910B4
  - Ascend910_93
  - Ascend950
  workload_phase:
  - decode
  - prefill
domain_path: _common
evidence_strength: hardware_spec
id: unit-vllm-ascend-hbm-memory-hidden-assassins-axioms
inherits_from: []
provenance:
  evidence_refs:
  - vllm-project/vllm-ascend@175ef4d49c2072f9233619636f0309f4f88b4e2c:vllm_ascend/compilation/acl_graph.py
  - next-release-design@2026-11-14:v2-pillars-4-to-7-design.md#15.1
  source_digest: b13492d8f117f7c7bcf3721f849f273c7c3ea890ae4cd022640b8280af9538a2
  source_locator: vllm-project/vllm-ascend@175ef4d49c2072f9233619636f0309f4f88b4e2c:vllm_ascend/compilation/acl_graph.py
  source_type: allocator_and_graph_pool_contract
revision: 2
schema: mindie-graded-unit/1
status: active
supersedes: []
tier: L1
title: HBM ledger for vLLM-Ascend — owned storage, half-open lifetimes, and peak
triggers:
  error_signatures:
  - NPU out of memory immediately after increasing EP or enabling EPLB
  - profile_run passes but the first real prefill or MTP step OOMs
  - npu-smi shows large reserved memory that is neither weights nor KV cache
  - HCCL_BUFFSIZE is too small or MoeDistributeDispatchV3 tiling fails at large EP
  - ACLGraph capture OOMs only when several capture buckets are enabled
  keywords:
  - HBM ledger
  - storage_id
  - lifetime
  - HCCL_BUFFSIZE
  - MC2 arena
  - EPLB redundant experts
  - MTP draft weights
  - paged KV cache
  - index_k
  - recurrent state
  - ACLGraph pool
  - graph_pool_handle
  - workspace
  - memory_reserved
---
# HBM ledger — owned storage, half-open lifetimes, and peak

## Condition

Every prefill and decode rank must satisfy this ledger; gate feasibility on the worst-case rank.

## Peak-Live Law

```text
peak_live(rank) = max_t sum bytes of unique live storages at t
required_rank   = peak_live + explicit_fragmentation_margin
feasible        = required_rank <= usable_HBM_capacity_rank
```

Assign each physical buffer a unique `storage_id`, owner, bytes, alignment, pool, evidence kind, category, and half-open lifetime `[start, end)`. Aliases add zero bytes, release-before-allocate intervals do not overlap, and persistent buffers span the horizon. Never sum replicas across ranks or non-concurrent workspaces.

## Six Ledger Categories

1. **Weights and scales.** Dense weights divide by `TP * PP`; routed experts divide by `EP` (`expert_bytes` uses per-expert TP; `expert_dp` duplicates); shared experts, routers, norms, and biases replicate. Keep base and redundant experts disjoint (`E/EP * expert_bytes + R/EP * expert_bytes`): at `E=64, R=8, EP=8` and 1 GiB/expert, rank storage is 9 GiB, never 10. Track scales and `npu_format_cast` copies separately.
2. **MTP and EPLB.** Draft layers replicate; EPLB tables and rebalance staging carry separate lifetimes.
3. **KV, index, recurrent, metadata.** GQA/MHA uses `L * T_kv * 2 * H_kv * D_head * dtype` divided by `DCP` and KV-head sharding plus page tables; MLA uses `L * T_kv * (kv_lora_rank + qk_rope_head_dim) * dtype`. Track `index_k` and per-sequence recurrent state separately, and count shared prefixes once. `PCP` is prefill-only and does not shrink the persistent decode pool; only DCP/TP sharding does.
4. **HCCL/MC2 arenas.** Sum rank-owned `align(buffsize_g * directions_g * pipeline_slots_g + metadata_bytes_g, alignment_g)`.
5. **ACLGraph pools.** Count a shared `graph_pool_handle()` arena once and sum distinct pools; keep bucket I/O separate and treat pool workspaces as aliases.
6. **Workspaces and reserve.** Concurrent workspaces peak at `max_t sum(active_workspace_bytes(t))`, not summed per-op maxima. Charge reserve as an allocation or capacity deduction, never both.

`memory_reserved - memory_allocated` is allocator slack; fragmentation requires block stats plus a margin.

## Gate & Boundary

Populate from checkpoint, launch, quantization, plan, and `HCCL_BUFFSIZE` (`measured`/`derived`/`unknown`; reject unbounded `memory_unqualified` or oversize `memory_infeasible` plans). Reconcile after load, KV setup, capture, and first prefill (`> 0.5 GiB` gap flags unmodeled memory). Covers ownership, aliasing, and peak lifetimes; derive HCCL/KV totals from the target build.
