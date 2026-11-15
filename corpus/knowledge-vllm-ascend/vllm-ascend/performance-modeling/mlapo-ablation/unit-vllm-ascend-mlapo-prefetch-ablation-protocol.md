---
category: workflow
conditions:
  accelerator: ascend
  cann: '>=9.0,<10'
  execution_mode: aclgraph
  framework:
  - vllm-ascend>=0.10,<0.12
  model_family:
  - deepseek-v3
  - deepseek-v4
  soc:
  - Ascend910B
  - Ascend910B4
  - Ascend910_93
  - Ascend950
  workload_phase: decode
domain_path: vllm-ascend/performance-modeling/mlapo-ablation
evidence_strength: hypothesis
id: unit-vllm-ascend-mlapo-prefetch-ablation-protocol
inherits_from:
- unit-vllm-ascend-hbm-memory-hidden-assassins-axioms
provenance:
  evidence_refs:
  - vllm-project/vllm-ascend@175ef4d49c2072f9233619636f0309f4f88b4e2c:vllm_ascend/device/device_op.py
  - vllm-project/vllm-ascend@175ef4d49c2072f9233619636f0309f4f88b4e2c:vllm_ascend/attention/mla_v1.py
  - knowledge@d5c7f01718bd508906d0eef1227b43697fa77912:mindie_knowledge/graded/ablation.py
  experiment_status: not_run_source_only
  source_digest: 21a310549545013b3d2e57deac73aae7db06cb80f31236d7a9d38067bbcd4f96
  source_locator: vllm-project/vllm-ascend@175ef4d49c2072f9233619636f0309f4f88b4e2c:vllm_ascend/attention/mla_v1.py
  source_type: source_only_optimization_verification_protocol
revision: 3
schema: mindie-graded-unit/1
status: active
supersedes: []
tier: L3
title: MLAPO prefetch and multi-stream promotion protocol — seven-arm ablation receipt
triggers:
  error_signatures:
  - four-factor candidate table has only three leave-one-out arms
  - interaction is dropped because one factor is noisy in isolation
  - prefetch gain is claimed without residency or foreground non-regression evidence
  - final combination is promoted without a baseline-bound receipt
  keywords:
  - seven-arm ablation
  - baseline
  - full_candidate
  - minus_A
  - minus_B
  - minus_C
  - minus_D
  - final_combination
  - ablation receipt
  - prefetch residency
  - foreground non-regression
  - experiment ledger
---
# MLAPO prefetch and multi-stream promotion protocol — seven-arm ablation receipt

## Purpose

`optimize-graph` and `weight-prefetch` are version-gated passes; probe `mindie-sim optimize-graph --help` first. This L4-verification workflow (stripped in `mode="explore"`) requires steady capture, `sol`, and `mindie://vllm-ascend/units/unit-vllm-ascend-hbm-memory-hidden-assassins-axioms@2`.

## Four Candidate Factors

- **A — UB-bounded Vector fusion.** Fuse `Add` -> `RmsNorm` -> `DynamicQuant` -> `RoPE` -> `Cast`/`Transpose` into one AIV kernel with a per-core tile loop. Enforce `bytes_live + double_buffer_tile + workspace <= usable_ub_bytes`; sequential core loops mean `ceil(T_bucket/T_tile)` may exceed the core count while `launched_blocks <= available_aiv_cores` bounds launch. Preserve GM live-outs, FP32 reductions, `slot_mapping`, masks, and alias safety.
- **B — Cube prologue/epilogue absorption.** Fold `ND -> FRACTAL_NZ`, cast, and static scale into `mte1`/`mte2`, and bias, dequant scale, cast, and layout into `fixpipe` where ABI-legal (per-token scales retain a Vector stage). Preserve NZ weights; this eliminates Vector/GM round-trips, not Cube FLOPs.
- **C — Static-weight prefetch.** Read-only `wd_qkv`/`wu_q` have no activation RAW hazard, whereas the foreground Vector chain produces the Cube input and cannot overlap that same Cube op—only weight loads overlap. Separate intra-kernel pipelining, L2 hints (`mte2_l2`), and AIC L1 staging (`mte1`); without verified cross-kernel residency/staging lowering, emit `unsupported_prefetch_path` and keep the original graph.

Bound prefetched bytes by `P = min(W, C_free, floor(integral b_prefetch(t) dt))` under `b_prefetch(t) <= max(0, B_HBM(t) - b_foreground(t))` and `sum resident_bytes(t) <= C_free(t)`. Foreground wins and evicted bytes never count as hits (`W - P` stays on HBM; never claim unmeasured 5–12 us gains or `>90%` idle HBM).

- **D — Shared-HBM multi-stream overlap.** Enforce `B_total = B_vector_bytes + B_cube_bytes <= B_hbm_bytes`, `T_overlap >= max(T_vector, T_cube)`, and `T_overlap >= B_total / BW_hbm`. HBM-saturated streams hide launch latency only; budget stream workspaces/pools and forbid intra-graph sync.

## Seven-Arm Ablation Matrix

| Arm | A | B | C | D |
| --- | --- | --- | --- | --- |
| `baseline` | off | off | off | off |
| `full_candidate` | on | on | on | on |
| `minus_A` | off | on | on | on |
| `minus_B` | on | off | on | on |
| `minus_C` | on | on | off | on |
| `minus_D` | on | on | on | off |
| `final_combination` | chosen | chosen | chosen | chosen |

Measuring `baseline`, `full_candidate`, and all four `minus_*` arms is mandatory. Choose `final_combination` after leave-one-out analysis and re-measure it against `baseline` and `full_candidate`, retaining interacting factors even when isolated deltas sit within noise.

## Receipt Contract (`verified_v2`)

Validated by `mindie_knowledge.graded.ablation.verified_v2`: keep `samples_ms` at run-level (not inside `timing`) and map `timing.buffer_sha256` as `{shape_sha256: [buffer_sha256, ...]}` rather than a flat list (`ablation-receipt-example.json`, SHA-256 `e24d5c269051f950c6e5616e214edea58b7fe106bb5d741b71054cf1faf52f06`, passes verbatim). Runs require `conditions.workload`, two-block `sample_groups` digests, `clock == "host_monotonic_ns"`, non-empty `synchronizer`, `warmup_rounds >= 1`, and synced `samples`. Final promotion binds `geomean_of_per_case_arithmetic_mean_ratios` with `uncertainty_verified`, `final_speedup_geomean`, `final_timing_sha256`, `min_speedup > 1`, and `0 <= max_relative_loss < 1`.

Run 20 warmup / 100 measured steps on identical buckets and live clocks. Require zero eager fallback, `max_abs_err <= 2e-2`, `cosine >= 0.999`, `P50 >= 1.05x`, workspace delta `<= 4 KiB`/op, HBM headroom, and zero added sync, alloc, or bucket splits.

## Execution & Boundary

Pass `test_mla_preprocess.py` and `test_mla.py`, capture `--gear fast` on matching buckets, attach `diff-trace`, and append the receipt to `docs/evidence/l4-day0-mlapo-prefetch-ablation.json`. Until hardware receipts exist, this protocol remains a decode-only MLA contract with model- and SoC-specific `C_prefetch`/`C_UB` bounds.
