# Topic: MiniMax-M3 CANN MegaMoe（enable_fused_mc2=2）

- **日期**: 2026-09-20（新建；PR MERGED 2026-09-19）
- **关联 case**: [`cases/pr-16545-minimax-m3-cann-megamoe.case.md`](../cases/pr-16545-minimax-m3-cann-megamoe.case.md)
- **PR**: https://github.com/vllm-project/vllm-ascend/pull/16545
- **Pins**: base `c8addbb2…` · head `a7c78d8e…` · **MERGED** `9ad52992…`；+577/−17，13 files

## 要点

- **VERIFIED**：已合入；mergeCommit `9ad52992…`（亦为 #16468 当前 base）；文件面 fused_moe / ascend_config / UT。
- **CLAIM**：`enable_fused_mc2=2`；SwiGLU-OAI multi-API；950 TP4×DP2 与 −22.6% latency。
- **UNVERIFIED**：本轮 NPU；跨代际外推。

## 勿过度推广

- 勿与 #16904 GBSA / #16634 W4A8 混 pin。

## Retrieval queries

1. MiniMax-M3 MegaMoe enable_fused_mc2 Ascend
2. PR 16545 cann_ops_transformer fused_mc2
3. merge 9ad52992 MegaMoe
