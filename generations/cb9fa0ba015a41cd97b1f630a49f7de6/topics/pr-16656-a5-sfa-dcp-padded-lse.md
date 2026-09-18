# Topic: A5 SFA DCP padded-index LSE / empty shards

- **日期**: 2026-09-18（MindIE acceptance 再修订）
- **关联 case**: [`cases/pr-16656-a5-sfa-dcp-padded-lse.case.md`](../cases/pr-16656-a5-sfa-dcp-padded-lse.case.md)
- **PR**: https://github.com/vllm-project/vllm-ascend/pull/16656
- **Pins**: base `c7ca0b67…` · head `13816345…` · OPEN BLOCKED

## 要点

- **VERIFIED**：9-file kernel/host/UT 面；+145/−7；head 相对初建 tip +2 commits；能力枚举改为 `SFA_C8_DCP_REPLICATED_INDEXER`；仅 `enable_sparse_sfa_c8` 时 gate；A5 profile 不含该 C8 能力；cpu-ut/pre-commit pass sample。
- **CLAIM**：A5 支持 non-C8 SFA DCP；C8+DCP+replicated indexer 未支持；padded LSE / empty shards 数值修复；非宣称 C8 可用。
- **UNVERIFIED**：全模型数值；本轮未跑 NPU；多数 selected e2e pending。

## 勿过度推广

- 关闭 C8 / gate C8 ≠ 启用 C8；勿合并 #16325 pin；旧名 `SFA_DCP_REPLICATED_INDEXER` 已 supersede。

## Retrieval queries

1. A5 SFA DCP padded LSE empty local keys
2. PR 16656 sparse flash attention DCP merge
3. SFA_C8_DCP_REPLICATED_INDEXER enable_sparse_sfa_c8 Ascend A5
