# [参考] DSpark SWA indices Triton 融合核（PR #16542）

- **日期**: 2026-09-15
- **状态**: OPEN / blocked / reference-only
- **阅读方式**: `gh` 元数据 + PR body/files（未跑 NPU）

## 修订（2026-09-18）

- base `d9ce0aa6…` / head `f4bb08f1…`；snake_case runtime scalar rename；仍 BLOCKED；perf **CLAIM**。

## 修订（2026-09-17）

- base `d9ce0aa6…` / head `b8df632c…`；comment trim + DraftBuilder stub；仍 BLOCKED；perf **CLAIM**。

## Pinned（本轮）

| 字段 | 值 |
|---|---|
| PR | https://github.com/vllm-project/vllm-ascend/pull/16542 |
| 标题 | `[Performance][DSA] Fuse build_dspark_swa_indices into a single Triton kernel for DSv4 DSpark decode` |
| state | OPEN；MERGEABLE；BLOCKED |
| base / head | `d9ce0aa6f5c40769920512a69f82d97aecff5d40` / `f4bb08f1c7a5b7e91e5183fe894a05fcb88b6fc7` |
| 规模 | +1285 / −18，**4 files** |
| 对比 | https://github.com/vllm-project/vllm-ascend/compare/90b5dd80d0180c4ddfe6d46a252d93d632b3c16b...b8df632c727f8ef46cf6a0e0c3a6e85cd33104b4 |

### CI（摘要）

| Check | 结论 |
|---|---|
| DCO / pre-commit / PR create / main | **pass** |
| cpu-ut | pending（本轮扫描时） |

## 为何重要（VA / NPU）

- DSpark decode 热路径 indices 构建；ACL-graph/executor 需要 **指针稳定**。
- Triton-Ascend 零 C++ rebuild vs #15626 AscendC 轮路径对比。

## VERIFIED vs CLAIM vs UNVERIFIED

- **VERIFIED**：变更文件集合；OPEN/BLOCKED；对比 URL；无用户 API 变更自述需当 CLAIM。
- **CLAIM**：device-time ~68× / wall ~5× vs eager；vs #15626 device 8.64×；23 UT pass on 910B4；A2-only gate。
- **UNVERIFIED**：生产 E2E；A3+；nightly 注册；本环境复测。

## Retrieval queries

1. Triton build_dspark_swa_indices ACLGraph executor stream
2. PR 16542 cf795e72 dspark_swa_indices A2 gate
3. AscendC 15626 vs Triton 16542 per_token_lens address stability
4. max_num_reqs_for_dspark R_alloc NUM_CB capacity grid
5. triton-ascend 3.2.0 DSpark SWA indices pad cleanup
