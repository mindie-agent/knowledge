# Maintenance run summary — 2026-09-18 daily PR incremental

- **Timezone context**: Asia/Shanghai（修订说明日期）；wall clock UTC may differ
- **Routine**: 「每日 PR 增量检查」；**budget ceiling ≤20 minutes**（预算上限，非实测耗时）
- **Constraints this run**: public `gh` only；未改上游；未装本地模型；未触 `_raw/`；**未删除**旧笔记；**未 recreate** 已有 case；package pin `cc1a1d76…`；verified curation-export → personal feed push if changed

## Scan window

- `vllm-ascend`: `gh search prs --sort updated` ≤12 + tracked re-check + GraphQL base/head
- `vllm`: `gh search prs --sort updated` ≤8
- deferred 快检：#16236、#16628；#16417 已 MERGED（记 scan）
- `gh pr list` `baseRefOid` 仍不可用（沿用 GraphQL）；未丢知识

## Disposition

| Source | Result |
|---|---|
| `vllm-ascend#15832` | **skip-unchanged** CLOSED；pins base `26f1363f…` head `b9227148…` |
| `vllm-ascend#14409` | **skip-unchanged** base `0b345a67…` head `cede05131…`；仍 DIRTY |
| `vllm-ascend#16157` | **skip-unchanged** base `fb2820b9…` head `debfa9bd…` |
| `vllm-ascend#16542` | **revised** base `d9ce0aa6…` head `f4bb08f1…`；snake_case rename |
| `vllm-ascend#16468` | **revised** base `00b0b979…` head `d8a34b81…`；draft DIRTY；+78.5k/411 files |
| `vllm-ascend#16555` | **skip-unchanged** base `10b4fb2a…` head `f9f35266…` |
| `vllm-ascend#16634` | **skip-unchanged** head `3d596b8f…` CLEAN |
| `vllm-ascend#16629` | **skip-unchanged** head `cefa37ab…` BLOCKED |
| `vllm-ascend#16636` | **skip-unchanged** head `1889ebee…` |
| `vllm-ascend#16731` | **revised** head `d73c7b3d…`；minimal torch_npu CPU builds |
| `vllm-ascend#16730` | **revised** head `b5cc342f…`；现 CONFLICTING/DIRTY |
| `vllm-ascend#16729` | **skip-unchanged** head `f7d285a7…` |
| `vllm-ascend#16798` | **new** case+topic（UpdatableGraph / ACL Graph host-side） |
| `vllm-ascend#16656` | **new** case+topic（A5 SFA DCP padded LSE） |
| `vllm-ascend#16119` | **budget-deferred**（KV Pool remove delayed free；相关 AscendStore） |
| `vllm-ascend#16363/#15851/#16755/#16621/#16544` | **budget-deferred / skipped-low**（SpecDecode/GLM PD/DSV4 等有信号） |
| `vllm-ascend#16728` | **scan-only** MERGED（W8A8 scale recover；昨日 deferred） |
| `vllm-ascend#16727/#16725` | **deferred**（ND PA revert / Sleep Mode） |
| `vllm-ascend#16628` | **deferred**（DSpark top-k draft DIRTY） |
| `vllm-ascend#16236` | **deferred** |
| `vllm-ascend#16417` | **scan-only** MERGED Eagle3 E2E |
| `vllm` top-8 | **skipped-low**（Mistral/Frontend/XPU/CUDA SM100/metrics 等） |
| #15645 / #15816 | kept CLOSED notes；未 recreate |

## Counts

- vllm-ascend scanned（search+tracked）：12 search + tracked re-check
- vllm scanned：8；written：0
- revised：4；new：2；skip-unchanged：8；deferred/budget：若干

## Artifacts this run

| Path | Role |
|---|---|
| `cases/pr-16542-…` / `topics/pr-16542-…` | revised |
| `cases/pr-16468-…` / `topics/pr-16468-…` | revised |
| `cases/pr-16731-…` / `topics/pr-16731-…` | revised |
| `cases/pr-16730-…` / `topics/pr-16730-…` | revised |
| `cases/pr-16798-…` / `topics/pr-16798-…` | new |
| `cases/pr-16656-…` / `topics/pr-16656-…` | new |
| `README.md` | index updated |
| `maintenance/2026-09-18-daily-pr-incremental.md` | this file |
| `meta/sources-2026-09-18.json` | local ops pins（**非 feed**） |

本地 `meta/` / `exports/` / `_raw/` **不属于** reference feed。

## Conflicts / unverified（摘要）

- #16468 仍 draft+DIRTY；规模继续膨胀；验收 CLAIM。
- #16730 现 DIRTY；无 device tests → UNVERIFIED。
- #16542/#16731/#16798/#16656 blocked；perf/手测 CLAIM。
- #14409/#16555/#16629/#16729/#16157/#16634/#16636/#15832 无 head 变化（本轮 skip）。
- vllm 顶更与 Ascend NPU 低相关 → skipped-low。

## Retrieval queries

1. va-knowledge 2026-09-18 daily PR incremental 16542 16468 16731 16730
2. new 16798 UpdatableGraph ACL Graph 16656 A5 SFA DCP LSE
3. skip-unchanged 14409 16555 16629 16729 15832 16157 16634 16636
4. pin f4bb08f1 d8a34b81 d73c7b3d b5cc342f 3e33c9fd 60c6c32a
5. maintenance budget ceiling 20 min Asia/Shanghai 2026-09-18
