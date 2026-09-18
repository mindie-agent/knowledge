# Case: GLM-5.3-Flash — CANN KeyPool + PoolKeyIndexer 集成

- **日期**: 2026-09-18（修订）；初建 2026-09-17
- **状态**: OPEN not-draft / **CONFLICTING·DIRTY** / reference-only
- **来源 PR**: https://github.com/vllm-project/vllm-ascend/pull/16730 （author `lijiahang226`；labels: `module:tests`, `merge-conflicts`）
- **Pinned（本轮 gh 2026-09-18）**: base `e1d1490314eeb5baec2e77b27a9610e5358aa153` · head `b5cc342f6dffb3dfd8141eb7ca3c8dbde91bffde`

## 修订说明 / Revision notes（2026-09-18，Asia/Shanghai）

- **重 pin**：base 不变 `e1d1490314ee…`；head `0c7c2dc5…` → `b5cc342f6dffb3dfd8141eb7ca3c8dbde91bffde`。
- 增量 commits（VERIFIED）：`test(glm5next): assert optional metadata before use`；`refactor: keep PoolKeyIndexer ACLNN dispatch local`。
- 状态变化：上轮 MERGEABLE/BLOCKED → 本轮 **CONFLICTING/DIRTY** + `merge-conflicts`。
- 规模约 +27655/−141，**71 files**。
- 保留旧对比：https://github.com/vllm-project/vllm-ascend/compare/e1d1490314eeb5baec2e77b27a9610e5358aa153...0c7c2dc59bf707a4464526e4be3bf74fb1ccd148
- 新对比：https://github.com/vllm-project/vllm-ascend/compare/e1d1490314eeb5baec2e77b27a9610e5358aa153...b5cc342f6dffb3dfd8141eb7ca3c8dbde91bffde
- 仍无 device-executing tests（PR CLAIM）→ 运行时正确性 **UNVERIFIED**；评分契约差异仍 **CLAIM**。


## 修订说明 / Revision notes（2026-09-17，Asia/Shanghai）

- 新建 case；Refs roadmap #15665（CLAIM 关系）。
- 体量以 vendored ops 为主：+27586/−142，**72 files**；A2/A3/A5 KeyPool 源来自 ops-transformer `a539608…`（PR CLAIM + license retained）。
- Commits（VERIFIED）：`feat(glm5next): integrate CANN KeyPool and PoolKeyIndexer`；`fix(glm5next): narrow optional CANN metadata types`。
- 测试：CPU UT mock native interfaces；**无** device-executing tests / bench（PR CLAIM）→ 运行时正确性 **UNVERIFIED**。
- 评分契约差异：CANN `sum_h(weight_h * ReLU(q…`（body CLAIM）≠ 旧 Triton pooling 合同。

## Trigger — 何时想起本 case

讨论 **GLM5-Next / GLM-5.3-Flash** 稀疏索引、KeyPool / PoolKeyIndexer、或 Triton pooling → CANN 替换时。

## Preconditions / environment signals

- base `main` @ `e1d14903…`；head @ `b5cc342f…`。
- 文件（VERIFIED sample）：`csrc/attention/key_pool/**`（arch22/arch35 tiling + kernels）、ACLNN adapters、GLM5-Next model path 适配。
- 用户面（CLAIM）：自动启用 CANN ops；无新配置开关。

## Observed failure pattern（若有）

- Feature 集成；正文提及 tail R/W races、empty-pool bounds、padded request boundaries 修复（CLAIM）。

## Fix direction（仅限本 PR，附条件）

1. Vendor KeyPool / PoolKeyIndexer（A2/A3/A5）+ build/ACLNN/Meta。
2. 适配 request metadata、pooled cache writes、ring-tail layout、causal tail packing。
3. 修 normalized tail 读写竞态、empty-pool 输出边界、padded 请求边界（CLAIM）。
4. CPU UT mock 原生接口。

**条件**: head `b5cc342f6dffb3dfd8141eb7ca3c8dbde91bffde`；OPEN DIRTY；无 device tests。

## Do-not-overgeneralize

- Vendored ops-transformer SHA ≠ 上游持续同步保证。
- 无 device tests → 禁止宣称已验证 NPU 正确性。
- 评分契约变更 → 禁止假设与旧 Triton 数值完全等价。

## Evidence links

- PR: https://github.com/vllm-project/vllm-ascend/pull/16730
- Compare: https://github.com/vllm-project/vllm-ascend/compare/e1d1490314eeb5baec2e77b27a9610e5358aa153...0c7c2dc59bf707a4464526e4be3bf74fb1ccd148
- Roadmap issue: https://github.com/vllm-project/vllm-ascend/issues/15665

## Retrieval queries

1. GLM-5.3-Flash KeyPool PoolKeyIndexer CANN GLM5-Next
2. ops-transformer key_pool arch22 arch35 Ascend vendor
3. PR 16730 replace Triton pooling sparse index selection
4. empty-pool bounds ring-tail causal packing GLM5
5. vllm-ascend KeyPoolIndexer integration blocked 2026-09-17
