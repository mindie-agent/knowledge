# [参考] ACL Graph / host-side / padding 交叉专题（#16157 · #15645 · #16798 · #14409）

- **日期**: 2026-09-20（daily 轻量重 pin #16798）；weekly 首建 2026-09-19
- **状态**: cross-cutting digest / reference-only；**非**单 PR 镜像；NPU **UNVERIFIED**
- **阅读方式**: 聚合已有 cases/topics 的公开 pin；本轮未再跑 `gh` 重验、未跑 NPU

## 范围与条件（绑定已记录 pin）

本专题只串联下列已落库证据；SHAs 一律取自对应 topic `.meta.json` / case（2026-09-19 daily 及更早），**不**发明硬件结论。

| PR | 角色（交叉视角） | 状态（已记录） | base / head（已 pin） |
|---|---|---|---|
| [#16157](https://github.com/vllm-project/vllm-ascend/pull/16157) | FULL ACL Graph 下 Query-T / padding / seq_lens 长度契约（K3 DSpark） | OPEN · DIRTY | `fb2820b9…` / `debfa9bd…` |
| [#15645](https://github.com/vllm-project/vllm-ascend/pull/15645) | 310P MTP+ACLGraph 捕获期 D2H / 107027 | CLOSED unmerged | `b1c53925…` / `6730f85f…` |
| [#16798](https://github.com/vllm-project/vllm-ascend/pull/16798) | UpdatableGraph host-side 参数更新（FIA/SpecDecode） | OPEN · BLOCKED | `b255ab59…` / `9f4ac9d1…`（2026-09-20 daily re-pin） |
| [#14409](https://github.com/vllm-project/vllm-ascend/pull/14409) | hardware-aware physical draft-K ↔ ACLGraph/spec | OPEN · DIRTY | `0b345a67…` / `cede0513…` |

2026-09-20 daily：仅刷新 #16798 base/head（GraphQL）；其余行仍为 weekly 记录 pin。

`validation_scope`（sidecar）：`source_verification` of linked PR metas；`author_claims_separated`；`NPU_e2e_unverified`；跨代际（310P vs 910/A3/A5）**禁止外推**。

## 交叉观察（分层）

### VERIFIED（来自已有公开笔记，非本轮新测）

1. **Host↔device 同步 / 捕获边界**：#15645 记录捕获流上 D2H / `aclrtMemcpy` 107027 拒同步模式；#16798 引入 `UpdatableGraph` 把可变 host 参数从重捕获路径拆出（文件面含 `compilation/updatable_graph.py`、`acl_graph.py`）。
2. **Padding / 长度契约**：#16157 聚焦 FULL 图下 draft vs target 宽度、`slot_mapping=-1` padding 行、`num_actual_tokens` / corrected `seq_lens`；门控名 `_is_k3_dspark` **不能**外推为「非 K3 强制 eager」。
3. **Spec + Graph 共存**：#14409 在 MRV2 DSpark/DFlash 路径上动态 physical K；与 #16157/#16798 同属「图捕获后仍要正确喂长度/参数」问题族，但 **文件面与产品门控不同**，勿合并为单一修复。

### CLAIM（作者/PR 叙述，未本轮复验）

- #16798 手测矩阵（A3/A5 × MRV1/MRV2 FIA/MTP 等）。
- #14409 性能表（Qwen3-8B V2 D-Spark FULL Graph）。
- #15645「910 A2/A3 不变」边界叙述。

### UNVERIFIED / 冲突

- 全部条目：**本代理未跑 NPU**；合并/关闭 ≠ 正确性。
- #16157 / #14409 仍 DIRTY；#15645 CLOSED unmerged → **勿当 main 行为**。
- 310P (#15645) 与 K3/A3/A5 路径 **不可**互相替代。

## 链到既有 artifacts

| 类型 | Path |
|---|---|
| case | [`cases/pr-16157-acl-graph-padding-precision.case.md`](../cases/pr-16157-acl-graph-padding-precision.case.md) |
| topic | [`topics/pr-16157-acl-graph-padding-precision.md`](pr-16157-acl-graph-padding-precision.md) |
| case | [`cases/pr-15645-310p-mtp-aclgraph-d2h-107027.case.md`](../cases/pr-15645-310p-mtp-aclgraph-d2h-107027.case.md) |
| topic | [`topics/pr-15645-310p-mtp-aclgraph-capture-d2h.md`](pr-15645-310p-mtp-aclgraph-capture-d2h.md) |
| case | [`cases/pr-16798-updatable-graph-host-side-params.case.md`](../cases/pr-16798-updatable-graph-host-side-params.case.md) |
| topic | [`topics/pr-16798-updatable-graph-host-side-params.md`](pr-16798-updatable-graph-host-side-params.md) |
| case | [`cases/pr-14409-hardware-aware-dynamic-spec-k.case.md`](../cases/pr-14409-hardware-aware-dynamic-spec-k.case.md) |
| topic | [`topics/hardware-aware-physical-k-aclgraph.md`](hardware-aware-physical-k-aclgraph.md) |

## Do-not-overgeneralize

- 本文件是 **导航/对照**，不是第四个「总修复」PR。
- 单 PR 细节以分案为准；本专题只标交叉问题族。
- head 会移动时，以分案 `.meta.json` 为权威 pin。

## Retrieval queries

1. ACL Graph host-side padding UpdatableGraph 16157 16798 15645 14409
2. aclrtMemcpy 107027 capture D2H 310P MTP
3. Query-T seq_lens slot_mapping=-1 FULL graph K3 DSpark
4. physical_k hardware-aware ACLGraph MRV2
5. cluster topic weekly 2026-09-19 NPU_unverified

## Evidence links（聚合入口）

- https://github.com/vllm-project/vllm-ascend/pull/16157
- https://github.com/vllm-project/vllm-ascend/pull/15645
- https://github.com/vllm-project/vllm-ascend/pull/16798
- https://github.com/vllm-project/vllm-ascend/pull/14409
