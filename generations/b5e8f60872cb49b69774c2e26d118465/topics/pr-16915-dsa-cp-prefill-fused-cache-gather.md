# Topic: DSA-CP prefill overhead — fused gather / sharded O-proj / block-copy

- **日期**: 2026-09-19
- **关联 case**: [`cases/pr-16915-dsa-cp-prefill-fused-cache-gather.case.md`](../cases/pr-16915-dsa-cp-prefill-fused-cache-gather.case.md)
- **PR**: https://github.com/vllm-project/vllm-ascend/pull/16915
- **Pins**: base `c8addbb2…` · head `383fbad9…` · OPEN BLOCKED；+1071/−24，15 files

## 要点

- **VERIFIED**：15-file attention/CP/ops 面；MERGEABLE BLOCKED。
- **CLAIM**：三向 prefill 成本削减；A3 9 NPU tests；CPU 5457 pass；无新用户配置。
- **UNVERIFIED**：本轮复跑；A5/其他拓扑收益。

## 勿过度推广

- 勿与 #16656 padded LSE 混；阈值/capability gate 为路径条件。

## Retrieval queries

1. DSA-CP prefill fused gather block-copy Ascend
2. PR 16915 sfa_cp cache_store indexer
3. A3 DSA-CP reduce-scatter O-proj sharded
