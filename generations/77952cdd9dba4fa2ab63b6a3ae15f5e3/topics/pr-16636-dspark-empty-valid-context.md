# [参考] DSpark empty valid context after discard（PR #16636）

- **日期**: 2026-09-16
- **状态**: OPEN / blocked / reference-only；多项 selected checks pass
- **阅读方式**: `gh` + PR body/files（未跑本地 NPU）

## Pinned（本轮）

| 字段 | 值 |
|---|---|
| PR | https://github.com/vllm-project/vllm-ascend/pull/16636 |
| 标题 | `[BugFix][Kernel] Handle empty DSpark context after discard` |
| state | OPEN；MERGEABLE；BLOCKED |
| base / head | `c267db731d03e18042047ea1594e033673e46a6f` / `1889ebeee16cb3855245b804b95f4b2a43eb8176` |
| 规模 | +105 / −1，**2 files** |
| 对比 | https://github.com/vllm-project/vllm-ascend/compare/c267db731d03e18042047ea1594e033673e46a6f...1889ebeee16cb3855245b804b95f4b2a43eb8176 |

### CI（摘要）

| Check | 结论 |
|---|---|
| DCO / ci-gate / pre-commit / cpu-ut | **pass** |
| selected 310p / a2 / a3 parts（抽样） | **pass** |
| mergeStateStatus | **BLOCKED**（整体原因 UNVERIFIED） |

## Themes

1. Discarded async spec → empty valid DSpark context
2. Safe anchor = request first position
3. Triton `spec_decode/utils` + nightly UT
4. IndexCheck / RoPE illegal position 关联（CLAIM）

## Retrieval queries

1. empty DSpark context discard IndexCheck 16636
2. valid_ctx_end equals ctx_start target_positions
3. safe anchor first position query positions
4. test_copy_and_expand_dflash_dspark regression
5. head 1889ebee blocked despite e2e pass
