# Topic: A2 BNSD long cached-prefill（opt-in）

- **日期**: 2026-09-20
- **关联 case**: [`cases/pr-16012-a2-bnsd-long-cached-prefill.case.md`](../cases/pr-16012-a2-bnsd-long-cached-prefill.case.md)
- **PR**: https://github.com/vllm-project/vllm-ascend/pull/16012
- **Pins**: base `b255ab59…` · head `4f120cbe…` · OPEN；+271/−7，9 files

## 要点

- **VERIFIED**：`enable_prefill_bnsd` 配置面；attention_v1 / hardware_profile / docs / UT；刚 merge upstream conflict fix。
- **CLAIM**：A2-only mixed TND/BNSD；≥4096 单 cached-prefill；性能表 −13% 量级。
- **UNVERIFIED**：本轮 NPU；当前 head vs 原型 commit 性能。

## 勿过度推广

- 默认关闭；门控外路径回退 TND；勿外推非 A2。

## Retrieval queries

1. enable_prefill_bnsd A2 BNSD Ascend
2. PR 16012 long cached prefill attention
3. TND vs BNSD NHD paged KV
