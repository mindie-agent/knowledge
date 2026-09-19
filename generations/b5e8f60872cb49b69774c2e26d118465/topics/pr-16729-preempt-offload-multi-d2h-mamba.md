# Topic: Preempt offload multi-D2H（MRV2）+ mamba resume index

- **日期**: 2026-09-17
- **关联 case**: [`cases/pr-16729-preempt-offload-multi-d2h-mamba.case.md`](../cases/pr-16729-preempt-offload-multi-d2h-mamba.case.md)
- **PR**: https://github.com/vllm-project/vllm-ascend/pull/16729
- **Pins**: base `e1d14903…` · head `f7d285a7…` · OPEN BLOCKED

## 要点

- **VERIFIED**：3-file 小补丁（manager/worker + UT）。
- **CLAIM**：双次 `handle_preemptions` → 多 D2H；mamba align resume 精度。
- **UNVERIFIED**：本轮未复跑 CI/设备。

## Retrieval queries

1. MRV2 preempt offload duplicate D2H Ascend
2. mamba align resume index Qwen3.5 offload
3. PR 16729 kv_offload preempt_offload
