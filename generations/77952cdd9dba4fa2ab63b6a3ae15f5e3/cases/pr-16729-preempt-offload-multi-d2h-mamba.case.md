# Case: Preempt offload — 防 MRV2 多次 D2H + mamba align resume index

- **日期**: 2026-09-17
- **状态**: OPEN not-draft / MERGEABLE / **BLOCKED** / reference-only
- **来源 PR**: https://github.com/vllm-project/vllm-ascend/pull/16729 （author `nwpu-zxr`；labels: `module:tests`, `ready-precise`）
- **Pinned（本轮 gh 2026-09-17）**: base `e1d1490314eeb5baec2e77b27a9610e5358aa153` · head `f7d285a7bf0fbb42e80598de2e564129a50803f3`

## 修订说明 / Revision notes（2026-09-17，Asia/Shanghai）

- 新建 case；小 diff +102/−4，**3 files**。
- 动机（PR body CLAIM）：MRV2 上 `handle_preemptions()` 被调用两次 → 需防止多次 D2H；并修 mamba align mode resume index（Qwen3.5 preempt offload 精度）。
- 测试：By CI（CLAIM）；本轮未复跑。
- vLLM pin CLAIM：`84030bbe3d74d99bad477a3d2e37a973ccd8865c`。

## Trigger — 何时想起本 case

讨论 **P/D KVOffload preempt**、MRV2 重复 preempt 处理、或 Qwen3.5 **mamba** align resume 精度问题时。

## Preconditions / environment signals

- base `main` @ `e1d14903…`；head @ `f7d285a7…`。
- 文件（VERIFIED）：`kv_offload/preempt_offload/manager.py`、`worker.py`、UT `tests/ut/kv_offload/test_preempt_offload_connector.py`。

## Observed failure pattern（若有）

- CLAIM：多次 D2H（重复 `handle_preemptions`）；mamba align resume index 导致精度问题。

## Fix direction（仅限本 PR，附条件）

1. 防止 MRV2 路径上 preempt offload 多次 D2H。
2. 修正 mamba align mode resume index。

**条件**: head `f7d285a7bf0fbb42e80598de2e564129a50803f3`；OPEN blocked；精度修复 **CLAIM**。

## Do-not-overgeneralize

- 禁止外推到非 preempt-offload / 非 MRV2。
- CI pass CLAIM ≠ 全拓扑验证。

## Evidence links

- PR: https://github.com/vllm-project/vllm-ascend/pull/16729
- Compare: https://github.com/vllm-project/vllm-ascend/compare/e1d1490314eeb5baec2e77b27a9610e5358aa153...f7d285a7bf0fbb42e80598de2e564129a50803f3

## Retrieval queries

1. preempt offload multi D2H MRV2 handle_preemptions Ascend
2. mamba align mode resume index Qwen3.5 KVOffload
3. PR 16729 kv_offload preempt_offload manager worker
4. P/D KVOffload accuracy preempt Ascend blocked
5. vllm-ascend preempt offloading 2026-09-17
