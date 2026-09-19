# [参考] A5 Kimi K3 Flash MLA/GQA + DCP + DSpark ACLGraph（PR #16468）

- **日期**: 2026-09-18 MindIE acceptance（修订）；初建 2026-09-15
- **状态**: OPEN **draft** / **DIRTY·CONFLICTING** / reference-only；超大变更集；PR 自述 experimental / not-intended-to-merge（CLAIM）
- **阅读方式**: `gh` 元数据 + PR body/file churn（未跑 NPU；未装模型）

## 修订（2026-09-18 MindIE acceptance）

- base `cb82154e…` / head `3c94ce74…`；+78628/415 files；仍 draft DIRTY；验收 **CLAIM**。

## 修订（2026-09-18 日更）

- base `00b0b979…` / head `d8a34b81…`；+78.5k/411 files；仍 draft DIRTY；验收 **CLAIM**。

## 修订（2026-09-17）

- base `10b4fb2a…` / head `48c8a870…`；+72.6k/350 files；仍 draft DIRTY；验收 **CLAIM**。

## Pinned（本轮 2026-09-18 MindIE acceptance）

| 字段 | 值 |
|---|---|
| PR | https://github.com/vllm-project/vllm-ascend/pull/16468 |
| state | OPEN draft；CONFLICTING；DIRTY |
| base / head | `cb82154ea226c733ba99fe76d7ee3bd3af137ed1` / `3c94ce740126984b9683501dd8c3772bf88d4311` |
| 规模 | +78628 / −1695，**415 files** |
| 对比（本轮） | https://github.com/vllm-project/vllm-ascend/compare/cb82154ea226c733ba99fe76d7ee3bd3af137ed1...3c94ce740126984b9683501dd8c3772bf88d4311 |
| 对比（日更 tip 永久） | https://github.com/vllm-project/vllm-ascend/compare/00b0b979fc605fbdedafa83d85d02c58c42ab8f3...d8a34b8161d27992a7c2ad439673f2d3e2073c35 |

## Prior Pinned（2026-09-16）

| 字段 | 值 |
|---|---|
| PR | https://github.com/vllm-project/vllm-ascend/pull/16468 |
| 标题 | `[Feat][A5] Kimi K3 MRv1/MRv2 DCP, DSpark ACLGraph and strided Flash MLA/GQA` |
| state | OPEN；**isDraft=true**；CONFLICTING；DIRTY |
| base / head | `c267db731d03e18042047ea1594e033673e46a6f` / `48c8a870d6b376b1ff854d636a2babe543cb395b` |
| 规模 | +66264 / −337，**292 files** |
| 对比（本轮） | https://github.com/vllm-project/vllm-ascend/compare/c267db731d03e18042047ea1594e033673e46a6f...48c8a870d6b376b1ff854d636a2babe543cb395b |
| 对比（2026-09-15 tip 永久） | https://github.com/vllm-project/vllm-ascend/compare/26f1363f7180dfcbeac1230679976cede6010fc8...8a293f09830a5c55bd4fc5c2e2219f0edcd59443 |

### Prior pins

| 轮次 | base | head |
|---|---|---|
| 2026-09-15 | `26f1363f7180dfcbeac1230679976cede6010fc8` | `8a293f09830a5c55bd4fc5c2e2219f0edcd59443` |

### CI（摘要）

| Check | 结论 |
|---|---|
| DCO / PR create | **pass**（本轮扫描摘要） |
| merge conflicts | **DIRTY** |
| 合入意图 | experimental cherry-pick（CLAIM） |

## Themes（索引）

1. **Flash MLA/GQA kernels（A5）** — strided / first-axis noncontiguous KV；paged current-chunk 128-token CLAIM
2. **DSpark ACLGraph** — DeviceMetadataTask/Executor；context prep outside query graph
3. **DCP** — gather Q / interleave-aware local KV；causal history vs current；noncausal DSpark full local seq
4. **MRv2 policy3 / DCP8 small-decode / EPLB** — 本轮正文增量 CLAIM
5. **分案** — rc `#16157` ≠ 本 main/A5 扩展；相关 #16417/#16318/#16347 为整合 CLAIM

## VERIFIED / CLAIM / UNVERIFIED

- **VERIFIED**：draft/DIRTY；pins；规模膨胀；labels 含 merge-conflicts。
- **CLAIM**：验收矩阵；vLLM `84030bbe…`；CANN FlashAttn 192/128 binding；B035 行为差异；EPLB on `64e6ab95`。
- **UNVERIFIED**：合入形态；本环境复现；与 #16157 行为等价。

## Retrieval queries

1. Kimi K3 A5 Flash MLA GQA DSpark ACLGraph draft 16468
2. DCP8 small-decode MRv2 policy3 EPLB 4e519623
3. DeviceMetadataTask ACLGraph padded requests DCP slots
4. PR 16468 vs 16157 rc0.26 padding separate cases
5. head 4e519623 rfc16464 DIRTY draft
