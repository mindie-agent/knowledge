# Topic: CANN native LI + MoE gating（替换 in-tree custom）

## 修订（2026-09-18）

- head → `d73c7b3d…`（minimal torch_npu CPU builds）；仍 BLOCKED；正确性 **CLAIM**。


- **日期**: 2026-09-18（修订）；初建 2026-09-17
- **关联 case**: [`cases/pr-16731-cann-native-li-moe-ops.case.md`](../cases/pr-16731-cann-native-li-moe-ops.case.md)
- **PR**: https://github.com/vllm-project/vllm-ascend/pull/16731
- **Pins**: base `e1d14903…` · head `d73c7b3d…` · OPEN BLOCKED

## 要点

- **VERIFIED**：大 diff 删除 custom LI quant / MoE gating 实现与 bindings；规模 +58/−17075 / 74 files。
- **CLAIM**：改走 `torch_npu.npu_quant_lightning_indexer` 与 `npu_moe_gating_top_k`；保留 hash 与 V2 LI metadata。
- **UNVERIFIED**：全环境正确性与性能；本轮未装模型/未跑 NPU。

## 勿过度推广

- native 替换 ≠ 所有 Ascend 平台等价；blocked ≠ 可合入。

## Retrieval queries

1. CANN native lightning indexer MoE gating top_k Ascend
2. remove custom lightning_indexer_quant moe_gating_top_k csrc
3. PR 16731 torch_npu operator migration
