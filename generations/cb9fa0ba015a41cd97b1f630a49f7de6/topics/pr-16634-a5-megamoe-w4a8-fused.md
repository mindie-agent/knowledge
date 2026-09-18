# [参考] A5 MegaMoE W4A8 fused MC2 backend（PR #16634）

- **日期**: 2026-09-16
- **状态**: OPEN / CLEAN / reference-only；NPU runtime UNVERIFIED
- **阅读方式**: `gh` 元数据 + PR body/files（未跑 NPU）

## Pinned（本轮）

| 字段 | 值 |
|---|---|
| PR | https://github.com/vllm-project/vllm-ascend/pull/16634 |
| 标题 | `[Feat][MoE] Add A5 MegaMoE W4A8 fused backend` |
| state | OPEN；MERGEABLE；**CLEAN** |
| base / head | `11ee45653b199a097805b87011824a81ffa51b95` / `3d596b8fcf99b4591ea94f4e558f7caef809c137` |
| 规模 | +789 / −58，**15 files** |
| 对比 | https://github.com/vllm-project/vllm-ascend/compare/11ee45653b199a097805b87011824a81ffa51b95...3d596b8fcf99b4591ea94f4e558f7caef809c137 |

### CI（摘要）

| Check | 结论 |
|---|---|
| DCO / PR create / ReadTheDocs | **pass** |
| NPU runtime | **UNVERIFIED**（作者环境缺 torch/vLLM/Ascend tools） |

## Themes

1. MegaMoE backend + `FUSED_MC2` 路由（A5 W4A8 MXFP）
2. Dedicated HCCL communicator / symmetric buffers
3. DP active/dummy 同路径选择
4. Fallbacks：MC2 / all-gather / all-to-all
5. 与 #14449 的 clean-rebuild 边界（无 tests / 无 W8A8·W4A4）

## Retrieval queries

1. MegaMoE W4A8 FUSED_MC2 Ascend A5 16634
2. fused_moe mega_moe HCCL symmetric buffer
3. w4a8_mxfp4 moe_comm_method prepare_finalize
4. enable_fused_mc2 additional_config constraints
5. head 3d596b8f CLEAN NPU unverified
