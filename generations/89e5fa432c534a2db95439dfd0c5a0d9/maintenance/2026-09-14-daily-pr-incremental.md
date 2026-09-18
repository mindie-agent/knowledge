# Maintenance run summary — 2026-09-14 daily PR incremental

- **Timezone context**: Asia/Shanghai（修订说明日期）；wall clock UTC may differ
- **Routine**: 「每日 PR 增量检查」；**budget ceiling ≤20 minutes**（预算上限，非实测耗时）
- **Constraints this run**: 未 push / 未 export / 未 git；未装本地模型；未碰上游；未触 `_raw/`；**未删除**旧笔记；**未 recreate** #16157

## Sources covered

| Source | Result |
|---|---|
| `vllm-project/vllm-ascend#15645` | NEW case+topic；CLOSED unmerged；pinned base `b1c53925…` head `6730f85f…` |
| `vllm-project/vllm-ascend#15816` | NEW case+topic；CLOSED CONFLICTING/DIRTY；pinned base `45fc6040…` head `22e086b0…`；cross-link #15645 |
| `vllm-project/vllm-ascend#15832` | NEW case+topic；OPEN **draft** blocked；pinned base `660c4582…` head `cea32e97…` |
| `vllm-project/vllm-ascend#14409` | NEW case+topic；OPEN blocked；head **moved** → pin `2aacea2f…`（vs earlier `cff3def…`）；pin-latest-each-run |
| `vllm-project/vllm-ascend#16157` | **skip-unchanged**（既有 case/topic 保留，不 recreate） |
| vllm XPU / low-priority PRs | **skipped**（本轮范围外） |
| Optional deferred `50984` / `56737` | **deferred**（未写笔记） |

## Artifacts this run

| Path | Role |
|---|---|
| `cases/pr-15645-310p-mtp-aclgraph-d2h-107027.case.md` | new |
| `topics/pr-15645-310p-mtp-aclgraph-capture-d2h.md` | new |
| `cases/pr-15816-310p-mrv2-mtp-qwen35.case.md` | new |
| `topics/pr-15816-310p-mrv2-mtp-adaptation.md` | new |
| `cases/pr-15832-ascendstore-multiprocess-kv-transfer.case.md` | new |
| `topics/ascendstore-mp-kv-ipc-event-ownership.md` | new |
| `cases/pr-14409-hardware-aware-dynamic-spec-k.case.md` | new |
| `topics/hardware-aware-physical-k-aclgraph.md` | new |
| `README.md` | index updated（保留 #16157；注明 meta/exports 非 feed） |
| `maintenance/2026-09-14-daily-pr-incremental.md` | this file |
| `meta/sources-2026-09-14.json` | local ops pins + skipped list（**非 feed**） |

本地 `meta/` / `exports/` / `_raw/` **不属于** reference feed。公开证据请用各笔记内 GitHub PR/compare/blob 链接。

## One-line focus per PR

| PR | Focus |
|---|---|
| #15645 | 310P MRv1 MTP+ACLGraph：图外 host splitfuse mask + 拒捕获流 D2H（107027） |
| #15816 | 310P MRv2 MTP Qwen3.5 新栈；与 15645 共享 D2H/mask 模式（分案）；CLOSED DIRTY |
| #15832 | AscendStore 可选 multiprocess KV；Worker 留 Event；draft |
| #14409 | hardware_aware 动态物理 K + ACLGraph capture scopes；head 移动中 |
| #16157 | skip-unchanged |

## Conflicts / unverified（摘要）

- #15645 / #15816：**CLOSED unmerged**；15816 **DIRTY** → 最终形态未知；310P ≠ 910 A2/A3。
- #15832：**draft**；Event hang 全版本复现与 merge readiness **UNVERIFIED**。
- #14409：DCO/ci-gate/pre-commit fail；NPU 性能 **UNVERIFIED**；须每轮重 pin head。
- 本轮未跑 UT/E2E/NPU；未独立 `gh` 再抓（使用委托 pinned SHAs）。

## Links

- Maintenance: this file
- Meta pins: [`../meta/sources-2026-09-14.json`](../meta/sources-2026-09-14.json)（local ops only）
- Index: [`../README.md`](../README.md)

## Retrieval queries

1. `va-knowledge 2026-09-14 daily PR incremental 15645 15816 15832 14409`
2. `310P MTP ACLGraph 107027 MRv2 skip 16157`
3. `AscendStore use_multiprocess draft Event ownership`
4. `hardware-aware physical_k pin-latest-each-run 2aacea2f`
5. `maintenance budget ceiling 20 min Asia/Shanghai 2026-09-14`
