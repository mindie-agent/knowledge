# [参考] vllm-ascend PR #15816：310P MRv2 MTP 适配（Qwen3.5）

- **日期**: 2026-09-14
- **状态**: CLOSED unmerged / CONFLICTING·DIRTY / reference-only（非正确性背书）
- **阅读方式**: 本轮委托 PR 元数据 + files/diff 信号（body 薄；未跑 NPU）

## Pinned（本轮委托证据）

| 字段 | 值 |
|---|---|
| PR URL | https://github.com/vllm-project/vllm-ascend/pull/15816 |
| 标题 | `[Feature][MRv2][310P] MRv2 adapting MTP on the 310P for Qwen3.5` |
| 状态 | **CLOSED** unmerged；closedAt `2026-09-14T01:03:02Z`；labels: `module:tests`, **`merge-conflicts`** |
| base / head 分支 | `main` / `mrv2_310p_mtp_fix` |
| **base SHA** | `45fc6040778a720affb451f80bcaf772eeea7818` |
| **head SHA** | `22e086b0d06fda8776d2e6b4a8ba091dafabd903` |
| mergeable | **CONFLICTING** / mergeStateStatus **DIRTY** |
| 作者 | `Thiagor2002` |
| 规模 | +1700 / −112，**21 files** |
| 对比 | https://github.com/vllm-project/vllm-ascend/compare/45fc6040778a720affb451f80bcaf772eeea7818...22e086b0d06fda8776d2e6b4a8ba091dafabd903 |

### CI / checks（委托）

| Check | 结论 |
|---|---|
| DCO | **pass** |
| PR create | **pass** |
| main | **pass** |

## 修订说明 / Revision notes（2026-09-14，Asia/Shanghai）

1. 新建 topic：MRv2 310P MTP 大 diff；关闭时仍 DIRTY。
2. Cross-link #15645（共享 D2H/mask）；**不**合并 case/topic。
3. Body CLAIM「adapts MTP for Qwen3.5 on 310P under MRv2 (RFC 15577)」；深度设计依赖 diff。

## Problem / intent（分层）

- **[CLAIM]** 在 310P 上为 Qwen3.5 适配 MRv2 MTP（RFC #15577）；对照测试 vLLM `e6bfe03ad73a3330cb427885aa90d97a12e1c704`。
- Body 无长篇设计/现场日志 → 细节以文件树与测例为准。

## Diff / structure analysis（head `22e086b0d06fda8776d2e6b4a8ba091dafabd903`）

### VERIFIED from files/diff signals

1. **新 MRv2 310P 栈** `vllm_ascend/_310p/worker/v2/`：`model_runner`, `model_state`, `sampler`, `rejection_sampler`, `spec_decode/{aclgraph,mtp_speculator}`, `spec_utils`。
2. **Patches**：`patch_idex_310.py`；`patch_v2/patch_spec_decode_310.py`。
3. **与 #15645 重叠**：host splitfuse mask / 107027 D2H 守卫出现在 `attention_mask` / `attention_v1` / `metadata_builder`。
4. **E2E** `test_spec_decode_mtp_mrv2_310p.py`：MRv2 MTP eager + FULL_DECODE_ONLY graph K=1；拒绝 classic 310P MTP corruption signatures（测例意图）。
5. **UT**：config 仅 MTP；NZ KV copy path；`greedy_rejection_sample_cpu`。

### CLAIM

1. 「适配 Qwen3.5 MTP on 310P under MRv2」— body 缺深设计散文。
2. RFC #15577 为权威设计来源 — 本轮未全文核验 RFC。

### UNVERIFIED

1. merge conflicts → **最终形态未知**；CLOSED dirty。
2. 真机 310P 正确性超出 smoke/测例命名。
3. 与日后 soft-land / 重开 PR 的关系。

## Applicability boundaries

- 适用参考：**310P + MRv2 + MTP** 代码结构与测例命名（pinned head）。
- 勿外推：910 A2/A3；已合入 main；MRv1-only（#15645）。

## Related

- Case: [`cases/pr-15816-310p-mrv2-mtp-qwen35.case.md`](../cases/pr-15816-310p-mrv2-mtp-qwen35.case.md)
- Shared D2H pattern: https://github.com/vllm-project/vllm-ascend/pull/15645 · [`topics/pr-15645-310p-mtp-aclgraph-capture-d2h.md`](pr-15645-310p-mtp-aclgraph-capture-d2h.md)
- RFC: https://github.com/vllm-project/vllm-ascend/pull/15577

## Retrieval queries

1. `vllm-ascend PR 15816 MRv2 310P MTP Qwen3.5`
2. `_310p/worker/v2 mtp_speculator aclgraph rejection_sampler`
3. `test_spec_decode_mtp_mrv2_310p corruption signatures`
4. `15816 CONFLICTING DIRTY closed 2026-09-14`
5. `15645 overlap host splitfuse mask 107027 MRv2`

## Explicit uncertainty markers

- `[CLOSED-UNMERGED-DIRTY]` CONFLICTING。
- `[THIN-BODY]` 设计细节靠 diff。
- `[CLAIM-QWEN35-ADAPT]` 产品叙述未独立证。
- `[UNVERIFIED-RUNTIME]` 无真机复验。
- `[[redacted:ticket-id]]` 共享模式，分案。
