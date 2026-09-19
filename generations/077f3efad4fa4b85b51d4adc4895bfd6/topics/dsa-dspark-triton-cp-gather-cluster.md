# [参考] DSA / DSpark / Triton / CP gather 交叉专题（#16542 · #16629 · #16915 · #16636）

- **日期**: 2026-09-19（weekly 首建）
- **状态**: cross-cutting digest / reference-only；**非**单 PR 镜像；NPU **UNVERIFIED**（本轮）
- **阅读方式**: 聚合已有 cases/topics 的公开 pin；本轮未再跑 `gh` 重验、未跑 NPU

## 范围与条件（绑定已记录 pin）

| PR | 角色（交叉视角） | 状态（已记录） | base / head（已 pin） |
|---|---|---|---|
| [#16542](https://github.com/vllm-project/vllm-ascend/pull/16542) | DSpark `build_dspark_swa_indices` Triton 单核融合 | OPEN · DIRTY | `82b0b10a…` / `7036bb09…` |
| [#16629](https://github.com/vllm-project/vllm-ascend/pull/16629) | DSA local token metadata 固定容量 Triton（抑 re-JIT） | OPEN · BLOCKED | `714dd1d1…` / `cefa37ab…` |
| [#16915](https://github.com/vllm-project/vllm-ascend/pull/16915) | DSA-CP prefill fused cache gather + token-sharded O-proj | OPEN · BLOCKED | `c8addbb2…` / `383fbad9…` |
| [#16636](https://github.com/vllm-project/vllm-ascend/pull/16636) | DSpark empty valid context / IndexCheck after discard | OPEN · BLOCKED | `c267db73…` / `1889ebee…` |

`validation_scope`：`source_verification` of linked PR metas；`author_claims_separated`；`NPU_e2e_unverified`；#16915 作者 A3 手测 → **CLAIM only**。

## 交叉观察（分层）

### VERIFIED（来自已有公开笔记）

1. **固定容量 / 抑特化**：#16542（SWA indices）与 #16629（local metadata）同属「避免逐步 shape 触发 Triton re-specialize」思想，但 **分案**：indices fuse vs CP local metadata builder。
2. **CP prefill 集体通信成本**：#16915 文件面覆盖 `cache_store` / `sfa_cp` / indexer / MLA patch；主题是 gather/store 与 O-proj shard，不是 indices kernel。
3. **Spec discard 边界**：#16636 针对 async reject 后 `valid_ctx_end == ctx_start` 的 RoPE IndexCheck；与 fuse/gather **正交**，同挂 DSpark/DSA 运维检索。

### CLAIM

- #16542 性能数字；#16629 re-JIT 秒级 vs ~1µs；#16915 profiled gather 成本与 A3 NPU 测试矩阵；#16636 NPU regression 计数。

### UNVERIFIED / 冲突

- 本轮未跑 NPU；#16542 rebase 后 DIRTY — 合入路径未定。
- 勿把 #16915「无新配置」CLAIM 当成全 SKU 通用保证。

## 链到既有 artifacts

| 类型 | Path |
|---|---|
| case | [`cases/pr-16542-dspark-swa-indices-triton-fuse.case.md`](../cases/pr-16542-dspark-swa-indices-triton-fuse.case.md) |
| topic | [`topics/pr-16542-dspark-swa-indices-triton.md`](pr-16542-dspark-swa-indices-triton.md) |
| case | [`cases/pr-16629-dsa-local-metadata-fixed-capacity-triton.case.md`](../cases/pr-16629-dsa-local-metadata-fixed-capacity-triton.case.md) |
| topic | [`topics/pr-16629-dsa-local-metadata-fixed-capacity-triton.md`](pr-16629-dsa-local-metadata-fixed-capacity-triton.md) |
| case | [`cases/pr-16915-dsa-cp-prefill-fused-cache-gather.case.md`](../cases/pr-16915-dsa-cp-prefill-fused-cache-gather.case.md) |
| topic | [`topics/pr-16915-dsa-cp-prefill-fused-cache-gather.md`](pr-16915-dsa-cp-prefill-fused-cache-gather.md) |
| case | [`cases/pr-16636-dspark-empty-valid-context.case.md`](../cases/pr-16636-dspark-empty-valid-context.case.md) |
| topic | [`topics/pr-16636-dspark-empty-valid-context.md`](pr-16636-dspark-empty-valid-context.md) |

## Do-not-overgeneralize

- Triton fuse ≠ CP fused gather ≠ empty-context guard；检索同簇，修复勿混用。
- 权威 pin 在分案 `.meta.json`。

## Retrieval queries

1. DSA DSpark Triton fixed-capacity 16542 16629
2. DSA-CP prefill fused cache gather 16915
3. empty valid context IndexCheck dspark 16636
4. re-JIT SWA indices local metadata cluster
5. weekly cluster 2026-09-19 NPU_unverified

## Evidence links

- https://github.com/vllm-project/vllm-ascend/pull/16542
- https://github.com/vllm-project/vllm-ascend/pull/16629
- https://github.com/vllm-project/vllm-ascend/pull/16915
- https://github.com/vllm-project/vllm-ascend/pull/16636
