# Topic: GLM-5.3-Flash CANN KeyPool / PoolKeyIndexer

## 修订（2026-09-18）

- head → `b5cc342f…`；现 CONFLICTING/DIRTY；ACLNN dispatch local refactor；仍无 device tests → **UNVERIFIED**。


- **日期**: 2026-09-18（修订）；初建 2026-09-17
- **关联 case**: [`cases/pr-16730-glm53-cann-keypool-indexer.case.md`](../cases/pr-16730-glm53-cann-keypool-indexer.case.md)
- **PR**: https://github.com/vllm-project/vllm-ascend/pull/16730
- **Pins**: base `e1d14903…` · head `b5cc342f…` · OPEN DIRTY

## 要点

- **VERIFIED**：大体积 vendor `csrc/attention/key_pool` + GLM5-Next 适配；两 commits。
- **CLAIM**：替换 Triton pooling/sparse index；自动启用；评分契约变更。
- **UNVERIFIED**：device 正确性（仅 CPU mock UT）。

## 勿过度推广

- blocked + 无 device tests → 参考-only。

## Retrieval queries

1. GLM5-Next KeyPool PoolKeyIndexer CANN Ascend
2. PR 16730 GLM-5.3-Flash vendor ops-transformer
3. Triton pooling vs CANN KeyPool scoring contract
