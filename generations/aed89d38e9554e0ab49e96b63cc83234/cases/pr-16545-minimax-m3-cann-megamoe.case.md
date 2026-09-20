# Case: MiniMax-M3 — CANN MegaMoe（enable_fused_mc2=2）

- **日期**: 2026-09-20（新建；PR 已 MERGED 2026-09-19）
- **状态**: **MERGED** / reference-only；mergeCommit `9ad52992c9682f4f640048eb0753f5626e475d50`
- **来源 PR**: https://github.com/vllm-project/vllm-ascend/pull/16545 （author `HaoxinZong`；labels: `module:tests`, `module:ops`, `module:core`, `module:quantization`, `ready-a5`, `ready-precise`）
- **Pinned（本轮 gh 2026-09-20）**: base `c8addbb24fe8fa820bb79685c116e43ba89b77e9` · head `a7c78d8ed72425f4f71a21fef56d333b48cd0b18` · **mergeCommit** `9ad52992c9682f4f640048eb0753f5626e475d50`

## 修订说明 / Revision notes（2026-09-20，Asia/Shanghai）

- 此前 **budget-deferred** 高信号；本轮扫到已 **MERGED**（`mergedAt` 2026-09-19T10:21:47Z VERIFIED）→ 新建 case+topic（不 recreate 旧 draft）。
- 规模 +577/−17，**13 files**（VERIFIED）。
- 文件面（VERIFIED sample）：`ascend_config.py`、`ops/fused_moe/moe_utils.py`、`moe_comm_method.py`、quant W8A8、UT（moe/quant/ascend_config）。
- tip commits（VERIFIED sample）：MegaMoe 主干 + activation binding harden + Ascend 950 MXFP8 + generic wrappers / lazy capability checks。
- 作者意图 CLAIM：MiniMax-M3 经 `additional_config.enable_fused_mc2=2` 选 CANN MegaMoe；拒绝 `dispatch_ffn_combine`；兼容多种 SwiGLU-OAI wrapper API；无法表示时显式失败而非静默降精度。
- 校验 CLAIM：ruff/compileall；Ascend 950 TP4×DP2 EP 启动 + chat；device profiling「token latency vs baseline 约 −22.6%」（CLAIM）。
- **合并 ≠ 本轮复验**：未跑 NPU。
- 对比：https://github.com/vllm-project/vllm-ascend/compare/c8addbb24fe8fa820bb79685c116e43ba89b77e9...a7c78d8ed72425f4f71a21fef56d333b48cd0b18
- 注：#16468 当前 base 与本 mergeCommit **同 SHA**（VERIFIED）→ draft 已 rebase 到含 MegaMoe 的 main tip。

## Trigger — 何时想起本 case

讨论 MiniMax-M3 **MegaMoe / fused MC2**、`enable_fused_mc2=2`、或 CANN SwiGLU-OAI activation binding 时。

## Preconditions / environment signals

- base `main` @ `c8addbb2…`；head @ `a7c78d8e…`；mergeCommit `9ad52992…`。
- 用户面 CLAIM：需兼容 `cann_ops_transformer`；配置 `enable_fused_mc2=2`。
- 与 #16904（sparse GBSA）/ #16634（A5 MegaMoe W4A8）相邻但 **分案**。

## Observed failure pattern（若有）

- Feature port + API harden；正文提及静默降精度风险（CLAIM）。

## Fix direction（仅限本 PR，附条件）

1. MiniMax-M3 MegaMoe 路径 + 拒绝 unsupported dispatch mode（CLAIM）。
2. 多形态 activation binding + schema fallback / explicit fail（CLAIM）。
3. MXFP8 / wrapper 兼容与 UT（VERIFIED 文件面）。

**条件**: mergeCommit `9ad52992c9682f4f640048eb0753f5626e475d50`；**MERGED**；NPU **UNVERIFIED** this run。

## Do-not-overgeneralize

- MERGED ≠ 全卡型/全精度已验证；950 CLAIM ≠ A2/A3 外推。
- 勿与 #16904 sparse attn 或 #16634 W4A8 fused 混 pin。
- `enable_fused_mc2=2` 语义以 PR/配置文档为准，勿 invent 其他取值。

## Evidence links

- PR: https://github.com/vllm-project/vllm-ascend/pull/16545
- Compare: https://github.com/vllm-project/vllm-ascend/compare/c8addbb24fe8fa820bb79685c116e43ba89b77e9...a7c78d8ed72425f4f71a21fef56d333b48cd0b18

## Retrieval queries

1. MiniMax-M3 CANN MegaMoe enable_fused_mc2=2 Ascend
2. PR 16545 SwiGLU-OAI activation binding
3. Ascend 950 MXFP8 MegaMoe fused_mc2
4. vllm-ascend MegaMoe wrapper API fail-closed
5. mergeCommit 9ad52992 MiniMax MegaMoe
