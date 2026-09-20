# Maintenance run summary — 2026-09-13

- **Timezone context**: Asia/Shanghai（修订说明日期）；时间戳 UTC 见 meta
- **Wall budget (ceiling, not measured elapsed)**: ≤ ~18–20 minutes allotted for the run；本轮未改上游仓库、未发评论、未装本地模型、未跑 NPU 实验；文中分钟数为预算上限，非实测耗时

## Sources covered

| Source | Result |
|---|---|
| `gh pr view/checks/diff` `vllm-project/vllm-ascend#16157` | OK；base `fb2820b9b6598f888d7dc56260c03d3352cc08e9`，head `debfa9bd48fefcc90added07b28540daee8b5435` |
| Head blobs：`dspark_proposer.py` / `llm_base_proposer.py` / `model_runner_v1.py` (+ UT) | OK via `gh api` contents |
| Raw diff vs prior local run-package attachment / `gh pr diff` | **unchanged**（未覆盖本地 raw attachment；`_raw/` **不是**本 feed 内容）。Compare: https://github.com/vllm-project/vllm-ascend/compare/fb2820b9b6598f888d7dc56260c03d3352cc08e9...debfa9bd48fefcc90added07b28540daee8b5435 |
| `vllm-ascend-workspace/vaws-knowledge` shallow clone | OK @ `e16d87287f7db51ca96efc945a9514e4146131d6` |

## Artifacts this run

| Path | Role | Cap count |
|---|---|---|
| `topics/pr-16157-acl-graph-padding-precision.md` | revised | 1 |
| `cases/pr-16157-acl-graph-padding-precision.case.md` | revised | 1 |
| `topics/vaws-corpus-nav-va-npu-infra.md` | new | 1 |
| `maintenance/2026-09-13-run-summary.md` | new (this file) | 1 |
| `meta/sources-2026-09-13.json`（仅原始本地 run 包；**非 feed**） | new (local ops) | 1 |
| `meta/run-summary-2026-09-13.md`（仅原始本地 run 包；**非 feed**） | new (local ops) | 1 |
| `README.md`（本地索引；**非 feed**） | revised index | 1 |
| **Total new/updated materials** | | **7 / ≤12** |

本地 ZIP / `_raw` / `meta` / `exports` 仅存在于原始本地 run 包 attachment，**不属于本 reference feed**。公开证据请用 PR/compare/blob 链接：https://github.com/vllm-project/vllm-ascend/pull/16157

## Conflicts / unverified（摘要）

- PR：**merge-conflicts**；相对新 base tip 可能无法干净应用。
- **K3 gating**：VERIFIED 仅专用分支；CLAIM「非 K3 皆 eager」对 DeepSeek V4 **未被** `use_cuda_graph=False` 证明 → UNVERIFIED 生产行为。
- Corpus：无 ACL Graph / HCCL / vllm-ascend runtime 笔记；与 PR #16157 **no association**。
- Peak 数值未对本机 CANN 复核。

## Links

- Topic PR: [`topics/pr-16157-acl-graph-padding-precision.md`](../topics/pr-16157-acl-graph-padding-precision.md)
- Case PR: [`cases/pr-16157-acl-graph-padding-precision.case.md`](../cases/pr-16157-acl-graph-padding-precision.case.md)
- Corpus nav: [`topics/vaws-corpus-nav-va-npu-infra.md`](../topics/vaws-corpus-nav-va-npu-infra.md)
- PR: https://github.com/vllm-project/vllm-ascend/pull/16157
- Compare: https://github.com/vllm-project/vllm-ascend/compare/fb2820b9b6598f888d7dc56260c03d3352cc08e9...debfa9bd48fefcc90added07b28540daee8b5435
- Corpus tree: https://github.com/vllm-ascend-workspace/vaws-knowledge/tree/e16d87287f7db51ca96efc945a9514e4146131d6

## Retrieval queries

1. `va-knowledge 2026-09-13 PR 16157 re-verify gh`
2. `fb2820b9 debfa9bd _is_k3_dspark use_cuda_graph`
3. `vaws-knowledge e16d8728 Ascend910B4 corpus nav`
4. `va-knowledge maintenance run-summary DCO CONFLICTING`
5. `ACL Graph padding precision notes revision 2026-09-13`
