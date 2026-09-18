# Case: A5 SFA DCP — padded-index LSE + empty local shards

- **日期**: 2026-09-18（MindIE acceptance 再修订）
- **状态**: OPEN not-draft / MERGEABLE / **BLOCKED** / reference-only
- **来源 PR**: https://github.com/vllm-project/vllm-ascend/pull/16656 （author `recky-c`；labels: `module:tests`, `module:core`, `ready-a5`, `ready-precise`）
- **Pinned（本轮 gh 2026-09-18 MindIE acceptance）**: base `c7ca0b676b9668535f467b78cff1274a4ddb63b2` · head `138163458f134fba039ab49e28ebe75e9116c32e`

## 修订说明 / Revision notes（2026-09-18 MindIE acceptance，Asia/Shanghai）

- **重 pin**：base 未变 `c7ca0b676b9668535f467b78cff1274a4ddb63b2`；head `60c6c32a…` → `138163458f134fba039ab49e28ebe75e9116c32e`（+2 commits VERIFIED）。
- 新 commits（VERIFIED `gh`）：`ace1c407…` `fix: gate SFA C8 DCP by operator capability`；`13816345…` `test: limit SFA C8 validation cases to SFA cache setting`。
- 规模：+145/−7，**9 files**（上轮 +112/−2，7 files）。
- **VERIFIED 控制流变更**（compare old-head…new-head）：
  - `HardwareCapability.SFA_DCP_REPLICATED_INDEXER` **重命名**为 `SFA_C8_DCP_REPLICATED_INDEXER`。
  - A5 `HardwareProfile.capabilities` **不再**包含该 C8 能力（blob @ head VERIFIED）。
  - `_validate_parallel_config` 仅在 `additional_config.enable_sparse_sfa_c8 == True` 且 profile 不支持 `SFA_C8_DCP_REPLICATED_INDEXER` 时 raise `NotImplementedError`（提示关闭 C8 以使用 non-C8 SFA DCP）。
  - 新增 UT `test_sfa_dcp_c8_hardware_validation`：A5 + `enable_sfa_c8` 期望 NotImplementedError。
- **CLAIM**（PR body / 注释）：A5 支持 non-C8 SFA DCP；SFA C8 + replicated indexer DCP 尚未支持；全模型目标仍关闭 SFA C8 / indexer C8。
- **UNVERIFIED**：本轮未跑 NPU；数值 / e2e selected 多数仍 pending → overall **BLOCKED**。
- CI sample（VERIFIED）：pre-commit / main / PR create / cpu-ut / DCO / select-tests **pass**；多数 a2/a3/a5 selected **pending**。
- 保留旧 pin 对比：https://github.com/vllm-project/vllm-ascend/compare/c7ca0b676b9668535f467b78cff1274a4ddb63b2...60c6c32ab18de4b8b97f643bfb6a568290bd373c
- 本轮对比：https://github.com/vllm-project/vllm-ascend/compare/c7ca0b676b9668535f467b78cff1274a4ddb63b2...138163458f134fba039ab49e28ebe75e9116c32e
- head-delta：https://github.com/vllm-project/vllm-ascend/compare/60c6c32ab18de4b8b97f643bfb6a568290bd373c...138163458f134fba039ab49e28ebe75e9116c32e

## 修订说明 / Revision notes（2026-09-18，Asia/Shanghai — 初建当日）

- 新建 case；单 commit `fix: handle padded and empty A5 SFA DCP shards`（`60c6c32a…`）。
- 规模 +112/−2，**7 files**；MERGEABLE **BLOCKED**。
- 文件面（VERIFIED）：`csrc/.../sparse_flash_attention_*_mla.h`、`attention/context_parallel/sfa_cp.py`、`device/hardware_profile.py`、相关 UT/e2e ops test。
- 根因 CLAIM：A5 SFA DCP decode 稀疏 index 前缀有效、尾部 `-1` padding；用 KV length 作 softmax 范围会污染 per-rank 归一化。
- 能力 CLAIM（**已被本轮 acceptance 修订 supersede**）：曾 advertise `SFA_DCP_REPLICATED_INDEXER` on A5；**不**声称 C8；目标 GLM5.2 W4A8、SFA C8 与 indexer C8 **均关闭**、TP8/DCP8/EP、eager、无 MTP/PD（body CLAIM）。
- CI 摘要（VERIFIED sample）：pre-commit/main/PR create/cpu-ut **SUCCESS**；a5 selected tests 仍进行中 → overall **BLOCKED**。

## Trigger — 何时想起本 case

讨论 **A5 Sparse Flash Attention + DCP**、padded sparse indices、LSE/softmax 合并错误、或 empty local key shards 时。

## Preconditions / environment signals

- base `main` @ `c7ca0b67…`；head @ `13816345…`（prior head `60c6c32a…` 保留）。
- 依赖关系 CLAIM：全模型实验曾叠加 #16325 indexer metadata（tested rev `ec6e301d…`）——**本 PR 不含**那些改动。
- 现有 custom op 已接受 PA_BSND + `return_softmax_lse` → 无需 host-tiling 变更（CLAIM）。
- C8 gate：仅 `enable_sparse_sfa_c8` 路径检查 `SFA_C8_DCP_REPLICATED_INDEXER`（VERIFIED）；A5 profile 不含该能力（VERIFIED blob）。

## Observed failure pattern（若有）

- Incorrect per-rank normalization / LSE when sparse index has `-1` pad beyond valid prefix；empty local selection with nonempty cache（CLAIM）。

## Fix direction（仅限本 PR，附条件）

1. Bound sparse-mode-0 LSE by actual valid index prefix。
2. Zero-init skipped queries（output + softmax max/sum；LSE → −inf 重建 CLAIM）。
3. Init `n2Size` before output init；prefill KV/RoPE views contiguous after packed DCP gather。
4. （初建 CLAIM；**acceptance 修订**）能力枚举改为 `SFA_C8_DCP_REPLICATED_INDEXER`；**仅**在 `enable_sparse_sfa_c8` 时 gate；A5 不 advertise 该 C8 能力 → C8+DCP 早拒，non-C8 SFA DCP 可走（VERIFIED 控制流 + CLAIM 注释）。

**条件**: head `138163458f134fba039ab49e28ebe75e9116c32e`；OPEN blocked；C8 DCP+replicated indexer **仍非**已支持范围。

## Do-not-overgeneralize

- A5 + 特定关闭 C8 配置 ≠ 全 SFA/C8/PD 路径已修。
- 勿把 #16325 或其他 indexer PR 的 pin 并入本 case。
- 数值修复 CLAIM ≠ 本轮 VERIFIED NPU。

## Evidence links

- PR: https://github.com/vllm-project/vllm-ascend/pull/16656
- Compare（本轮）: https://github.com/vllm-project/vllm-ascend/compare/c7ca0b676b9668535f467b78cff1274a4ddb63b2...138163458f134fba039ab49e28ebe75e9116c32e
- Compare（初建 tip 永久）: https://github.com/vllm-project/vllm-ascend/compare/c7ca0b676b9668535f467b78cff1274a4ddb63b2...60c6c32ab18de4b8b97f643bfb6a568290bd373c
- Head-delta: https://github.com/vllm-project/vllm-ascend/compare/60c6c32ab18de4b8b97f643bfb6a568290bd373c...138163458f134fba039ab49e28ebe75e9116c32e
- Blob platform gate: https://github.com/vllm-project/vllm-ascend/blob/138163458f134fba039ab49e28ebe75e9116c32e/vllm_ascend/platform.py
- Blob hardware_profile: https://github.com/vllm-project/vllm-ascend/blob/138163458f134fba039ab49e28ebe75e9116c32e/vllm_ascend/device/hardware_profile.py

## Retrieval queries

1. A5 SFA DCP padded index LSE empty shards Ascend
2. PR 16656 sparse flash attention softmax valid prefix
3. SFA_C8_DCP_REPLICATED_INDEXER enable_sparse_sfa_c8 gate A5
4. GLM5.2 W4A8 TP8 DCP8 SFA C8 disabled eager
5. sparse_flash_attention_kernel_mla padded -1 indices
