# Maintenance run summary — 2026-09-15 daily PR incremental

- **Timezone context**: Asia/Shanghai（修订说明日期）；wall clock UTC may differ
- **Routine**: 「每日 PR 增量检查」；**budget ceiling ≤20 minutes**（预算上限，非实测耗时）
- **Constraints this run**: public `gh` only；未改上游；未装本地模型；未触 `_raw/`；**未删除**旧笔记；**未 recreate** 已有 case；package pin `cc1a1d76…`；verified curation-export → personal feed push if changed

## Scan window

- `gh search prs` `updated:>=2026-09-13`，每仓 ≤8
- 额外重检 tracked OPEN：#15832、#14409、#16157

## Disposition

| Source | Result |
|---|---|
| `vllm-ascend#15832` | **revised** pins base `cdad5a32…` head `22c8c589…`（was `660c4582…`/`cea32e97…`） |
| `vllm-ascend#14409` | **revised** head `6b458cff…`（was `2aacea2f…`）；now DIRTY；auto_tune bucket note |
| `vllm-ascend#16157` | **skip-unchanged** |
| `vllm-ascend#16542` | **new** case+topic（Triton SWA indices） |
| `vllm-ascend#16468` | **new** case+topic（A5 Kimi K3 draft） |
| `vllm-ascend#16555` | **new** case+topic（Mooncake EC MRv1） |
| `vllm-ascend#16236` | **deferred** |
| `vllm-ascend#16417` | **deferred** |
| main2main / noise | **skipped-low** |
| `vllm` top-8（CUDA/ROCm/rust/…） | **skipped-low**（无清晰 Ascend 信号） |
| #15645 / #15816 | kept；CLOSED 笔记未删 |

## Counts

- vllm-ascend scanned（list+tracked）：8 search + 3 tracked re-check（重叠不计双次处置）
- vllm scanned：8；written：0

## Artifacts this run

| Path | Role |
|---|---|
| `cases/pr-15832-…` / `topics/ascendstore-mp-…` | revised |
| `cases/pr-14409-…` / `topics/hardware-aware-…` | revised |
| `cases/pr-16542-…` / `topics/pr-16542-…` | new |
| `cases/pr-16468-…` / `topics/pr-16468-…` | new |
| `cases/pr-16555-…` / `topics/pr-16555-…` | new |
| `README.md` | index updated |
| `maintenance/2026-09-15-daily-pr-incremental.md` | this file |
| `meta/sources-2026-09-15.json` | local ops pins（**非 feed**） |

本地 `meta/` / `exports/` / `_raw/` **不属于** reference feed。

## Conflicts / unverified（摘要）

- #15832 仍 draft；A3 选测 fail 根因 UNVERIFIED。
- #14409 DIRTY；DCO/ci fail；NPU 增益 UNVERIFIED；须继续 pin-latest。
- #16468 draft 超大；验收矩阵 CLAIM only。
- #16542/#16555 性能/bench CLAIM；本轮未复跑。
- #16157 无变化。

## Retrieval queries

1. va-knowledge 2026-09-15 daily PR incremental 15832 14409 16542 16468 16555
2. AscendStore mp re-pin 22c8c589 hardware_aware 6b458cff DIRTY
3. Triton dspark_swa_indices 16542 A5 Kimi K3 16468 Mooncake EC 16555
4. skip-unchanged 16157 deferred 16236 16417
5. maintenance budget ceiling 20 min Asia/Shanghai 2026-09-15
