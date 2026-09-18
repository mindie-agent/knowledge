# Case: 310P MRv2 适配 MTP（Qwen3.5）— CLOSED unmerged / DIRTY

- **日期**: 2026-09-14
- **状态**: CLOSED unmerged / merge-conflicts / reference-only
- **来源 PR**: https://github.com/vllm-project/vllm-ascend/pull/15816 （CLOSED；mergedAt null；CONFLICTING/DIRTY；labels: `module:tests`, `merge-conflicts`）
- **Pinned（本轮委托证据）**: base `45fc6040778a720affb451f80bcaf772eeea7818` · head `22e086b0d06fda8776d2e6b4a8ba091dafabd903`

## 修订说明 / Revision notes（2026-09-14，Asia/Shanghai）

- 新建 case：CLOSED @ 2026-09-14T01:03:02Z；**DIRTY** — 最终合入形态未知。
- Body 偏薄：RFC #15577；tested vs vLLM `e6bfe03ad73a3330cb427885aa90d97a12e1c704`。设计细节主要来自 **files/diff 信号**。
- 与 #15645 共享 host splitfuse / 107027 D2H 守卫 **模式**；**分案**，不合并。
- **310P ≠ 910 A2/A3**。

## Trigger — 何时想起本 case

在 **310P** 上为 **Qwen3.5** 启用 **MRv2** + **MTP**（含 eager 与 FULL_DECODE_ONLY graph），并讨论：

- `vllm_ascend/_310p/worker/v2/` 新栈（runner / sampler / rejection / spec_decode）；
- 与 #15645 重叠的 attention mask D2H 守卫；
- e2e 是否拒绝「经典 310P MTP 损坏签名」；
- CLOSED+CONFLICTING 下代码是否仍可参考。

## Preconditions / environment signals

- 仓库：`vllm-project/vllm-ascend`；base `main` @ `45fc6040778a720affb451f80bcaf772eeea7818`；head `mrv2_310p_mtp_fix` @ `22e086b0d06fda8776d2e6b4a8ba091dafabd903`。
- RFC 语境：https://github.com/vllm-project/vllm-ascend/pull/15577（编号引用；本轮未深读 RFC 正文）。
- 上游对照 CLAIM：vLLM @ `e6bfe03ad73a3330cb427885aa90d97a12e1c704`。
- 硬件：**310P**；MRv2 路径；勿与 910 A2/A3 混谈。

## Observed failure pattern（若有）

- PR body **未**详述现场崩溃日志；动机侧接 #15645 类 MTP+graph / 310P 适配（**推断，非 VERIFIED 症状**）。
- e2e 命名暗示需拒绝「classic 310P MTP corruption signatures」（测例意图；**非**本轮复现）。

## Fix direction（仅限本 PR，附条件）

1. 新增 MRv2 310P 栈：`vllm_ascend/_310p/worker/v2/` — `model_runner`, `model_state`, `sampler`, `rejection_sampler`, `spec_decode/{aclgraph,mtp_speculator}`, `spec_utils`。
2. Patches：`patch_idex_310.py`；`patch_v2/patch_spec_decode_310.py`。
3. 共享/扩展 #15645 风格：host splitfuse mask + 107027 D2H 守卫（`attention_mask` / `attention_v1` / `metadata_builder`）。
4. E2E：`test_spec_decode_mtp_mrv2_310p.py` — MRv2 MTP eager + FULL_DECODE_ONLY graph K=1。
5. UT：config 仅接受 MTP（拒非 MTP）；NZ KV copy；`greedy_rejection_sample_cpu`。

**条件**: head `22e086b0d06fda8776d2e6b4a8ba091dafabd903`；**CLOSED + CONFLICTING/DIRTY**；最终形态 **UNVERIFIED**。

## Do-not-overgeneralize

- 禁止把 DIRTY 关闭 PR 当作已合入 MRv2 MTP。
- 禁止与 #15645（MRv1）混为一谈或合并 case。
- 禁止 310P ↔ 910 等价。
- Body 薄 → 「适配 Qwen3.5 MTP」多为 **CLAIM**；深度设计散文缺失。

## Evidence links

- PR: https://github.com/vllm-project/vllm-ascend/pull/15816
- Compare: https://github.com/vllm-project/vllm-ascend/compare/45fc6040778a720affb451f80bcaf772eeea7818...22e086b0d06fda8776d2e6b4a8ba091dafabd903
- Related MRv1 D2H fix: https://github.com/vllm-project/vllm-ascend/pull/15645
- RFC ref: https://github.com/vllm-project/vllm-ascend/pull/15577

## Retrieval queries

1. `310P MRv2 MTP Qwen3.5 vllm_ascend _310p worker v2`
2. `test_spec_decode_mtp_mrv2_310p FULL_DECODE_ONLY K=1`
3. `PR 15816 CLOSED CONFLICTING mrv2_310p_mtp_fix`
4. `patch_spec_decode_310 host splitfuse 107027 overlap 15645`
5. `greedy_rejection_sample_cpu NZ KV copy MTP-only config 310P`
