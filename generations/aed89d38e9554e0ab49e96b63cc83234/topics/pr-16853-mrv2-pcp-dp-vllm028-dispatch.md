# Topic: MRV2 PCP+DP dispatch adapt for vLLM 0.28.0

- **日期**: 2026-09-20（MERGED）；初建 2026-09-19
- **关联 case**: [`cases/pr-16853-mrv2-pcp-dp-vllm028-dispatch.case.md`](../cases/pr-16853-mrv2-pcp-dp-vllm028-dispatch.case.md)
- **PR**: https://github.com/vllm-project/vllm-ascend/pull/16853
- **Pins**: base `628fac6d…` · head `fb0554b7…` · **MERGED** `8f2e3fed…`；+337/−9，7 files

## 要点

- **2026-09-20 VERIFIED**：GraphQL 确认 **MERGED**；mergeCommit `8f2e3fed7332…`；head 未变；本轮未跑 NPU。

- **VERIFIED**：7-file MRV2 worker/patch/UT 面；MERGEABLE BLOCKED。
- **CLAIM**：0.28.0 限定 config+dispatch token-count 修复；DCP=1；显式 MRV2；121 UT 先验。
- **UNVERIFIED**：本轮 NPU；其他 vLLM 版本。

## 勿过度推广

- 勿与 #16848 default-MRV2 混；范围不含 DCP>1。

## Retrieval queries

1. MRV2 PCP DP vLLM 0.28 dispatch Ascend
2. PR 16853 pcp_manager_v2 token count
3. DPMetadata.make local vs global PCP
