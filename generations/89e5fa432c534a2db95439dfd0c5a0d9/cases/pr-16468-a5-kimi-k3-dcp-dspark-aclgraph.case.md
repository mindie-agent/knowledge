# Case: A5 Kimi K3 — MRv1/MRv2 DCP、DSpark ACLGraph、strided Flash MLA/GQA（draft）

- **日期**: 2026-09-18（MindIE acceptance 再修订）；初建 2026-09-15
- **状态**: OPEN **draft** / **CONFLICTING·DIRTY** / reference-only；超大 PR（+78628/−1695，415 files）
- **来源 PR**: https://github.com/vllm-project/vllm-ascend/pull/16468 （author `maoxx241`；labels: `documentation`, `module:tests`, `module:ops`, `module:core`, `module:quantization`, `merge-conflicts`）
- **Pinned（本轮 gh 2026-09-18 MindIE acceptance）**: base `cb82154ea226c733ba99fe76d7ee3bd3af137ed1` · head `3c94ce740126984b9683501dd8c3772bf88d4311`

## 修订说明 / Revision notes（2026-09-18 MindIE acceptance，Asia/Shanghai）

- **重 pin**：base `00b0b979…` → `cb82154ea226c733ba99fe76d7ee3bd3af137ed1`（base 相对上轮 ahead_by=7 VERIFIED）；head `d8a34b81…` → `3c94ce740126984b9683501dd8c3772bf88d4311`（compare prior-head…live-head：diverged ahead1/behind1；tip commit CLAIM 同题 `feat(attention): integrate A5 K3 graphs and operator optimizations`）。
- 规模：+78628/−1695，**415 files**（上轮 +78461/−1694，411 files）；仍 **draft + DIRTY/CONFLICTING**。
- 验收 / 性能仍 **CLAIM**；experimental not-to-merge **CLAIM**；本轮未跑 NPU。
- 保留旧对比：https://github.com/vllm-project/vllm-ascend/compare/00b0b979fc605fbdedafa83d85d02c58c42ab8f3...d8a34b8161d27992a7c2ad439673f2d3e2073c35
- 本轮对比：https://github.com/vllm-project/vllm-ascend/compare/cb82154ea226c733ba99fe76d7ee3bd3af137ed1...3c94ce740126984b9683501dd8c3772bf88d4311

## 修订说明 / Revision notes（2026-09-18，Asia/Shanghai — 日更）

- **重 pin**：base `10b4fb2a…` → `00b0b979fc605fbdedafa83d85d02c58c42ab8f3`；head `48c8a870…` → `d8a34b8161d27992a7c2ad439673f2d3e2073c35`（single feature commit CLAIM：`feat(attention): integrate A5 K3 graphs and operator optimizations`）。
- 规模再膨胀：+78461/−1694，**411 files**（上轮 +72.6k/−1330，350 files）；仍 **draft + DIRTY** + `merge-conflicts`。
- 正文仍 CLAIM：集成基线 Ascend parent `990243c4…`；匹配 vLLM `84030bbe…`；A5 / CANN 9.2.0B035（B050 for PP4 MegaMoe case CLAIM）。
- 保留旧对比：https://github.com/vllm-project/vllm-ascend/compare/10b4fb2a99c4fe8f33efb591b1f2996265430ce3...48c8a870d6b376b1ff854d636a2babe543cb395b
- 新对比：https://github.com/vllm-project/vllm-ascend/compare/00b0b979fc605fbdedafa83d85d02c58c42ab8f3...d8a34b8161d27992a7c2ad439673f2d3e2073c35
- 验收 / 性能仍 **CLAIM**；experimental not-to-merge **CLAIM**；勿与 #16157 混 pin。


## 修订说明 / Revision notes（2026-09-17，Asia/Shanghai）

- **重 pin**：base `c267db73…` → `10b4fb2a99c4fe8f33efb591b1f2996265430ce3`；head `4e519623…` → `48c8a870d6b376b1ff854d636a2babe543cb395b`（single feature commit CLAIM；body 自述）。
- 规模再膨胀：+72637/−1330，**350 files**（上轮 +66k/−337，292 files）；仍 **draft + DIRTY**。
- 集成基线 CLAIM：Ascend parent `990243c4c5b0304248b8ccf01213849e957e1301`；匹配 vLLM `84030bbe3d74d99bad477a3d2e37a973ccd8865c`；环境 A5 / CANN 9.2.0B035。
- 保留旧对比：https://github.com/vllm-project/vllm-ascend/compare/c267db731d03e18042047ea1594e033673e46a6f...4e519623bbd12a3daf1a9555ce63b1d44e9ce867
- 新对比：https://github.com/vllm-project/vllm-ascend/compare/10b4fb2a99c4fe8f33efb591b1f2996265430ce3...48c8a870d6b376b1ff854d636a2babe543cb395b
- 验收 / 性能仍 **CLAIM**；experimental not-to-merge **CLAIM**；勿与 #16157 混 pin。

## 修订说明 / Revision notes（2026-09-16，Asia/Shanghai）

- **重 pin**：base `26f1363f…` → `c267db73…`；head `8a293f09…` → `4e519623…`（single feature commit CLAIM；rebased onto Ascend main `990243c` CLAIM）。
- mergeable **CONFLICTING** / mergeStateStatus **DIRTY**；label `merge-conflicts`。
- 规模膨胀：+66264/−337，**292 files**（上轮 +53270/−207，220 files）。
- 正文增量 CLAIM：DCP8 small-decode 优化、MRv2 policy3、EPLB verification on `64e6ab95`（另记）；整合 #16318/#16347/#16417 与 device DCP metadata 相关 #16304/#16546（CLAIM 关系）。
- vLLM pin 仍 CLAIM：`84030bbe3d74d99bad477a3d2e37a973ccd8865c`。
- 保留旧对比：https://github.com/vllm-project/vllm-ascend/compare/26f1363f7180dfcbeac1230679976cede6010fc8...8a293f09830a5c55bd4fc5c2e2219f0edcd59443
- 新对比：https://github.com/vllm-project/vllm-ascend/compare/c267db731d03e18042047ea1594e033673e46a6f...4e519623bbd12a3daf1a9555ce63b1d44e9ce867
- 验收矩阵 / 性能 → 仍 **CLAIM**；本轮未复跑 NPU。
- **勿**与 #16157 rc0.26 混 pin。

## 修订说明 / Revision notes（2026-09-15，Asia/Shanghai）

- 新建 case；**draft** 全程标注；验收数字与拓扑来自 PR body/评论 → **CLAIM**（本轮未复跑 NPU）。
- 与 #16157（K3 DSpark ACL Graph padding/Query-T on `releases/v0.26.0rc`）主题相关但 **分案**：本 PR 面向 main / A5 Flash MLA·GQA·DCP 扩展。
- CI：ci-gate/pre-commit **fail**；CI 非 acceptance gate（PR 自述 CLAIM）。
- B035 AsStrided 100-layer backing-size 边界与 `gpu_memory_utilization=0.88` workaround → **CLAIM**，非根因修复。

## Trigger — 何时想起本 case

讨论 **Kimi K3 on Ascend A5** 时：

- MRv1/MRv2 + **DCP**（含 draft 复制 KV / DCP1 vs target DCP2）；
- DSpark **ACLGraph** replay（target+draft）；
- strided **Flash MLA / GQA**；PD 跨节点 KV；
- 与 rc0.26 `#16157` padding 契约的差异（勿混 pin）。

## Preconditions / environment signals

- base `main` @ `10b4fb2a…`；head @ `48c8a870…`。
- 体量：csrc `a5_mla_common` / `flash_mla_with_kvcache` / MoE `causal_conv1d_v2` + Python attention/spec_decode/worker 大范围改动（VERIFIED 文件列表）。
- vLLM pin（PR body）：`84030bbe3d74d99bad477a3d2e37a973ccd8865c`（CLAIM）。

## Observed failure pattern（若有）

- Feature/RFC 落地；已知 CLAIM：B035 AsStrided 窄边界失败 → 调低 memory util 规避。

## Fix direction（仅限本 PR，附条件 — 高层）

1. A5 Flash MLA/GQA 内核与 tiling 路径（大量 `csrc/attention/a5_mla_common`、`flash_mla_with_kvcache`）。
2. DSpark proposer / graph contract + MRv2 speculator/device metadata（Python）。
3. DCP 配置：GQA draft 可 DCP1 + replicated KV；target DCP2（CLAIM 验收矩阵）。
4. PD：跨节点 KV transfer + full-graph replay 叙述（CLAIM）。
5. CPU UTs：graph contract、DCP slots、replicated KV、Flash cache（VERIFIED 存在测试文件）。

**条件**: head `48c8a870d6b376b1ff854d636a2babe543cb395b`；**OPEN draft** + DIRTY；experimental / not-intended-to-merge（PR CLAIM）。

## Do-not-overgeneralize

- Draft / 超大 diff → 禁止当已合入行为。
- 验收表（GPQA/GSM8K/acceptance%）为作者环境 CLAIM，非本轮 VERIFIED。
- 禁止与 #16157 rc 分支 pin 混用。
- memory-util workaround ≠ 通用 >16GiB 修复。

## Evidence links

- PR: https://github.com/vllm-project/vllm-ascend/pull/16468
- Compare: https://github.com/vllm-project/vllm-ascend/compare/26f1363f7180dfcbeac1230679976cede6010fc8...8a293f09830a5c55bd4fc5c2e2219f0edcd59443
- Related padding case #16157: https://github.com/vllm-project/vllm-ascend/pull/16157
- Launch/results comment (PR): https://github.com/vllm-project/vllm-ascend/pull/16468#issuecomment-5666315412

## Retrieval queries

1. A5 Kimi K3 Flash MLA GQA DCP DSpark ACLGraph MRv1 MRv2
2. PR 16468 draft head 8a293f0 feat/rfc16464-a5-kimi-k3-flash-mla
3. replicated draft KV DCP1 target DCP2 DP4 TP8 acceptance
4. B035 AsStrided backing-size gpu_memory_utilization 0.88 workaround
5. a5_mla_common flash_mla_with_kvcache causal_conv1d_v2
