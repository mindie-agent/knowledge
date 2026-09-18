# Maintenance run summary — 2026-09-17 daily PR incremental

- **Timezone context**: Asia/Shanghai（修订说明日期）；wall clock UTC may differ
- **Routine**: 「每日 PR 增量检查」；**budget ceiling ≤20 minutes**（预算上限，非实测耗时）
- **Constraints this run**: public `gh` only；未改上游；未装本地模型；未触 `_raw/`；**未删除**旧笔记；**未 recreate** 已有 case；package pin `cc1a1d76…`；verified curation-export → personal feed push if changed

## Scan window

- `vllm-ascend`: `gh pr list` recent（updated≥2026-09-16 / default list ≤8）+ tracked re-check
- `vllm`: `updated:>=2026-09-15` ≤8
- deferred 快检：#16236、#16417、#16628
- 初次 `updated:>=2026-09-15` + `baseRefOid` 字段失败；改用可用字段并重试 list（记录 gap，未丢知识）

## Disposition

| Source | Result |
|---|---|
| `vllm-ascend#15832` | **skip-unchanged** CLOSED；pins base `26f1363f…` head `b9227148…` |
| `vllm-ascend#14409` | **revised** base `0b345a67…` head `cede05131…`；仍 DIRTY；cost-model + small-batch bypass |
| `vllm-ascend#16157` | **skip-unchanged** base `fb2820b9…` head `debfa9bd…` |
| `vllm-ascend#16542` | **revised** base `d9ce0aa6…` head `b8df632c…`；comment trim + DraftBuilder stub |
| `vllm-ascend#16468` | **revised** base `10b4fb2a…` head `48c8a870…`；draft DIRTY；+72.6k/350 files |
| `vllm-ascend#16555` | **revised** base `10b4fb2a…` head `f9f35266…`；rebase pin |
| `vllm-ascend#16634` | **skip-unchanged** head `3d596b8f…` |
| `vllm-ascend#16629` | **revised** head `cefa37ab…`；formatter/test fix |
| `vllm-ascend#16636` | **skip-unchanged** head `1889ebee…` |
| `vllm-ascend#16731` | **new** case+topic（CANN native LI/MoE ops） |
| `vllm-ascend#16730` | **new** case+topic（GLM-5.3 KeyPool/PoolKeyIndexer） |
| `vllm-ascend#16729` | **new** case+topic（preempt multi-D2H + mamba resume） |
| `vllm-ascend#16728/#16727/#16725` | **skipped-low / deferred**（预算；量化/PA revert/sleep 有信号但未写） |
| `vllm-ascend#16722/#16721/#16724` | **skipped-low**（doc/CI） |
| `vllm-ascend#16726` | **skipped-low**（MERGED revert ACL Graph host-side；记 scan） |
| `vllm-ascend#16628` | **deferred**（DSpark top-k draft DIRTY） |
| `vllm-ascend#16236` | **deferred** |
| `vllm-ascend#16417` | **deferred**（Eagle3 E2E tests） |
| `vllm` top-8 | **skipped-low**（frontend/rust、nvidia DSpark FlashInfer、ROCm CI、DeepGEMM CUDA 等） |
| #15645 / #15816 | kept CLOSED notes；未 recreate |

## Counts

- vllm-ascend scanned（list+tracked）：8 list + tracked re-check
- vllm scanned：8；written：0
- revised：5；new：3；skip-unchanged：4；deferred：3+；skipped-low：若干

## Artifacts this run

| Path | Role |
|---|---|
| `cases/pr-14409-…` / `topics/hardware-aware-…` | revised |
| `cases/pr-16542-…` / `topics/pr-16542-…` | revised |
| `cases/pr-16468-…` / `topics/pr-16468-…` | revised |
| `cases/pr-16555-…` / `topics/pr-16555-…` | revised |
| `cases/pr-16629-…` / `topics/pr-16629-…` | revised |
| `cases/pr-16731-…` / `topics/pr-16731-…` | new |
| `cases/pr-16730-…` / `topics/pr-16730-…` | new |
| `cases/pr-16729-…` / `topics/pr-16729-…` | new |
| `README.md` | index updated |
| `maintenance/2026-09-17-daily-pr-incremental.md` | this file |
| `meta/sources-2026-09-17.json` | local ops pins（**非 feed**） |

本地 `meta/` / `exports/` / `_raw/` **不属于** reference feed。

## Conflicts / unverified（摘要）

- #14409 仍 DIRTY；NPU 增益 UNVERIFIED。
- #16468 draft+DIRTY；验收 CLAIM；规模继续膨胀。
- #16542/#16629/#16555 blocked；perf/bench CLAIM。
- #16731 native 正确性 CLAIM；#16730 无 device tests → UNVERIFIED。
- #16729 精度修复 CLAIM。
- #16157/#16634/#16636/#15832 无变化。
- 初次 GraphQL list 失败一次（已重试成功）。

## Retrieval queries

1. va-knowledge 2026-09-17 daily PR incremental 14409 16542 16468 16555 16629
2. new 16731 CANN LI MoE 16730 KeyPool GLM53 16729 preempt D2H
3. skip-unchanged 15832 16157 16634 16636 deferred 16236 16417 16628
4. pin cede05131 b8df632c 48c8a870 f9f35266 cefa37ab 0a167331 0c7c2dc5 f7d285a7
5. maintenance budget ceiling 20 min Asia/Shanghai 2026-09-17
