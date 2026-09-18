# Case: DSv4 DSpark `build_dspark_swa_indices` Triton 单核融合

- **日期**: 2026-09-18（修订）；初建 2026-09-15
- **状态**: OPEN not-draft / MERGEABLE / mergeStateStatus **BLOCKED** / reference-only
- **来源 PR**: https://github.com/vllm-project/vllm-ascend/pull/16542 （author `zhangxiaoshanha`；labels: `module:tests`, `module:ops`）
- **Pinned（本轮 gh 2026-09-18）**: base `d9ce0aa6f5c40769920512a69f82d97aecff5d40` · head `f4bb08f1c7a5b7e91e5183fe894a05fcb88b6fc7`

## 修订说明 / Revision notes（2026-09-18，Asia/Shanghai）

- **重 pin**：base 不变 `d9ce0aa6f5c40769920512a69f82d97aecff5d40`；head `b8df632c…` → `f4bb08f1c7a5b7e91e5183fe894a05fcb88b6fc7`。
- 增量 commit（VERIFIED）：`[Refactor] Rename runtime scalar kernel params to snake_case`（`f4bb08f1…`）。
- 规模仍约 +1233/−18，**5 files**；MERGEABLE **BLOCKED**。
- 保留旧对比：https://github.com/vllm-project/vllm-ascend/compare/d9ce0aa6f5c40769920512a69f82d97aecff5d40...b8df632c727f8ef46cf6a0e0c3a6e85cd33104b4
- 新对比：https://github.com/vllm-project/vllm-ascend/compare/d9ce0aa6f5c40769920512a69f82d97aecff5d40...f4bb08f1c7a5b7e91e5183fe894a05fcb88b6fc7
- 性能数字仍 **CLAIM**；部署收益 **UNVERIFIED**。


## 修订说明 / Revision notes（2026-09-17，Asia/Shanghai）

- **重 pin**：base `90b5dd80…` → `d9ce0aa6f5c40769920512a69f82d97aecff5d40`；head `cf795e72…` → `b8df632c727f8ef46cf6a0e0c3a6e85cd33104b4`。
- 相对上轮 tip 增量（VERIFIED）：`Trim kernel inline comments…`（`ff2430b57d16…`）；`[Test] Update DraftBuilder stub for set_dspark_num_query_per_req`（`b8df632c…`）。
- 规模 +1233/−18，**5 files**；仍 MERGEABLE **BLOCKED**。
- 保留旧对比：https://github.com/vllm-project/vllm-ascend/compare/90b5dd80d0180c4ddfe6d46a252d93d632b3c16b...cf795e72494ce8690a6bf8d3384463fa306b6b95
- 新对比：https://github.com/vllm-project/vllm-ascend/compare/d9ce0aa6f5c40769920512a69f82d97aecff5d40...b8df632c727f8ef46cf6a0e0c3a6e85cd33104b4
- 性能数字仍 **CLAIM**；部署收益 **UNVERIFIED**。

## 修订说明 / Revision notes（2026-09-15，Asia/Shanghai）

- 新建 case；与 #15626（AscendC 路线）对照标 **CLAIM** 关系；本 PR 为 Triton JIT 路线。
- 性能数字来自 PR body（910B4 / CANN 8.5.0 / torch_npu 2.9.0 / triton-ascend 3.2.0）→ 标 **CLAIM**（本轮未复跑）。
- E2E VllmRunner / nightly 注册未含 → **UNVERIFIED** 部署收益。
- Eligibility：A2 family gate；A3+ 需另行 sign-off（PR 自述）。

## Trigger — 何时想起本 case

讨论 **DSpark decode** 路径上 `build_dspark_swa_indices` 过重（eager 12-op / 多 launch / AiCPU `FloorDiv`），以及：

- ACL-graph / executor stream 下 **地址稳定** 的 indices buffer；
- Triton-Ascend vs AscendC（#15626）取舍；
- pad-row 清理与 capacity grid（`R_alloc × NUM_CB`）避免图重特化。

## Preconditions / environment signals

- 仓库：`vllm-project/vllm-ascend`；base `main` @ `d9ce0aa6…`；head @ `f4bb08f1…`。
- 关键文件（VERIFIED）：`ops/triton/dspark_swa_indices.py`（新）、`attention/dsa_v1.py`、`spec_decode/dspark_proposer.py`、nightly UT `test_dspark_swa_indices.py`。
- Gate（CLAIM from body）：`HAS_TRITON` × A2 family × pow2 `block_size` × `next_pow2(block_table_width) ≤ 8192`；否则 fallback eager。

## Observed failure pattern（若有）

- 动机为 **performance**（launch-bound），非 crash；PR 未附生产事故日志。

## Fix direction（仅限本 PR，附条件）

1. 单 launch Triton kernel 替代 eager 链；capacity grid 固定跨 ACL-graph replay。
2. Pad-row `[T_active, T_padded)` 每步重置 `(-1, 0)`；stable-address view `buffer[:T_active]`。
3. Ascend lowering CLAIM：fp32 roundtrip for `tl.gather`；int32→fp32 clamp/where。
4. Eager：eligible 时新分配；executor/ACL-graph：写持久 full-size buffer。
5. JIT warmup ledger keyed `(block_table_width, index_width)`。
6. `num_query_per_req`：anchor `spec` 与 non-anchor `spec+1` 均支持。

**条件**: head `b8df632c727f8ef46cf6a0e0c3a6e85cd33104b4`；OPEN blocked；无用户可见 API 变更（PR CLAIM）。

## Do-not-overgeneralize

- 禁止把 device-time 68× 说成本轮已复测（**CLAIM only**）。
- A2 gate ≠ A3 已验证。
- 对比 #15626 的 wall-clock 优劣依赖 W；禁止单一结论。
- UT 23/23 on 910B4 ≠ 全部署 E2E。

## Evidence links

- PR: https://github.com/vllm-project/vllm-ascend/pull/16542
- Compare（本轮）: https://github.com/vllm-project/vllm-ascend/compare/d9ce0aa6f5c40769920512a69f82d97aecff5d40...b8df632c727f8ef46cf6a0e0c3a6e85cd33104b4
- Compare（2026-09-15 tip 永久）: https://github.com/vllm-project/vllm-ascend/compare/90b5dd80d0180c4ddfe6d46a252d93d632b3c16b...cf795e72494ce8690a6bf8d3384463fa306b6b95
- Related AscendC route: https://github.com/vllm-project/vllm-ascend/pull/15626
- Head blob kernel: https://github.com/vllm-project/vllm-ascend/blob/b8df632c727f8ef46cf6a0e0c3a6e85cd33104b4/vllm_ascend/ops/triton/dspark_swa_indices.py

## Retrieval queries

1. build_dspark_swa_indices Triton fuse DSv4 DSpark decode
2. dspark_swa_indices capacity grid pad-row ACLGraph stable address
3. PR 16542 vs 15626 AscendC Triton-Ascend eligibility A2
4. dsa_v1 dspark_proposer HAS_TRITON block_table_width 8192
5. triton-ascend tl.gather fp32 roundtrip INDEX_W
