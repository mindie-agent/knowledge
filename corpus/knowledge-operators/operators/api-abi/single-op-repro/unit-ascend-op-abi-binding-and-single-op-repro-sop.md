---
category: sop
conditions:
  accelerator: ascend
  cann: '>=9.0,<10'
  execution_mode: aclgraph
  framework:
  - vllm-ascend>=0.10,<0.12
  measurement_gear: deep
  soc:
  - Ascend910B
  - Ascend910B4
  - Ascend910_93
  - Ascend950
domain_path: operators/api-abi/single-op-repro
evidence_strength: hardware_spec
id: unit-ascend-op-abi-binding-and-single-op-repro-sop
inherits_from: []
provenance:
  evidence_refs:
  - cann/ops-transformer@4891cf6a210bf08a4a9bb26f7c1e2277ed43c860:attention/fused_infer_attention_score/docs/aclnnFusedInferAttentionScoreV3.md
  - vllm-project/vllm-ascend@175ef4d49c2072f9233619636f0309f4f88b4e2c:vllm_ascend/device/device_op.py
  source_digest: 95a54b37c47aebeaafa09d3fec860ed53d91a8f83de364619094f85599367cb6
  source_locator: vllm-project/vllm-ascend@175ef4d49c2072f9233619636f0309f4f88b4e2c:vllm_ascend/device/device_op.py
  source_type: profiling_tool_and_operator_catalog_contract
revision: 3
schema: mindie-graded-unit/1
status: active
supersedes: []
tier: L2
title: Named operator ABI binding, FRACTAL_NZ storage, and single-op reproduction
triggers:
  error_signatures:
  - msprof op_summary positional Input Shapes cannot be mapped back to named API arguments
  - repro script passes an ND tensor to a FRACTAL_NZ slot or fails with format mismatch
  - actual_seq_lens_q is non-monotonic or its last element differs from queryT
  - block_table contains -1, an unallocated block id, or too few columns
  - standalone repro is more than 2x slower than the captured operator without a clock
    or shape change
  keywords:
  - mindie-profiler inspect-op-abi
  - mindie-profiler repro-op
  - op_api_catalog.json
  - MSPTI tensor descriptor
  - named ABI
  - FRACTAL_NZ
  - format 29
  - actual_seq_lens
  - group_list
  - block_table
  - slot_mapping
  - single-op reproduction
  - workspace
---
# Named operator ABI binding, FRACTAL_NZ storage, and single-op reproduction

## Capability Gate

`inspect-op-abi`, `repro-op`, and `fabric-probe` are version-gated `mindie-profiler` subcommands that may be absent from older installations. Probe availability before invoking them:

```bash
mindie-profiler inspect-op-abi --help >/dev/null 2>&1 || echo unavailable
```

If the probe prints `unavailable`, do not pass `--op`, `--cann-version`, `--occurrence`, or `--output-dir` to the installed binary. Instead, run `mindie-profiler capture --gear deep --output capture.json -- <serve cmd>`, analyze the target window with `mindie-profiler analyze --root <capture> --step-window <STEP=START:END> --output step-analysis.json`, inspect the operator via `kernels` or `export-sim-case`, and author the standalone reproducer manually.

On builds exposing these subcommands, invoke `inspect-op-abi --op OP --cann-version VERSION` (returning an `ABIInspection` record with overload identity, argument synopsis, padding mode, completeness, and constraint continuation) and `repro-op --capture capture.json --occurrence RUN:RANK:STEP:INDEX --output-dir repro` (emitting `repro.py`, a binding manifest, and provenance digests). Validate the returned schema version before parsing.

## Bind Positional Trace Slots to Named Arguments

- Map MSPTI or `msprof` positional descriptors strictly by catalog argument order and tensor-list span—never by shape or dtype similarity. Distinguish `absent` (API null), `undefined` (`DT_UNDEFINED` / `NULL`), scalar (`shape=[]` with concrete dtype), and zero-sized tensors (`0` in `shape`). Mark missing required slots `unbound_required`, and ensure optional holes never shift subsequent positional tensors.
- Keep scalar attributes in their dedicated attribute namespace rather than inventing tensor indices; compute a binding digest only when every required argument resolves.
- Recover exact bindings for `query`, `key`, `value`, `query_rope`, `key_rope`, `weight`, `antiquant_scale`, `actual_seq_lens_q`, `actual_seq_lens_kv`, `seqused_k`, `group_list`, `block_table`, and `slot_mapping`. Report unrecognized overloads as `unknown_abi` with raw trace descriptors rather than guessing parameter names.

## Length, Padding, and Boundary Legality

`Q` and `KV` sequence-length domains are independent and follow `unit-ascend-aclgraph-bucket-padding-and-varlen-axioms`@3:
- `TND` query lengths are cumulative and end at physical packed `T`; paged `TND` KV lengths are per-request (`<= KV_S`), whereas non-paged `TND` KV lengths are cumulative.
- `BSH` / `BSND` / `BNSD` lengths are per-batch `S_b`, and paged KV requires `ceil(kv_len / block_size)` allocated `block_table` columns.
- Resolve `group_list` from the catalog as either cumulative offsets or per-group counts whose active prefix equals real routed tokens rather than `M_b`.
- Keep active `slot_mapping` indices unique and `< num_blocks * block_size` (using only ABI-declared sentinels on padded rows), and align `atten_mask`, `prompt_length`, `kv_length`, and `softmax_lse` with the selected mask mode.

## `FRACTAL_NZ` Physical Storage

`ND` and `FRACTAL_NZ` (format 29) have distinct physical layouts. For logical `[N, D]` (preserving leading batch dimensions):

```text
storage = [ceil(D / C0), ceil(N / 16), 16, C0]
C0      = 32 / dtype_bytes        # 16 bf16/fp16, 32 int8/fp8
```

A logical `[17, 17]` BF16 matrix allocates `[2, 2, 16, 16]` = 1024 elements (2048 bytes), not 544. An ATB `Transdata` NZ descriptor expresses the blocked storage as rank-4 `[batch, ceil(D/C0), ceil(N/16)*16, C0]`: for `[17, 17]` BF16 that is `[1, 2, 32, 16]` = 1,024 elements, never `[1, 32, 32]`. Materialize NZ inputs via `torch_npu.npu_format_cast(x, 29)` or an explicit storage descriptor while keeping the backing tensor live; never `.view()` NZ storage as ND (`format_mismatch`), and preserve 512-byte GM page alignment and 32-byte vector alignment.

## Reproduce One Captured Occurrence

1. Pin an immutable occurrence (`RUN:RANK:STEP:INDEX`) rather than an aggregated summary row, preserving dtype, format, strides, storage offset, aliasing, and scalar attributes.
2. Populate value-sensitive inputs (`group_list`, quantization scales, sparse indices, numerical edge cases) from captured snapshots; otherwise generate seeded legal inputs labeled `descriptor_synthetic`.
3. Enforce pre-launch checks for `format_mismatch`, `varlen_invariant`, `paged_invariant`, `group_invariant`, and `slot_misalignment`, failing closed on missing required scalars.
4. Execute under `SteadyStateGate` with explicit warmup and stream synchronization outside kernel timing (excluding setup, allocation, and cache flushes), reporting P50/P90, sample count, gate settings, cache state (`warm` or `cold_verified`), and numerical status separately.
5. Accept timing reproduction when P50 matches the captured occurrence within 5% at identical clocks and shapes (a `>= 2x` discrepancy typically indicates `M_b` vs. `M_l`, an `ND`/`NZ` mismatch, or altered workspace tiling; numerically invalid runs always fail).

## Boundary

Catalog coverage is finite—add reviewed catalog entries rather than guessing uncatalogued ABIs—and script emission alone never substitutes for verified NPU execution.
