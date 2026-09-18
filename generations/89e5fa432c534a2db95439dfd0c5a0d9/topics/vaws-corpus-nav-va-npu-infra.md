# VAWS corpus 导航：vllm-ascend / NPU / 推理基础设施相关笔记

- **日期**: 2026-09-13
- **状态**: draft / reference-only
- **Corpus repo**: https://github.com/vllm-ascend-workspace/vaws-knowledge
- **Pinned commit SHA（实际浅克隆读取）**: `e16d87287f7db51ca96efc945a9514e4146131d6`
- **Fetch UTC**: 2026-09-13T06:26:42Z
- **Default branch**: `main`（public）

## 修订说明 / Revision notes（2026-09-13）

- 本文件为本轮新建。
- 语料以 CANN `platform_config` 峰值快照为主；**未发现** 针对 ACL Graph / HCCL / vllm-ascend runtime / DSpark / PR #16157 的专题笔记。
- 已排除：VAWS 自初始化、MCP/stdio 契约、contribution/distribution 示例、容器与 packaging 测试代码。

## Coverage summary

| 主题 | 语料内情况 |
|---|---|
| vllm-ascend 运行时 / PR 级行为 | **无关联笔记** |
| ACL Graph / cudagraph / DSpark | **无** |
| HCCL / 集合通信 | **无** |
| NPU SoC 理论峰值 / MFU 分母 | 大量 `corpus/references/*-platform-config-peaks.md` |
| NPU 实测 sustained matmul | 至少 1 篇 910B4 microbench |

**与 PR #16157 的关系**: 所选 6 篇均为硬件/平台常数或微基准；**结论：与 ACL Graph padding / K3 gating 无直接关联**（no association）。不可用峰值笔记替代 PR 控制流证据。

## Selected notes（≤6）

### 1. Ascend910B4 single-card matmul observations

- Path: `corpus/references/ascend910b4-single-card-dense-matmul-sustained-2026-06-03.md`
- Permalink: https://github.com/vllm-ascend-workspace/vaws-knowledge/blob/e16d87287f7db51ca96efc945a9514e4146131d6/corpus/references/ascend910b4-single-card-dense-matmul-sustained-2026-06-03.md
- Recorded (in-note): 2026-09-08
- Why selected: NPU microbenchmark / sustained matmul fractions; explicitly scopes vllm/vllm-ascend off timed path; inference roofline infra.
- Association to PR #16157: **none** (platform/microbench only)

### 2. Ascend910B4 CANN 9.0.0 platform constants

- Path: `corpus/references/ascend910b4-cann-9.0.0-platform-config-peaks.md`
- Permalink: https://github.com/vllm-ascend-workspace/vaws-knowledge/blob/e16d87287f7db51ca96efc945a9514e4146131d6/corpus/references/ascend910b4-cann-9.0.0-platform-config-peaks.md
- Recorded (in-note): 2026-09-08
- Why selected: Theoretical peaks for a common vllm-ascend 910B-class SoC; MFU denominator; NPU platform infra.
- Association to PR #16157: **none** (platform/microbench only)

### 3. Ascend910B3 CANN 9.0.0 platform constants

- Path: `corpus/references/ascend910b3-cann-9.0.0-platform-config-peaks.md`
- Permalink: https://github.com/vllm-ascend-workspace/vaws-knowledge/blob/e16d87287f7db51ca96efc945a9514e4146131d6/corpus/references/ascend910b3-cann-9.0.0-platform-config-peaks.md
- Recorded (in-note): 2026-09-08
- Why selected: Same family platform peaks for Ascend910B3 (do not equate to B4).
- Association to PR #16157: **none** (platform/microbench only)

### 4. Ascend910B2 CANN 9.0.0 platform constants

- Path: `corpus/references/ascend910b2-cann-9.0.0-platform-config-peaks.md`
- Permalink: https://github.com/vllm-ascend-workspace/vaws-knowledge/blob/e16d87287f7db51ca96efc945a9514e4146131d6/corpus/references/ascend910b2-cann-9.0.0-platform-config-peaks.md
- Recorded (in-note): 2026-09-08
- Why selected: 910B2 platform peaks; NPU infra baseline.
- Association to PR #16157: **none** (platform/microbench only)

### 5. Ascend910B1 CANN 9.0.0 platform constants

- Path: `corpus/references/ascend910b1-cann-9.0.0-platform-config-peaks.md`
- Permalink: https://github.com/vllm-ascend-workspace/vaws-knowledge/blob/e16d87287f7db51ca96efc945a9514e4146131d6/corpus/references/ascend910b1-cann-9.0.0-platform-config-peaks.md
- Recorded (in-note): 2026-09-08
- Why selected: 910B1 platform peaks; keep SKU-specific.
- Association to PR #16157: **none** (platform/microbench only)

### 6. Ascend910B CANN 9.0.0 platform constants

- Path: `corpus/references/ascend910b-cann-9.0.0-platform-config-peaks.md`
- Permalink: https://github.com/vllm-ascend-workspace/vaws-knowledge/blob/e16d87287f7db51ca96efc945a9514e4146131d6/corpus/references/ascend910b-cann-9.0.0-platform-config-peaks.md
- Recorded (in-note): 2026-09-08
- Why selected: Family-level Ascend910B constants; not a substitute for B1–B4 SKU notes.
- Association to PR #16157: **none** (platform/microbench only)

## Explicitly excluded（示例）

- `corpus/references/mcp-stdio-newline-delimited-jsonrpc.md` — MCP/stdio，VAWS 工具契约，非 NPU 推理。
- `examples/corpus-distribution/corpus/graph-launch.md`、`examples/corpus-contribution/ordinary.md` — 贡献/分发示例文案，非可检索生产经验主体。
- `vaws_knowledge/**`、`tests/**`、docs 中的 distribution/contribution — VAWS 实现。
- 大量 310 / 950 / Kirin platform peaks — 同型重复；本轮优先 910B 系列（常见 vllm-ascend 推理卡语境），不代表其它 SoC 可忽略。

## Unverified conflicts

- 各 peak 笔记自称「historical / not independently confirmed」；本轮未对照本机 CANN 安装校验数值。
- 910B 家族笔记与 B1–B4 SKU 笔记并存：**不得把家族峰值当作某一 SKU 的峰值**。
- sustained 笔记缺 CANN/driver/torch 版本边界；与 theoretical peak 笔记不可混为一谈。

## Retrieval queries

1. `vaws-knowledge Ascend910B4 platform_config peaks CANN 9.0.0`
2. `Ascend910B4 single-card dense matmul sustained fraction torch_npu`
3. `vllm-ascend-workspace vaws-knowledge NPU MFU theoretical peak`
4. `vaws-knowledge ACL Graph HCCL dspark`
5. `e16d87287f7db51ca96efc945a9514e4146131d6 corpus/references ascend910b`

## Gap statement

若需要 ACL Graph / HCCL / vllm-ascend 控制流经验，本 corpus @ 本 SHA **不能**提供；应回到上游 PR/docs/源码（如本库 `topics/pr-16157-acl-graph-padding-precision.md`）。
