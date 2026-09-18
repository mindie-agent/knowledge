# Case: A5 MegaMoE W4A8 fused backend（FUSED_MC2）

- **日期**: 2026-09-16
- **状态**: OPEN not-draft / MERGEABLE / mergeStateStatus **CLEAN** / reference-only
- **来源 PR**: https://github.com/vllm-project/vllm-ascend/pull/16634 （author `wt0671`；labels: `documentation`, `module:ops`, `module:core`, `module:quantization`）
- **Pinned（本轮 gh）**: base `11ee45653b199a097805b87011824a81ffa51b95` · head `3d596b8fcf99b4591ea94f4e558f7caef809c137`

## 修订说明 / Revision notes（2026-09-16，Asia/Shanghai）

- 新建 case；clean rebuild of #14449 on `rfc/vllm_cann`（CLAIM 关系）；本 PR **排除** tests / diagnostic logs / W8A8·W4A4 行为变更。
- NPU runtime validation：**作者环境不可用** → 运行时正确性 **UNVERIFIED**；仅 py_compile / ruff / scope checks 为作者 CLAIM。
- DCO / PR create / RTD docs：**pass**（VERIFIED checks 摘要）；勿把 CLEAN 当生产验收。

## Trigger — 何时想起本 case

讨论 **A5 W4A8 MXFP MoE** 走逻辑 `FUSED_MC2`、MegaMoE backend、HCCL communicator / symmetric-buffer，或与 #14449 / `enable_fused_mc2` 文档约束时。

## Preconditions / environment signals

- base `main` @ `11ee4565…`；head `feat/a5-megamoe-w4a8` @ `3d596b8f…`。
- 文件（VERIFIED）：`ops/fused_moe/mega_moe.py`（新）、`fused_moe.py`、`moe_comm_method.py`、`prepare_finalize.py`、`quantization/methods/w4a8_mxfp4.py`、`distributed/parallel_state.py`、`ascend_forward_context.py`、`worker/model_runner_v1.py`、docs `additional_config.md`。
- vLLM pin（CLAIM）：`ee0da84ab9e04ac7610e28580af62c365e898389`。
- 用户面（CLAIM）：无 API 变更；经既有 fused-MC2 配置选择。

## Observed failure pattern（若有）

- Feature/perf backend；非 crash 修复。动机：A5 W4A8 走 MegaMoE + 专用 HCCL / symmetric buffer（PR CLAIM）。

## Fix direction（仅限本 PR，附条件）

1. 支持的 A5 W4A8 MXFP MoE 路由到逻辑 `FUSED_MC2`。
2. 新增 MegaMoE backend + dedicated HCCL communicator + symmetric-buffer lifecycle + prepare/finalize。
3. DP ranks 同步，使 active/dummy 选同一 graph 与 collective path（CLAIM）。
4. 保留 MC2 / all-gather / all-to-all fallbacks for unsupported configs。
5. 相对 #14449：无 tests  diff、无 W8A8/W4A4 实现 diff、无 diagnostic logging。

**条件**: head `3d596b8fcf99b4591ea94f4e558f7caef809c137`；OPEN CLEAN；NPU runtime **UNVERIFIED**。

## Do-not-overgeneralize

- CLEAN ≠ 已在 NPU 上验收。
- A5 W4A8 ≠ W8A8/W4A4；禁止外推量化格式。
- 禁止把 #14449 旧行为与本 clean rebuild 视为同一 tip。
- 310P runner 文件触及 ≠ 310P 已验证（**UNVERIFIED**）。

## Evidence links

- PR: https://github.com/vllm-project/vllm-ascend/pull/16634
- Compare: https://github.com/vllm-project/vllm-ascend/compare/11ee45653b199a097805b87011824a81ffa51b95...3d596b8fcf99b4591ea94f4e558f7caef809c137
- Related prior: https://github.com/vllm-project/vllm-ascend/pull/14449
- Head blob: https://github.com/vllm-project/vllm-ascend/blob/3d596b8fcf99b4591ea94f4e558f7caef809c137/vllm_ascend/ops/fused_moe/mega_moe.py

## Retrieval queries

1. A5 MegaMoE W4A8 FUSED_MC2 fused backend 16634
2. mega_moe HCCL symmetric-buffer prepare_finalize
3. w4a8_mxfp4 enable_fused_mc2 additional_config
4. PR 16634 vs 14449 clean rebuild exclude tests
5. DP dummy rank same graph collective path Ascend MoE
