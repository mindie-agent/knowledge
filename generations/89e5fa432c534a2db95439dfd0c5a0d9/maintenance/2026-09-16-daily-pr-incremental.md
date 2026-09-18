# Maintenance run summary — 2026-09-16 daily PR incremental

- **Timezone context**: Asia/Shanghai（修订说明日期）；wall clock UTC may differ
- **Routine**: 「每日 PR 增量检查」；**budget ceiling ≤20 minutes**（预算上限，非实测耗时）
- **Constraints this run**: public `gh` only；未改上游；未装本地模型；未触 `_raw/`；**未删除**旧笔记；**未 recreate** 已有 case；package pin `cc1a1d76…`；verified curation-export → personal feed push if changed

## Scan window

- `gh pr list --search updated:>=2026-09-14`，每仓 ≤8
- 额外重检 tracked：#15832、#14409、#16157、#16542、#16468、#16555
- deferred 快检：#16236、#16417

## Disposition

| Source | Result |
|---|---|
| `vllm-ascend#15832` | **revised** → **CLOSED** not-merged；pins base `26f1363f…` head `b9227148…` |
| `vllm-ascend#14409` | **revised** base `0b345a67…` head `6ee85790…`；仍 DIRTY |
| `vllm-ascend#16157` | **skip-unchanged** base `fb2820b9…` head `debfa9bd…` |
| `vllm-ascend#16542` | **skip-unchanged** base `90b5dd80…` head `cf795e72…` |
| `vllm-ascend#16468` | **revised** base `c267db73…` head `4e519623…`；现 DIRTY；规模膨胀 |
| `vllm-ascend#16555` | **revised** head `e634788d…`（base 同）；mypy/alias commits |
| `vllm-ascend#16634` | **new** case+topic（A5 MegaMoE W4A8） |
| `vllm-ascend#16629` | **new** case+topic（DSA local metadata fixed-capacity Triton） |
| `vllm-ascend#16636` | **new** case+topic（DSpark empty valid context） |
| `vllm-ascend#16635` | **skipped-low**（小 FusedMoE all-reduce dep；记录于 scan） |
| `vllm-ascend#16633/#16630/#16627` | **skipped-low**（doc/CI noise） |
| `vllm-ascend#16628` | **deferred**（DSpark top-k draft DIRTY） |
| `vllm-ascend#16236` | **deferred**（仍 rename/tuple 级） |
| `vllm-ascend#16417` | **deferred**（title 现为 Eagle3 E2E 补测；仍 defer） |
| `vllm` top-8 | **skipped-low**（CUDA/ROCm/frontend；#57086 Kimi-K3 AR overlap 无 Ascend 直接信号） |
| #15645 / #15816 | kept CLOSED notes；未 recreate |

## Counts

- vllm-ascend scanned（list+tracked）：8 search + tracked re-check
- vllm scanned：8；written：0
- revised：4；new：3；skip-unchanged：2；deferred：3+；skipped-low：若干

## Artifacts this run

| Path | Role |
|---|---|
| `cases/pr-15832-…` / `topics/ascendstore-mp-…` | revised（CLOSED） |
| `cases/pr-14409-…` / `topics/hardware-aware-…` | revised |
| `cases/pr-16468-…` / `topics/pr-16468-…` | revised |
| `cases/pr-16555-…` / `topics/pr-16555-…` | revised |
| `cases/pr-16634-…` / `topics/pr-16634-…` | new |
| `cases/pr-16629-…` / `topics/pr-16629-…` | new |
| `cases/pr-16636-…` / `topics/pr-16636-…` | new |
| `README.md` | index updated |
| `maintenance/2026-09-16-daily-pr-incremental.md` | this file |
| `meta/sources-2026-09-16.json` | local ops pins（**非 feed**） |

本地 `meta/` / `exports/` / `_raw/` **不属于** reference feed。

## Conflicts / unverified（摘要）

- #15832 CLOSED not-merged；关闭动机 UNVERIFIED。
- #14409 仍 DIRTY；NPU 增益 UNVERIFIED；须继续 pin-latest。
- #16468 draft+DIRTY；验收 CLAIM only；experimental not-to-merge CLAIM。
- #16634 CLEAN 但作者自述无 NPU runtime → UNVERIFIED。
- #16629 blocked；re-JIT 收益 CLAIM。
- #16636 多项 e2e pass 但仍 BLOCKED（整体原因 UNVERIFIED）。
- #16157/#16542 无变化。

## Retrieval queries

1. va-knowledge 2026-09-16 daily PR incremental 15832 CLOSED 14409 16468 16555
2. new 16634 MegaMoE W4A8 16629 DSA metadata Triton 16636 empty context
3. skip-unchanged 16157 16542 deferred 16236 16417 16628
4. pin b9227148 6ee85790 4e519623 e634788d 3d596b8f 70540b56 1889ebee
5. maintenance budget ceiling 20 min Asia/Shanghai 2026-09-16
