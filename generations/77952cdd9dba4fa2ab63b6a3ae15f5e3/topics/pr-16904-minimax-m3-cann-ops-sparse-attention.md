# Topic: MiniMax-M3 CANN Ops sparse attention（GBSA）

- **日期**: 2026-09-19
- **关联 case**: [`cases/pr-16904-minimax-m3-cann-ops-sparse-attention.case.md`](../cases/pr-16904-minimax-m3-cann-ops-sparse-attention.case.md)
- **PR**: https://github.com/vllm-project/vllm-ascend/pull/16904
- **Pins**: base `c8addbb2…` · head `257739fc…` · OPEN BLOCKED；+361/−43568，161 files

## 要点

- **VERIFIED**：大规模删除 in-tree MSA/sparse score；Python/binding/adapter 文件面；MERGEABLE BLOCKED。
- **CLAIM**：路由到 `generic_block_sparse_attention`；薄 ACLNN adapter；A5 mixed KV-cache；CANN 9.2 签名对照；5 UT。
- **UNVERIFIED**：本轮 NPU e2e；全 CANN 版本兼容。

## 勿过度推广

- MiniMax-M3 范围；勿并入 #16545 MegaMoe 或 #16426 IndexScore。

## Retrieval queries

1. MiniMax-M3 CANN GBSA sparse attention Ascend
2. PR 16904 msa_index_score removal
3. generic_block_sparse_attention npu_msa_index_score adapter
