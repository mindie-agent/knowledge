# Topic: GLM-5.3-Flash CANN KeyPool / PoolKeyIndexer

- **日期**: 2026-09-20（修订）；初建 2026-09-17
- **关联 case**: [`cases/pr-16730-glm53-cann-keypool-indexer.case.md`](../cases/pr-16730-glm53-cann-keypool-indexer.case.md)
- **PR**: https://github.com/vllm-project/vllm-ascend/pull/16730
- **Pins**: base `b255ab59…` · head `7ae1df8d…` · OPEN BLOCKED；+576/−142，8 files

## 要点

- **2026-09-20 VERIFIED**：DIRTY→BLOCKED；tip「use external CANN KeyPool operators」；diff 缩至 8 files（相对早期 vendored 树）。
- **CLAIM**：自动启用 CANN ops；评分契约相对旧 Triton 可能不同。
- **UNVERIFIED**：NPU 正确性；与 #16936 MRV2 GLM-5.3-Flash（draft）勿混 pin。

## 勿过度推广

- external CANN 包版本矩阵未本轮验证；勿假设与 vendored 数值等价。

## Retrieval queries

1. GLM-5.3-Flash KeyPool PoolKeyIndexer CANN
2. PR 16730 external CANN KeyPool operators
3. sparse_attn_indexer_kpool Ascend
