# Maintenance run summary — 2026-09-19 daily PR incremental

- **Timezone context**: Asia/Shanghai（修订说明日期）；wall clock UTC may differ
- **Routine**: 「每日 PR 增量检查」；**budget ceiling ≤20 minutes**（预算上限，非实测耗时）
- **Constraints this run**: public `gh` only；未改上游；未装本地模型；未触 `_raw/`；**未删除**旧笔记；**未 recreate** 已有 case；package pin `cc1a1d76…`；verified curation-export → org feed `mindie-agent/knowledge` branch `knowledge/vllm-ascend` via locked publish

## Scan window

- `vllm-ascend`: starters ≤8 + tracked re-check（GraphQL base/head）+ nearby defer list
- `vllm`: `gh search prs --sort updated` ≤8
- `gh pr list` `baseRefOid` 仍不可用（沿用 GraphQL）；未丢知识

## Disposition

| Source | Result |
|---|---|
| `vllm-ascend#14409` | **skip-unchanged** base `0b345a67…` head `cede05131…`；仍 DIRTY |
| `vllm-ascend#15645` / `#15816` | **skip-unchanged** CLOSED；pins 未变 |
| `vllm-ascend#15832` | **skip-unchanged** CLOSED；pins 未变 |
| `vllm-ascend#16157` | **skip-unchanged** base `fb2820b9…` head `debfa9bd…` |
| `vllm-ascend#16555` | **skip-unchanged** head `f9f35266…` |
| `vllm-ascend#16629` | **skip-unchanged** head `cefa37ab…` |
| `vllm-ascend#16634` | **skip-unchanged** head `3d596b8f…` |
| `vllm-ascend#16636` | **skip-unchanged** head `1889ebee…` |
| `vllm-ascend#16729` | **skip-unchanged** head `f7d285a7…` |
| `vllm-ascend#16730` | **skip-unchanged** head `b5cc342f…` |
| `vllm-ascend#16731` | **skip-unchanged** head `d73c7b3d…` |
| `vllm-ascend#16468` | **revised** base `bdd53a2a…` head `0344beea…`；draft DIRTY；+87.7k/445 |
| `vllm-ascend#16542` | **revised** base `82b0b10a…` head `7036bb09…`；rebase → DIRTY |
| `vllm-ascend#16656` | **revised** **MERGED** mergeCommit `aff1b74b…`；head SHA 未变 |
| `vllm-ascend#16798` | **revised** head `4c29f963…`；gemma-mtp/fix |
| `vllm-ascend#16904` | **new** case+topic（MiniMax-M3 CANN Ops sparse attn） |
| `vllm-ascend#16915` | **new** case+topic（DSA-CP prefill fused gather） |
| `vllm-ascend#16853` | **new** case+topic（MRV2 PCP+DP vLLM 0.28） |
| `vllm-ascend#16545` | **budget-deferred**（MiniMax MegaMoe；高信号） |
| `vllm-ascend#16848` | **budget-deferred**（default MRV2 all configs） |
| `vllm-ascend#16394` / `#16718` / `#16393` / `#14544` | **skipped-low**（test/CI/Main2Main/noise） |
| `vllm-ascend#16836/#16816/#16677` 等 CI | **budget-deferred** |
| deferred 昨日列表 `#16119/#16363/#15851/#16755/#16621/#16544/#16727/#16725/#16628/#16236` | **budget-deferred**（未深挖） |
| `vllm` top-8 | **skipped-low**（[redacted:credential-high-entropy] 等） |

## Counts

- vllm-ascend scanned（search starters + tracked）：8+ starters + 16 tracked re-check
- vllm scanned：8；written：0
- revised：4；new：3；skip-unchanged：12；deferred/budget：若干；skipped-low：vllm 8 + CI/noise

## Artifacts this run

| Path | Role |
|---|---|
| `cases/pr-16468-…` / `topics/pr-16468-…` | revised |
| `cases/pr-16542-…` / `topics/pr-16542-…` | revised |
| `cases/pr-16656-…` / `topics/pr-16656-…` | revised（MERGED） |
| `cases/pr-16798-…` / `topics/pr-16798-…` | revised |
| `cases/pr-16904-…` / `topics/pr-16904-…` | new |
| `cases/pr-16915-…` / `topics/pr-16915-…` | new |
| `cases/pr-16853-…` / `topics/pr-16853-…` | new |
| `README.md` | index + high notes |
| `maintenance/2026-09-19-daily-pr-incremental.md` | this file |
| `meta/sources-2026-09-19.json` | local ops pins（**非 feed**） |
| `meta/feed-publish-2026-09-19.md` | local publish note（**非 feed**） |

本地 `meta/` / `exports/` / `_raw/` **不属于** reference feed。

## Conflicts / unverified（摘要）

- #16468 仍 draft+DIRTY；规模继续膨胀；+MLA C8 commit；验收 CLAIM。
- #16542 rebase 后 DIRTY；性能 CLAIM。
- #16798/#16904/#16915/#16853 blocked；手测/UT CLAIM；本轮未跑 NPU。
- #16656 已 MERGED；合并≠本轮复验。
- #16545/#16848 高信号但预算延后。
- vllm 顶更与 Ascend NPU 低相关 → skipped-low。

## Retrieval queries

1. va-knowledge 2026-09-19 daily PR incremental 16904 16915 16853
2. revised 16656 MERGED 16798 16468 16542
3. deferred 16545 MegaMoe 16848 default MRV2
4. pin 257739fc 383fbad9 fb0554b7 4c29f963 0344beea 7036bb09 aff1b74b
5. maintenance budget ceiling 20 min Asia/Shanghai 2026-09-19

## Ops note

- Fixed `/workspace/va-tools/bin/org-feed-publish-locked.sh` tip-compare status capture (NEEDS_PUBLISH exit 3 was swallowed by bash `if`; first attempt exited 0 without push). Re-ran locked publish → tip `caab5632…`.
