# Maintenance run summary — 2026-09-18 MindIE acceptance incremental

- **Timezone context**: Asia/Shanghai（修订说明日期）；wall clock also recorded in UTC
- **Routine**: MindIE Agent acceptance — ONE real PR/knowledge incremental maintenance
- **Budget ceiling**: ≤20 minutes（预算上限，非实测耗时声明）
- **Constraints**: public `gh` only；未改上游 / 未发上游评论；未装模型；未跑 NPU；未触 `_raw/`；**未删除**旧笔记；**未 recreate** 已有 case（in-place 修订 + 保留旧 links）；package pin `cc1a1d76fdee2c90b129f6ba56c171d500c158af`；schema `vaws-curation-export/1`；publish only to `maoxx241/vaws-knowledge` branch `codex/va-reference-feed`

## Wall clock

- **start**: 2026-09-18 12:30:49 CST (UTC+0800) / 2026-09-18 04:30:49 UTC
- **end**: （export/push 完成后回填）

## Prior feed tip（verified）

- `e4e3b965ab828c7bed7077640cb3f165d7f28687` on `codex/va-reference-feed`

## Scan window

- Priority re-check：`vllm-ascend#16798`、`#16656`（GraphQL/REST base+head）
- Tracked pin spot-check：`#16468`（pins moved）以及 `#16542/#16731/#16730/#16629/#16729/#16119/#16363`
- Open search ≤8：`vllm-ascend` + `vllm`（updated desc）
- deferred：新 PR 笔记（#16800 EPLB draft、#16140 MRV2 PP、#16363 FLy、#16849 runtime_guard、#16780 MXFP8、#16119 KV Pool 等）

## Disposition

| Source | Result |
|---|---|
| `vllm-ascend#16798` | **skip-unchanged**；base `3db3f931…` head `3e33c9fd…`；OPEN BLOCKED |
| `vllm-ascend#16656` | **revised**；base `c7ca0b67…`（same）；head `60c6c32a…` → `13816345…`；C8 capability rename/gate |
| `vllm-ascend#16468` | **revised** pins；base `00b0b979…` → `cb82154e…`；head `d8a34b81…` → `3c94ce74…`；仍 draft DIRTY |
| `vllm-ascend#16542/#16731/#16730/#16629/#16729` | **skip-unchanged**（head 未动） |
| `vllm-ascend` open top-8（#16800/#16140/#15935/#16780/#16849/#15011/#16853/#16852） | **budget-deferred**（有信号但未写新 case） |
| `vllm` top-8 | **skipped-low**（Frontend/XGrammar、NVIDIA GLM kpool、MoonEP、ROCm、CPU Zen、Triton acceptance estimator 等；#57477 为 NVIDIA 路径） |

## Counts

- revised：2（#16656、#16468）
- skip-unchanged priority：1（#16798）
- new cases：0
- deferred/skipped-low：若干（见上）

## Artifacts this run

| Path | Role |
|---|---|
| `cases/pr-16656-a5-sfa-dcp-padded-lse.case.md` | revised |
| `topics/pr-16656-a5-sfa-dcp-padded-lse.md` | revised |
| `cases/pr-16468-a5-kimi-k3-dcp-dspark-aclgraph.case.md` | revised pins |
| `topics/pr-16468-a5-kimi-k3-flash-mla-dcp.md` | revised pins |
| `README.md` | index + acceptance high notes |
| `maintenance/2026-09-18-mindie-acceptance-incremental.md` | this file（ops evidence） |
| `meta/sources-2026-09-18-mindie-acceptance.json` | local ops pins（**非 feed**） |

本地 `meta/` / `exports/` / `_raw/` **不属于** reference feed。

## Conflicts / unverified（摘要）

- #16656：C8 gate VERIFIED 控制流；数值/e2e 仍 UNVERIFIED；BLOCKED。
- #16468：仍 draft+DIRTY；超大 diff；验收 CLAIM。
- #16798：pins 未变；仍 BLOCKED；手测 CLAIM。
- 未跑 NPU；未装模型。

## Retrieval queries

1. MindIE acceptance 2026-09-18 incremental 16656 16798 16468
2. SFA_C8_DCP_REPLICATED_INDEXER enable_sparse_sfa_c8 A5 gate
3. skip-unchanged 16798 UpdatableGraph
4. pin 13816345 cb82154e 3c94ce74 e4e3b965
5. maintenance budget ceiling 20 min Asia/Shanghai 2026-09-18 MindIE
