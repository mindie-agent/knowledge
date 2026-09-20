# Topic: MiniMax-M3 CANN Ops sparse attention（GBSA）

- **日期**: 2026-09-20（修订）；初建 2026-09-19
- **关联 case**: [`cases/pr-16904-minimax-m3-cann-ops-sparse-attention.case.md`](../cases/pr-16904-minimax-m3-cann-ops-sparse-attention.case.md)
- **PR**: https://github.com/vllm-project/vllm-ascend/pull/16904
- **Pins**: base `b255ab59…` · head `e4374826…` · OPEN BLOCKED；+394/−43581，162 files

## 要点

- **2026-09-20 VERIFIED**：rebase + head tip 增加 CANN-op-unavailable / E2E skip tests；仍 BLOCKED。
- **CLAIM**：`generic_block_sparse_attention`；薄 ACLNN adapter；A5 mixed KV-cache。
- **UNVERIFIED**：本轮 NPU e2e；全 CANN 版本。

## 勿过度推广

- MiniMax-M3 范围；勿并入 #16545 MegaMoe（已 MERGED，另案）或 #16426 IndexScore。

## Retrieval queries

1. MiniMax-M3 CANN GBSA sparse attention Ascend
2. PR 16904 msa_index_score removal
3. generic_block_sparse_attention npu_msa_index_score adapter
