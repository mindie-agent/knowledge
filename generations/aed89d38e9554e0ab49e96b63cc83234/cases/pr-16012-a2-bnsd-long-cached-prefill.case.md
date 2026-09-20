# Case: A2 — opt-in BNSD path for long cached prefill

- **日期**: 2026-09-20
- **状态**: OPEN not-draft / MERGEABLE / reference-only（mergeState 未强制 BLOCKED 本轮；以 GraphQL MERGEABLE 为准）
- **来源 PR**: https://github.com/vllm-project/vllm-ascend/pull/16012 （author `Mlight7`；labels: `documentation`, `module:tests`, `module:core`）
- **Pinned（本轮 gh 2026-09-20）**: base `b255ab59662d8c665e5534e926f913b1c1179b1c` · head `4f120cbe1b32edf7d9008e68bd22bf19bc696e17`

## 修订说明 / Revision notes（2026-09-20，Asia/Shanghai）

- 新建 case（top-8 updated；Ascend A2 attention 高信号）；规模 +271/−7，**9 files**。
- tip commits（VERIFIED）：BNSD long cached prefills；docs；310P UT mock；`chore: merge upstream main and resolve attention conflict`（2026-09-20）。
- 文件面（VERIFIED）：`attention/attention_v1.py`、`ascend_config.py`（`enable_prefill_bnsd`）、`device/hardware_profile.py`、docs `bnsd_prefill.md`、UT。
- 设计 CLAIM：短 decode 保持 TND；**恰好一个** ≥4096 token 的 cached/chunked prefill 走 BNSD；保留 native paged **NHD** KV（不转 HND）；不支持硬件/shape/dtype/batch 时回退 TND。
- 门控 CLAIM：默认关闭；需 Atlas **A2**、BF16、head_dim=256、block_size=128；PCP/SWA/sinks/non-decoder 走旧路径。
- 性能表 CLAIM（Qwen3.8-27B A2 TP2，原型 commit `f4a08bddd` + CANN 9.0.1）：单算子 ~7680q×100k KV：TND 62.7ms → BNSD 54.5ms（约 −13%）；mixed batch 亦有表。
- 本轮未跑 NPU → 性能/正确性 **CLAIM**。
- 对比：https://github.com/vllm-project/vllm-ascend/compare/b255ab59662d8c665e5534e926f913b1c1179b1c...4f120cbe1b32edf7d9008e68bd22bf19bc696e17

## Trigger — 何时想起本 case

讨论 **A2 长 cached-prefill** 注意力布局（TND vs BNSD）、`enable_prefill_bnsd`、或 paged NHD KV 不转 HND 时。

## Preconditions / environment signals

- base `main` @ `b255ab59…`；head @ `4f120cbe…`。
- Opt-in；默认关闭；硬件/形状门控严格（CLAIM）。

## Observed failure pattern（若有）

- Performance feature；非单一 crash。

## Fix direction（仅限本 PR，附条件）

1. 选择性 mixed-layout：eligible long cached-prefill → BNSD query + NHD paged KV（CLAIM）。
2. Config flag + hardware_profile / UT / docs（VERIFIED 文件面）。

**条件**: head `4f120cbe1b32edf7d9008e68bd22bf19bc696e17`；OPEN；A2-only CLAIM；NPU **UNVERIFIED**。

## Do-not-overgeneralize

- A2 + 形状门控 ≠ A3/A5/310P 自动适用。
- 原型 benchmark commit/CANN 版本 ≠ 当前 head 已复测。
- 勿与 ACL Graph host-side（#16798）或 padding（#16157）混为同一修复。

## Evidence links

- PR: https://github.com/vllm-project/vllm-ascend/pull/16012
- Compare: https://github.com/vllm-project/vllm-ascend/compare/b255ab59662d8c665e5534e926f913b1c1179b1c...4f120cbe1b32edf7d9008e68bd22bf19bc696e17
- Docs path in PR: `docs/source/user_guide/feature_guide/bnsd_prefill.md`

## Retrieval queries

1. A2 BNSD long cached prefill enable_prefill_bnsd
2. PR 16012 TND NHD paged KV Ascend attention
3. Atlas A2 BF16 head_dim 256 block 128 BNSD
4. mixed-layout attention cached prefill vllm-ascend
5. Qwen3 BNSD vs TND latency claim Ascend A2
