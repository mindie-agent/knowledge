---
category: mutator
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
domain_path: vllm-ascend/day0-graph/mla-prolog-prefetch
evidence_strength: hypothesis
id: unit-ascend-day0-vector-fusion-mlapo-prefetch-multistream-mutator
inherits_from:
- unit-vllm-ascend-hbm-memory-hidden-assassins-axioms
provenance:
  evidence_refs:
  - vllm-project/vllm-ascend@175ef4d49c2072f9233619636f0309f4f88b4e2c:vllm_ascend/attention/mla_v1.py
  experiment_status: not_run_source_only
  source_digest: 21a310549545013b3d2e57deac73aae7db06cb80f31236d7a9d38067bbcd4f96
  source_locator: vllm-project/vllm-ascend@175ef4d49c2072f9233619636f0309f4f88b4e2c:vllm_ascend/attention/mla_v1.py
  source_type: source_only_optimization_hypothesis
  supersedes: []
revision: 3
schema: mindie-graded-unit/1
status: active
supersedes: []
tier: L4
title: Day-0 MLAPO vector fusion, static-weight prefetch, and multi-stream mutator
triggers:
  error_signatures:
  - MLA prolog decode is launch-bound with separate RmsNorm, DynamicQuant, RoPE, and
    Cast kernels
  - vector fusion exceeds the AIV UB budget or silently drops an external live-out
  - prefetch regresses prefill because HBM is already saturated
  - multi-stream overlap of Vector and Cube is slower than one stream under shared
    HBM
  keywords:
  - MLAPO
  - mla_preprocess_only_decode
  - wd_qkv
  - wu_q
  - static weight prefetch
  - C_prefetch
  - AIV UB fusion
  - external live-out
  - Cube prologue epilogue absorption
  - multi-stream overlap
  - leave-one-out ablation
---
## Scope & Anchor

Decode-only after steady capture, `sol`, and `mindie://vllm-ascend/units/unit-vllm-ascend-hbm-memory-hidden-assassins-axioms@2`. Anchors `vllm-ascend@175ef4d`: `AscendMLAImpl.forward` (`attention/mla_v1.py:1717`) -> `DeviceOperator.mla_preprocess_only_decode` (`device/device_op.py:306`) -> `torch.ops._C_ascend.mla_preprocess` (`wd_qkv`/`wu_q`).

## Factors

- **A:** Fuse Add/RmsNorm/Quant/RoPE/Cast with per-core tile loops (`live UB <= C_UB`), preserving GM live-outs.
- **B:** Fold ND->NZ, cast, and static scale into Cube `mte1`/`mte2`/`fixpipe`.
- **C:** Stage static `wd_qkv`/`wu_q` via L2 (`mte2_l2`) or AIC L1 (`mte1`) within `P = min(W, C_free, service)` under foreground priority. Because the foreground Vector chain produces the Cube input, it cannot overlap that Cube op; only weight loads overlap. Without verified cross-kernel residency, emit `unsupported_prefetch_path` and keep the original graph.
- **D:** Bound two streams by `BW_hbm` and `T_overlap >= max(T_vector, T_cube)`.

## Gates

Verify `baseline`, `full`, `minus_A..minus_D`, and `final` via `ablation.verified_v2` (`unit-vllm-ascend-mlapo-prefetch-ablation-protocol`@3) and `pytest -q tests/ut/ops/test_mla.py`: no eager fallback, `abs<=2e-2`, `cos>=0.999`, `P50>=1.05x`, HBM headroom, and no sync/alloc/bucket churn.
