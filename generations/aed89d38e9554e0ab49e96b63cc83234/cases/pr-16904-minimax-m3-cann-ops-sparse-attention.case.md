# Case: MiniMax-M3 — route sparse attention to CANN Ops GBSA（remove in-tree MSA）

- **日期**: 2026-09-20（修订）；初建 2026-09-19
- **状态**: OPEN not-draft / MERGEABLE / **BLOCKED** / reference-only
- **来源 PR**: https://github.com/vllm-project/vllm-ascend/pull/16904 （author `HaoxinZong`；labels: `documentation`, `module:tests`, `module:core`, `ready-precise`）
- **Pinned（本轮 gh 2026-09-20）**: base `b255ab59662d8c665e5534e926f913b1c1179b1c` · head `e4374826c567515ca7292e3736cc1fae12c0eb29`

## 修订说明 / Revision notes（2026-09-20，Asia/Shanghai）

- **重 pin**：base `c8addbb24fe8…` → `b255ab59662d8c665e5534e926f913b1c1179b1c`；head `257739fc5293…` → `e4374826c567515ca7292e3736cc1fae12c0eb29`。
- tip commits（VERIFIED）：`7624d4db9a28…` refactor sparse attn；`a913bd10675b…` skip MsaIndexScore when CANN op unavailable；`e4374826c567…` skip MiniMax-M3 E2E without CANN index op。
- 规模约 +394/−43581，**162 files**；仍 MERGEABLE **BLOCKED**。
- 主题不变：删 in-tree MSA + 路由 CANN GBSA；测试侧增加 CANN 不可用时 skip（CLAIM）。
- 本轮未跑 NPU。
- 保留旧对比：https://github.com/vllm-project/vllm-ascend/compare/c8addbb24fe8fa820bb79685c116e43ba89b77e9...257739fc529385e0692f1809a8358112128ba499
- 本轮对比：https://github.com/vllm-project/vllm-ascend/compare/b255ab59662d8c665e5534e926f913b1c1179b1c...e4374826c567515ca7292e3736cc1fae12c0eb29

## 修订说明 / Revision notes（2026-09-19，Asia/Shanghai）

- 新建 case；规模 +361/−43568，**161 files**（大量删除 in-tree `msa_index_score` / `sparse_attention_score`）。
- MERGEABLE **BLOCKED**（VERIFIED GraphQL）。
- 文件面（VERIFIED sample）：`vllm_ascend/models/minimax_m3/ops/msa_m3_npu.py`、`vllm_ascend/platform.py`、`csrc/torch_binding*.cpp`、`csrc/aclnn_torch_adapter/msa_index_score_torch_adpt.h`、相关 UT/e2e；删除 `csrc/attention/msa_index_score/**` 与 `sparse_attention_score/**` 大量树。
- 作者意图 CLAIM：BF16/FP8 sparse attention 走 CANN Ops `generic_block_sparse_attention`；保留 `torch.ops._C_ascend.npu_msa_index_score` API 经薄 ACLNN adapter；A5 上 MiniMax-M3 dense GQA KV-cache 在全局 FP8 cache 时自动保持 BF16。
- 校验 CLAIM：compileall、5 targeted UT、对照 CANN 9.2 GBSA signatures；**未含** #16426 IndexScore 算法/性能与 Eagle3 quant workaround。
- 本轮未跑 NPU → 正确性 / 性能 **CLAIM**；overall **BLOCKED**。

## Trigger — 何时想起本 case

讨论 MiniMax-M3 **稀疏注意力**是否仍依赖仓库内 `msa_index_score` / `sparse_attention_score`，或已切到 CANN Ops Transformer GBSA 时。

## Preconditions / environment signals

- base `main` @ `b255ab59…`；head @ `e4374826…`。
- 依赖 CLAIM：兼容安装的 CANN Ops Transformer（`generic_block_sparse_attention`）；匹配 vLLM `84030bbe…`（PR body）。
- 平台改动 CLAIM：纳入 #16286；early init 用 `hf_config.architectures`。

## Observed failure pattern（若有）

- Refactor / deps 迁移；非单一 crash 报告。

## Fix direction（仅限本 PR，附条件）

1. 删除 in-tree sparse MSA 算子实现与 custom-op build 条目（VERIFIED 大规模 deletions）。
2. 经 ACLNN adapter 转发既有 `npu_msa_index_score` API 到 CANN Ops（CLAIM）。
3. A5 MiniMax-M3 mixed KV-cache 策略（dense GQA BF16 when global FP8）（CLAIM + UT CLAIM）。

**条件**: head `e4374826c567515ca7292e3736cc1fae12c0eb29`；OPEN blocked；NPU e2e **UNVERIFIED**。

## Do-not-overgeneralize

- 删除 in-tree ≠ 所有模型稀疏注意力路径已迁；范围锚定 MiniMax-M3 + GBSA。
- CANN 9.2 signature 对照 CLAIM ≠ 本轮 VERIFIED 全版本矩阵。
- 勿与 #16545 MegaMoe / #16426 IndexScore 混 pin。

## Evidence links

- PR: https://github.com/vllm-project/vllm-ascend/pull/16904
- Compare: https://github.com/vllm-project/vllm-ascend/compare/c8addbb24fe8fa820bb79685c116e43ba89b77e9...257739fc529385e0692f1809a8358112128ba499

## Retrieval queries

1. MiniMax-M3 CANN Ops generic_block_sparse_attention Ascend
2. PR 16904 remove msa_index_score sparse_attention_score
3. npu_msa_index_score ACLNN adapter GBSA BF16 FP8
4. A5 MiniMax-M3 mixed KV-cache BF16 dense GQA
5. vllm-ascend cann_ops_transformer sparse attention cleanup
