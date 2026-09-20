# Case: MRV2 PCP+DP — vLLM 0.28.0 dispatch token-count adaptation

- **日期**: 2026-09-20（MERGED）；初建 2026-09-19
- **状态**: **MERGED** / reference-only；mergeCommit `8f2e3fed73328148ea603ccfe5764542fa7cbbd2`
- **来源 PR**: https://github.com/vllm-project/vllm-ascend/pull/16853 （author `wzx0726`；labels: `module:tests`, `ready-precise`）
- **Pinned（本轮 gh 2026-09-20）**: base `628fac6d85c27f5f24a589db300ae9add24151ff` · head `fb0554b7ff61747f20badb5acb17896c7c7ca313` · **mergeCommit** `8f2e3fed73328148ea603ccfe5764542fa7cbbd2`

## 修订说明 / Revision notes（2026-09-20，Asia/Shanghai）

- **状态变化**：OPEN BLOCKED → **MERGED**（`mergedAt` 2026-09-19T03:32:21Z VERIFIED GraphQL）。
- head SHA **未变** `fb0554b7ff61747f20badb5acb17896c7c7ca313`；mergeCommit `8f2e3fed73328148ea603ccfe5764542fa7cbbd2`。
- 规模仍 +337/−9，**7 files**（VERIFIED `gh pr view`）。
- **合并 ≠ 本轮复验**：未跑 NPU；PCP+DP token-count 修复仍为参考笔记。
- 对比（合入前）：https://github.com/vllm-project/vllm-ascend/compare/628fac6d85c27f5f24a589db300ae9add24151ff...fb0554b7ff61747f20badb5acb17896c7c7ca313

## 修订说明 / Revision notes（2026-09-19，Asia/Shanghai）

- 新建 case；规模 +337/−9，**7 files**；MERGEABLE **BLOCKED**。
- 文件面（VERIFIED）：`worker/v2/model_runner.py`、`worker/v2/pcp_manager.py`、`patch/platform/patch_use_v2_model_runner.py`、`patch/__init__.py` + UT。
- 根因 CLAIM：vLLM 0.28.0 上 Ascend MRV2 PCP+DP 在 parallel config 校验被拒；仅放行配置不够——dispatch 在 PCP 分区前仍同步 **global** token count，导致如 44-token prefill + PCP2 时 `DPMetadata.make` 见 44 但 local batch 22。
- 修复 CLAIM：配置 workaround 限定 vLLM 0.28.0 + Ascend MRV2 显式启用 + DP>1 + PCP>1 + DCP=1；按 rank-segment 规则在 dispatch 前算 PCP execution count（含 replicated decode / uneven prefill）；经 call-scoped context 只传计算后的 count。
- 基线 CLAIM：upstream main `628fac6d8…`；线性提交保留 #16832 对 default MRV2 whitelist 的 revert；图 replay revert 已在 #16726 upstream，本 PR 不含。
- 校验 CLAIM：ruff/AST/`git diff --check`；先验本地 121 UT；本轮未跑 NPU → **UNVERIFIED** device。

## Trigger — 何时想起本 case

讨论 **MRV2 + PCP + DP** 在 vLLM 0.28.0 上配置校验失败，或 dispatch 全局/本地 token 计数不一致时。

## Preconditions / environment signals

- base `main` @ `628fac6d…`；head @ `fb0554b7…`。
- 门控 CLAIM：仅 vLLM 0.28.0 × MRV2 explicit × DP>1 × PCP>1 × DCP=1。
- MRV2 仍需环境显式启用（#16832 revert whitelist CLAIM）。

## Observed failure pattern（若有）

- Config rejection and/or DPMetadata token-count mismatch under PCP partitioning（CLAIM）。

## Fix direction（仅限本 PR，附条件）

1. Scoped parallel-config workaround + restore real PCP size on exit。
2. Pre-dispatch PCP execution count via existing rank-segment rules。
3. Call-scoped context into original dispatch；保留 global request state / DP sync checks。

**条件**: head `fb0554b7ff61747f20badb5acb17896c7c7ca313`；**MERGED** `8f2e3fed…`；device **UNVERIFIED**。

## Do-not-overgeneralize

- 0.28.0 限定 ≠ 其他 vLLM 版本自动适用。
- 勿与 #16848「默认 MRV2」混 pin（本 PR 明确保留显式启用）。
- DCP≠1 路径不在本 workaround 范围（CLAIM）。

## Evidence links

- PR: https://github.com/vllm-project/vllm-ascend/pull/16853
- Compare: https://github.com/vllm-project/vllm-ascend/compare/628fac6d85c27f5f24a589db300ae9add24151ff...fb0554b7ff61747f20badb5acb17896c7c7ca313

## Retrieval queries

1. MRV2 PCP DP vLLM 0.28.0 dispatch token count Ascend
2. PR 16853 pcp_manager model_runner_v2 DPMetadata
3. Ascend PCP2 prefill local batch global sync mismatch
4. patch_use_v2_model_runner PCP DP workaround
5. DCP=1 only PCP DP MRV2 0.28
