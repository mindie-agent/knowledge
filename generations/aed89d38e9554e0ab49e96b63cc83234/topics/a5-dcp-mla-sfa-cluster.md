# [参考] A5 DCP / MLA / SFA 交叉专题（#16468 · #16656）

- **日期**: 2026-09-20（daily 轻量重 pin #16468）；weekly 首建 2026-09-19
- **状态**: cross-cutting digest / reference-only；**显式区分** MERGED vs OPEN draft
- **阅读方式**: 聚合已有 cases/topics 的公开 pin；本轮未再跑 `gh` 重验、未跑 NPU

## 范围与条件（绑定已记录 pin）

| PR | 角色（交叉视角） | 状态（已记录） | base / head（已 pin） |
|---|---|---|---|
| [#16468](https://github.com/vllm-project/vllm-ascend/pull/16468) | A5 Kimi K3 大包：DCP / DSpark ACLGraph / Flash MLA·GQA / MLA C8 | OPEN **draft** · DIRTY | `9ad52992…` / `9410826a…`（2026-09-20；base=#16545 mergeCommit） |
| [#16656](https://github.com/vllm-project/vllm-ascend/pull/16656) | A5 SFA DCP padded-index LSE + C8 capability gate | **MERGED** · mergeCommit `aff1b74b…` | `c7ca0b67…` / `13816345…` |

`validation_scope`：`source_verification`；`author_claims_separated`；`NPU_e2e_unverified`；**MERGED≠本轮复验**；#16468 **experimental draft reference-only**。

## 交叉观察（分层）

### VERIFIED（来自已有公开笔记）

1. **状态不对称（关键）**：#16656 已于 2026-09-18 UTC 合入（mergeCommit `aff1b74b66467a7805cde69ef0728b7e32c0f990`）；#16468 仍为超大 **draft + DIRTY**（约 +121k / 603 files @ 2026-09-20 pin；base 对齐 #16545 MegaMoe merge）→ **不可**把 draft 行为当成已合入 A5 默认路径。
2. **SFA DCP / C8 门控（#16656）**：能力名 `SFA_DCP_REPLICATED_INDEXER` → `SFA_C8_DCP_REPLICATED_INDEXER`；A5 profile 去掉该 C8 能力；仅在 `enable_sparse_sfa_c8` 且缺能力时 raise（允许 non-C8 SFA DCP）。
3. **#16468 tip 主题（commit headlines）**：含 A5 K3 graphs/operator optimizations 与「A5 MLA C8 and direct DCP output」及「bounded non-absorbed A5 MLA prefill」（2026-09-20 tip）——与 #16656 C8/DCP 叙事 **相邻但独立**；draft 内实现是否等价合入树 → **UNVERIFIED**。

### CLAIM

- #16468 验收/性能/experimental-not-to-merge 叙述。
- #16656 数值与全拓扑正确性（合并后仍未本轮复测）。

### UNVERIFIED / 冲突

- 禁止用 #16656 MERGED 推断 #16468 可合入或行为一致。
- 禁止发明「A5 全量 DCP 已就绪」硬件结论。

## 链到既有 artifacts

| 类型 | Path |
|---|---|
| case | [`cases/pr-16468-a5-kimi-k3-dcp-dspark-aclgraph.case.md`](../cases/pr-16468-a5-kimi-k3-dcp-dspark-aclgraph.case.md) |
| topic | [`topics/pr-16468-a5-kimi-k3-flash-mla-dcp.md`](pr-16468-a5-kimi-k3-flash-mla-dcp.md) |
| case | [`cases/pr-16656-a5-sfa-dcp-padded-lse.case.md`](../cases/pr-16656-a5-sfa-dcp-padded-lse.case.md) |
| topic | [`topics/pr-16656-a5-sfa-dcp-padded-lse.md`](pr-16656-a5-sfa-dcp-padded-lse.md) |

## Do-not-overgeneralize

- MERGED (#16656) vs OPEN draft (#16468) 必须在任何摘要中并列标明。
- 权威 pin 在分案 `.meta.json`。

## Retrieval queries

1. A5 DCP MLA SFA 16468 16656 cluster
2. SFA_C8_DCP_REPLICATED_INDEXER padded LSE MERGED
3. Kimi K3 draft DIRTY experimental 16468
4. mergeCommit aff1b74b weekly 2026-09-19
5. MERGED_vs_OPEN_draft NPU_unverified

## Evidence links

- https://github.com/vllm-project/vllm-ascend/pull/16468
- https://github.com/vllm-project/vllm-ascend/pull/16656
- https://github.com/vllm-project/vllm-ascend/commit/aff1b74b66467a7805cde69ef0728b7e32c0f990
