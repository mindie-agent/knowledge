---
category: axiom
conditions:
  accelerator: ascend
  cann: '>=9.0,<10'
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
id: unit-ascend-aclgraph-bucket-padding-and-varlen-axioms
inherits_from: []
provenance:
  evidence_refs:
  - cann/ops-transformer@4891cf6a210bf08a4a9bb26f7c1e2277ed43c860:attention/fused_infer_attention_score/docs/aclnnFusedInferAttentionScoreV3.md
  - gitcode.com/cann/ops-transformer@308a15f56d4af213e86871b8fb9747e1494571dd:attention/quant_lightning_indexer/docs/aclnnQuantLightningIndexer.md
  source_digest: cbfc50be1e2eef6c7f9b366a780b1bf7937c1e38eb38f7979c060a5baa05a598
  source_locator: cann/ops-transformer@4891cf6a210bf08a4a9bb26f7c1e2277ed43c860:attention/fused_infer_attention_score/docs/aclnnFusedInferAttentionScoreV3.md
  source_type: operator_abi_and_graph_capture_contract
revision: 3
schema: mindie-graded-unit/1
status: active
supersedes: []
tier: L1
title: ACLGraph bucket padding classes and exact varlen operator contracts
triggers:
  error_signatures:
  - queryT (12) != last element of actualSequenceLengthQ (9)
  - grouped matmul reads padded tail rows because group_list ends at M_bucket
  - ACLGraph replay latency is flat inside a bucket and jumps at the next bucket
  - AllReduce or AllGather time is unchanged when the logical token count drops
  keywords:
  - aclgraph
  - bucket padding
  - masked_varlen
  - grouped_prefix
  - full_padded_tensor
  - actual_seq_lens_q
  - actual_seq_lens_kv
  - FusedInferAttentionScore
  - FlashAttentionVarLenScore
  - QuantLightningIndexer
  - GroupedMatmulV4
  - group_list
  - M_bucket
  - M_logical
---
# ACLGraph bucket padding classes and exact varlen operator contracts

## Condition

Applies to Ascend 910B, A3, and A5 under `vllm-ascend` ACLGraph capture, where `M_b` is the captured bucket width and `M_l` is the logical token count (`M_b >= M_l >= 0`). Because captured tensor shapes are static, each operator either filters padded phantom rows via declared sequence lengths, bounds them via a routing prefix, or executes across the full padded tensor.

## Three Padding Classes

| Class | Representative Operators | Scales with `M_b` | Scales with `M_l` |
| --- | --- | --- | --- |
| `masked_varlen` | FIA V3 / `npu_fused_infer_attention_score`, `FlashAttentionVarLenScore`, `QuantLightningIndexer`, paged attention | Tiling metadata, workspace, output buffer, block table | Valid token pairs and touched KV blocks |
| `grouped_prefix` | `GroupedMatmulV4`, `GroupedMatmulSwigluQuant`, dispatch/combine V3 | Static allocations, HCCL window, padded capacity | Active rows indexed by `group_list` |
| `full_padded_tensor` | `Matmul`, `RmsNorm`, `AddRmsNormDynamicQuant`, `SwiGlu`, bucketed collectives | Every padded row and byte | None |

Classify per overload and CANN release rather than by operator name alone: a compound operator can mask its attention core while executing its projection epilogue across all `M_b` rows.

## Operator-Specific Varlen Contracts

- **FIA V3 / `npu_fused_infer_attention_score` (`TND`).** `actual_seq_lens_q` is a nondecreasing 1-D `int64` cumulative prefix sum whose final element must equal the physical packed query total `T`. In non-paged `TND`, `actual_seq_lens_kv` is likewise cumulative up to packed KV total; in paged TND, `actual_seq_lens_kv` stores per-request KV lengths (each `<= KV_S`, matching KV batch length) rather than a cumulative sum. When `T = M_b > M_l`, partition the padded tail `M_b - M_l` into `ceil((M_b - M_l) / q_max)` dummy request entries, each at most the overload's per-request `q_max` (for instance, A2 MLA `D=512` restricts `q_len in [1, 16]`, splitting `31 = 16 + 15`, never one 31-token request). Append those cumulative offsets after `M_l` so `actual_seq_lens_q[-1] = M_b`, zero-fill and mask each dummy request, and omit its `slot_mapping` writes; leaving `actual_seq_lens_q[-1] = M_l` triggers `queryT != actualSequenceLengthQ[-1]`. Emit zero-length entries only when explicitly permitted by the overload.
- **`BSH` / `BSND` / `BNSD`.** These batch-major layouts belong to `aclnnFusedInferAttentionScoreV2`/`V3` and `aclnnFlashAttentionScore`, never `aclnnFlashAttentionVarLenScore` (or V2/V4). Entries record per-batch sequence lengths `S_b` with no cumulative tail-equals-`T` constraint; pad unused batch slots with `S_b = 0` when supported, or else with a masked dummy sequence backed by an allocated block.
- **`aclnnFlashAttentionVarLenScore` (and V2/V4).** Supports **only `TND`**; there is no `BSH`/`BSND`/`BNSD` overload. Apply the cumulative `T = M_b` and `q_max` tail-chunking contract above, and check its dedicated mask and `sparse_mode` signature rather than assuming FIA `preTokens`/`nextTokens` defaults.
- **Paged KV.** Supply per-request `actual_seq_lens_kv` and allocate at least `ceil(kv_len / block_size)` `block_table` columns plus a valid dummy block for padded slots; use `-1` only where the operator ABI explicitly defines a sentinel.
- **`aclnnQuantLightningIndexer`.** Emits raw `int32` indices shaped `[T1, N2, sparseCount]` (`TND`) or `[B, S1, N2, sparseCount]` (`BSND`) without head-axis squeezing. Query lengths are nonnegative (cumulative in `TND`, per-request in `BSND`), and key lengths are per-request in paged `TND` and cumulative only in non-paged `TND` (zero lengths require explicit ABI support). `sparseCount <= 2048` is the ABI cap; `compress_ratio` is a model-side selection (`min(topk, ceil(seq_len / compress_ratio))`), not an ABI field. Emit trailing `-1` indices only when the downstream sparse-attention kernel masks them or bounds reads via `actual_seq_lens_kv`.
- **`group_list`.** Confirm from the catalog whether `group_list` encodes cumulative offsets (final entry equals active rows) or per-group counts (sum equals active rows); ending at `M_b` routes padded rows into experts.

## Bucket Padding Tax

```text
M_eff = M_l | active_prefix(group_list) | M_b
T_op  = T_launch + T_fixed(M_b) + max(FLOPs(M_eff)/P_cube,
        Bytes(M_eff)/B_hbm, Cycles_vec(M_eff)/P_vector)
tax   = (T_step(M_b) - T_step(M_l)) / T_step(M_l)
```

At `M_l = 3` inside `M_b = 16`, `full_padded_tensor` kernels and bucketed collectives pay `> 5x` overhead while `masked_varlen` attention adds near-zero work. Flat step latency within a bucket followed by discrete step-ups—or invariant `AllReduce` time as `M_l` drops—diagnoses full-tensor padding, whereas `expert_token_num > M_l * top_k` diagnoses an unclipped `group_list`.

## Boundary

Covers ACLGraph capture only (excluding eager shapes and undocumented sentinels). Resolve layout and length rules from the versioned operator catalog, paired with `unit-cann9-steady-state-capture-and-clock-hygiene`.
