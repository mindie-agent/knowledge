# Case: DSpark empty valid context after discard（IndexCheck）

- **日期**: 2026-09-16
- **状态**: OPEN not-draft / MERGEABLE / **BLOCKED** / reference-only；多项 selected e2e **pass**（VERIFIED checks）
- **来源 PR**: https://github.com/vllm-project/vllm-ascend/pull/16636 （author `wangxiaoteng888`；labels: `module:tests`, `module:ops`, `ready-precise`）
- **Pinned（本轮 gh）**: base `c267db731d03e18042047ea1594e033673e46a6f` · head `1889ebeee16cb3855245b804b95f4b2a43eb8176`

## 修订说明 / Revision notes（2026-09-16，Asia/Shanghai）

- 新建 case；discarded async spec step 可致 `valid_ctx_end == ctx_start`，旧核读 `target_positions[valid_ctx_end - 1]` → underflow / cross-request → RoPE IndexCheck（PR CLAIM 根因叙述）。
- CI 摘要（VERIFIED）：DCO/ci-gate/pre-commit/cpu-ut/selected 310p+a2+a3 parts **pass**；mergeStateStatus 仍 **BLOCKED**（整体门禁未放行，原因未深挖 → **UNVERIFIED**）。
- 作者 CLAIM：Ascend NPU regression 12 passed / 9 deselected @ CI-pinned vLLM。

## Trigger — 何时想起本 case

异步 speculative decoding discard 后 DSpark 扩展核读到非法 position，或 NPU **IndexCheck** 出现在 RoPE/query positions 路径时。

## Preconditions / environment signals

- base `main` @ `c267db73…`；head `fix/dspark-empty-valid-context` @ `1889ebee…`。
- 文件（VERIFIED）：`ops/triton/spec_decode/utils.py`；UT `test_copy_and_expand_dflash_dspark.py`。
- 场景 CLAIM：5 draft + 1 target 全拒 → 空有效 context。

## Observed failure pattern（若有）

- **[CLAIM]** empty valid context → load `target_positions[valid_ctx_end - 1]` → first-row underflow 或跨 request 读 → RoPE IndexCheck。

## Fix direction（仅限本 PR，附条件）

1. 无有效 context 时用当前 request **first position** 作 safe anchor。
2. Query positions 从该 anchor 起算；rejection / slot-mapping 语义不变（CLAIM）。
3. 回归覆盖 first-row underflow、cross-request、两采样模式、int32/int64 positions。

**条件**: head `1889ebeee16cb3855245b804b95f4b2a43eb8176`；OPEN blocked（尽管多项 checks pass）。

## Do-not-overgeneralize

- Selected e2e pass ≠ 全矩阵绿 / 已合入。
- 禁止外推到非 DSpark / 非 discard 空 context 场景。
- 5+1 token 例子仅为说明 CLAIM。

## Evidence links

- PR: https://github.com/vllm-project/vllm-ascend/pull/16636
- Compare: https://github.com/vllm-project/vllm-ascend/compare/c267db731d03e18042047ea1594e033673e46a6f...1889ebeee16cb3855245b804b95f4b2a43eb8176
- Head blob: https://github.com/vllm-project/vllm-ascend/blob/1889ebeee16cb3855245b804b95f4b2a43eb8176/vllm_ascend/ops/triton/spec_decode/utils.py

## Retrieval queries

1. DSpark empty valid context discard IndexCheck 16636
2. valid_ctx_end ctx_start target_positions underflow
3. copy_and_expand dflash dspark safe anchor first position
4. async speculative decoding rejected all context entries
5. head 1889ebee ready-precise blocked e2e pass
