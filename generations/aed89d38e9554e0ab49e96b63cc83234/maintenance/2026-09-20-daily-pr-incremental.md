# Maintenance run summary — 2026-09-20 daily PR incremental

- **Timezone context**: Asia/Shanghai（修订说明日期）；wall clock UTC may differ
- **Routine**: 「每日 PR 增量检查」；**budget ceiling ≤20 minutes**（预算上限，非实测耗时）
- **Constraints this run**: public `gh` only；未改上游；未装本地模型；未触 `_raw/`；**未删除**旧笔记；**未 recreate** 已有 case；package pin `cc1a1d76…`（HEAD file verified）；verified curation-export → org feed `mindie-agent/knowledge` branch `knowledge/vllm-ascend` via locked publish

## Scan window

- `vllm-ascend`: starters ≤8 + tracked re-check（GraphQL base/head/mergeCommit）+ deferred revisit
- `vllm`: `gh search prs --sort updated` ≤8
- `gh pr list` `baseRefOid` 仍不可用（沿用 GraphQL）；未丢知识

## Disposition

| Source | Result |
|---|---|
| `vllm-ascend#14409` | **skip-unchanged** base `0b345a67…` head `cede05131…`；仍 DIRTY |
| `vllm-ascend#15645` / `#15816` / `#15832` | **skip-unchanged** CLOSED；pins 未变 |
| `vllm-ascend#16157` | **skip-unchanged** base `fb2820b9…` head `debfa9bd…` |
| `vllm-ascend#16555` | **skip-unchanged** head `f9f35266…` |
| `vllm-ascend#16629` | **skip-unchanged** head `cefa37ab…` |
| `vllm-ascend#16634` | **skip-unchanged** head `3d596b8f…` |
| `vllm-ascend#16636` | **skip-unchanged** head `1889ebee…` |
| `vllm-ascend#16729` | **skip-unchanged** head `f7d285a7…` |
| `vllm-ascend#16731` | **skip-unchanged** head `d73c7b3d…` |
| `vllm-ascend#16542` | **skip-unchanged** base `82b0b10a…` head `7036bb09…`；仍 DIRTY（updatedAt 有动） |
| `vllm-ascend#16656` | **skip-unchanged** **MERGED** mergeCommit `aff1b74b…` |
| `vllm-ascend#16915` | **skip-unchanged** base `c8addbb2…` head `383fbad9…`（mergeable 现 MERGEABLE） |
| `vllm-ascend#16853` | **revised** **MERGED** mergeCommit `8f2e3fed…`；head 未变 |
| `vllm-ascend#16798` | **revised** base `b255ab59…` head `9f4ac9d1…`；仍 BLOCKED |
| `vllm-ascend#16904` | **revised** base `b255ab59…` head `e4374826…`；仍 BLOCKED；skip-tests tip |
| `vllm-ascend#16468` | **revised** base `9ad52992…`（=#16545 merge） head `9410826a…`；draft DIRTY；+121k/603 |
| `vllm-ascend#16730` | **revised** base `b255ab59…` head `7ae1df8d…`；DIRTY→BLOCKED；external CANN KeyPool |
| `vllm-ascend#16545` | **new** case+topic（MiniMax MegaMoe **MERGED** `9ad52992…`；原 deferred） |
| `vllm-ascend#16012` | **new** case+topic（A2 BNSD long cached-prefill opt-in） |
| `vllm-ascend#16848` | **budget-deferred**（default MRV2；仍 OPEN MERGEABLE） |
| `vllm-ascend#16936` | **budget-deferred**（GLM-5.3-Flash MRV2 draft） |
| `vllm-ascend#15734` / `#16940` | **skipped-low**（CI / Test） |
| deferred 旧列表 `#16119/#16363/#15851/#16755/#16621/#16544/#16727/#16725/#16628/#16236` | **budget-deferred**（未深挖） |
| `vllm` top-8 | **skipped-low**（XPU/Frontend/KV-offload/ROCm-adjacent/examples/loader） |

## Counts

- vllm-ascend scanned（search starters + tracked）：8 starters + 19 tracked re-check
- vllm scanned：8；written：0
- revised：5；new：2；skip-unchanged：12；deferred/budget：若干；skipped-low：vllm 8 + CI/test

## Artifacts this run

| Path | Role |
|---|---|
| `cases/pr-16853-…` / `topics/pr-16853-…` | revised（MERGED） |
| `cases/pr-16798-…` / `topics/pr-16798-…` | revised |
| `cases/pr-16904-…` / `topics/pr-16904-…` | revised |
| `cases/pr-16468-…` / `topics/pr-16468-…` | revised |
| `cases/pr-16730-…` / `topics/pr-16730-…` | revised |
| `cases/pr-16545-…` / `topics/pr-16545-…` | **new**（MERGED） |
| `cases/pr-16012-…` / `topics/pr-16012-…` | **new** |
| `topics/aclgraph-hostside-padding-cluster.*` | light re-pin #16798 |
| `topics/a5-dcp-mla-sfa-cluster.*` | light re-pin #16468 |
| `README.md` | index + high notes |
| `maintenance/2026-09-20-daily-pr-incremental.md` | this file |
| `meta/sources-2026-09-20.json` | local ops pins（**非 feed**） |
| `meta/feed-publish-2026-09-20.md` | local publish note（**非 feed**） |

本地 `meta/` / `exports/` / `_raw/` **不属于** reference feed。

## Conflicts / unverified（摘要）

- #16468 仍 draft+DIRTY；规模继续膨胀；base 已含 MegaMoe merge；验收 CLAIM。
- #16798/#16904/#16730/#16012 blocked或OPEN；手测/性能 CLAIM；本轮未跑 NPU。
- #16853/#16545 已 MERGED；合并≠本轮复验。
- #16848/#16936 高信号但预算延后。
- vllm 顶更与 Ascend NPU 低相关 → skipped-low。

## Retrieval queries

1. va-knowledge 2026-09-20 daily PR incremental 16545 16012 16853
2. revised 16798 16904 16468 16730 MERGED 16853
3. deferred 16848 default MRV2 16936 GLM53 Flash MRV2
4. pin 9f4ac9d1 e4374826 9410826a 7ae1df8d 8f2e3fed 9ad52992 4f120cbe
5. maintenance budget ceiling 20 min Asia/Shanghai 2026-09-20
