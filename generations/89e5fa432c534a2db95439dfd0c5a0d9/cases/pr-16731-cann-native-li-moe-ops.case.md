# Case: Replace custom Lightning Indexer / MoE gating with CANN native ops

- **日期**: 2026-09-18（修订）；初建 2026-09-17
- **状态**: OPEN not-draft / MERGEABLE / **BLOCKED** / reference-only
- **来源 PR**: https://github.com/vllm-project/vllm-ascend/pull/16731 （author `yanhao5023`；labels: `documentation`, `module:tests`）
- **Pinned（本轮 gh 2026-09-18）**: base `e1d1490314eeb5baec2e77b27a9610e5358aa153` · head `d73c7b3d732d2f383755349429dab62e51e7db0f`

## 修订说明 / Revision notes（2026-09-18，Asia/Shanghai）

- **重 pin**：base 不变 `e1d1490314ee…`；head `0a167331…` → `d73c7b3d732d2f383755349429dab62e51e7db0f`。
- 增量 commit（VERIFIED）：`test: support minimal torch_npu CPU builds`（`d73c7b3d…`）。
- 规模现约 +59/−17075，**74 files**；仍 MERGEABLE **BLOCKED**。
- 保留旧对比：https://github.com/vllm-project/vllm-ascend/compare/e1d1490314eeb5baec2e77b27a9610e5358aa153...d73c7b3d732d2f383755349429dab62e51e7db0f
- 新对比：https://github.com/vllm-project/vllm-ascend/compare/e1d1490314eeb5baec2e77b27a9610e5358aa153...d73c7b3d732d2f383755349429dab62e51e7db0f
- 作者自测 CLAIM：CANN 9.2.0-beta.2 / torch 2.10 / torch_npu 2.10.0.post4 / 910B4-1；本轮未复跑 → 正确性仍 **CLAIM**。


## 修订说明 / Revision notes（2026-09-17，Asia/Shanghai）

- 新建 case；大规模删除 in-tree custom kernels（+58/−17075，**74 files**）。
- 路由到 `torch_npu.npu_quant_lightning_indexer` 与 `torch_npu.npu_moe_gating_top_k`（PR body CLAIM）。
- 保留 `moe_gating_top_k_hash`、`npu_quant_lightning_indexer_v2` 与 V2 metadata path（CLAIM）。
- 正确性：作者称对照既有 reference coverage → **CLAIM**；本轮未复跑 NPU。
- DCO / PR create **SUCCESS**（VERIFIED 摘要）；pre-commit 结论本轮未完整 rollup。

## Trigger — 何时想起本 case

讨论 Ascend **Lightning Indexer 量化** / **MoE gating top-k** 是否仍走自研 `csrc`，或迁移到 CANN / `torch_npu` 原生算子时。

## Preconditions / environment signals

- base `main` @ `e1d14903…`；head @ `d73c7b3d…`。
- 文件面（VERIFIED sample）：删除 `csrc/attention/lightning_indexer_quant/**` 大量 host/kernel；同步 bindings/schemas/build；更新 operator/device tests（PR CLAIM）。
- 用户面（CLAIM）：受影响路径改用 CANN via `torch_npu`。

## Observed failure pattern（若有）

- Refactor / deps：动机为以原生算子替换 custom；非单一 crash。

## Fix direction（仅限本 PR，附条件）

1. Quant LI → `npu_quant_lightning_indexer`（`pre_tokens`/`next_tokens` = `INT64_MAX` CLAIM）。
2. MoE gating → `npu_moe_gating_top_k`，保留 bias/group/renorm/output/routed-scale 参数（CLAIM）。
3. 删除 superseded custom kernels / Torch bindings / schemas / build entries。
4. 保留 hash 变体与 V2 LI metadata 路径。

**条件**: head `d73c7b3d732d2f383755349429dab62e51e7db0f`；OPEN blocked；正确性 **CLAIM**。

## Do-not-overgeneralize

- 删除 custom ≠ 全卡型 / 全 CANN 版本已验证。
- 禁止把 reference coverage CLAIM 当本轮 VERIFIED。
- V2 LI / hash 路径仍在 → 勿说「全部 LI/MoE 已 native」。

## Evidence links

- PR: https://github.com/vllm-project/vllm-ascend/pull/16731
- Compare: https://github.com/vllm-project/vllm-ascend/compare/e1d1490314eeb5baec2e77b27a9610e5358aa153...0a16733161a65da4e30a5c7f7030500b0bfc7cac

## Retrieval queries

1. npu_quant_lightning_indexer npu_moe_gating_top_k CANN native replace custom
2. lightning_indexer_quant csrc remove torch_npu Ascend MoE gating
3. PR 16731 LI MoE operators refactor blocked
4. retain moe_gating_top_k_hash npu_quant_lightning_indexer_v2 metadata
5. vllm-ascend ops CANN native vs in-tree kernels 2026-09-17
