# [参考] Hardware-aware physical K / ACLGraph（PR #14409）

- **日期**: 2026-09-17（修订）；初建 2026-09-14
- **状态**: OPEN / **DIRTY·CONFLICTING** / reference-only；**head 移动中 — 每轮重 pin**
- **阅读方式**: 本轮 `gh` 元数据 + PR files/diff（未跑 NPU）

## 修订（2026-09-17）

- head → `cede05131b37…`；cost-model usable + small-batch bypass commits（VERIFIED headlines）；仍 DIRTY；NPU **UNVERIFIED**。

## Pinned（本轮委托证据 2026-09-16）

| 字段 | 值 |
|---|---|
| PR URL | https://github.com/vllm-project/vllm-ascend/pull/14409 |
| 标题 | `feat: [MRV2] add dynamic hardware-aware for dspark` |
| 状态 | **OPEN**；not draft；mergeable **CONFLICTING**；mergeStateStatus **DIRTY** |
| labels | `module:tests`, `module:core`, `merge-conflicts` |
| **base SHA** | `0b345a675599bcd31eb3907a57921a9cedaf4ef2` |
| **head SHA（本轮）** | `cede05131b37f9df4e36b17645fa91ae07ab9188` |
| 作者 | `momo609` |
| 规模 | +1916 / −68，**10 files** |
| 对比（本轮） | https://github.com/vllm-project/vllm-ascend/compare/0b345a675599bcd31eb3907a57921a9cedaf4ef2...cede05131b37f9df4e36b17645fa91ae07ab9188 |
| 对比（2026-09-15 tip 永久） | https://github.com/vllm-project/vllm-ascend/compare/6b9b83968f5ea81aa11750fc19ef4bd1eb383321...6b458cff751ae55fdeb3c8c30d594ef7e0293bb4 |
| 对比（更早 tip 永久） | https://github.com/vllm-project/vllm-ascend/compare/6b9b83968f5ea81aa11750fc19ef4bd1eb383321...2aacea2f2641da45d91074a4b79f38b32fa196ce |

### Prior pins（保留，勿当最新）

| 轮次 | base | head |
|---|---|---|
| 更早 | — | `cff3def…`（过时） |
| 2026-09-14 | `6b9b8396…` | `2aacea2f2641da45d91074a4b79f38b32fa196ce` |
| 2026-09-15 | `6b9b83968f5ea81aa11750fc19ef4bd1eb383321` | `6b458cff751ae55fdeb3c8c30d594ef7e0293bb4` |

### CI / checks（本轮 gh）

| Check | 结论 |
|---|---|
| DCO | **fail** |
| ci-gate | **fail** |
| pre-commit | **fail** |
| PR create / main | **pass** |

## 修订说明 / Revision notes（2026-09-16，Asia/Shanghai）

1. 重 pin base+head；仍 DIRTY；文件面收窄到 MRv2 `worker/v2/spec_decode/*` + `dynamic_spec.py`。
2. 性能表 CLAIM；NPU 增益 UNVERIFIED；继续 pin-latest-each-run。

## 修订说明 / Revision notes（2026-09-15，Asia/Shanghai）

1. 重 pin head → `6b458cff…`；状态变为 **DIRTY**。
2. 增量：auto_tune **batch-bucket** K 选择；cost observe 绑定 `physical_k`；相关 UT。
3. NPU 性能与冲突解决路径仍 **UNVERIFIED**。

## 修订说明 / Revision notes（2026-09-14，Asia/Shanghai）

1. 新建 topic；显式 **pin-latest-each-run**。
2. CLAIM：追随 #15098；动态控制 next-iter 物理 draft 长度 K。
3. NPU 性能增益与 CI 失败根因 **UNVERIFIED**。

## Intent（CLAIM）

- 通过 batch/acceptance hybrid 动态控制下一轮物理 draft 长度 K；更短 K → 更少 draft compute。
- 追随 https://github.com/vllm-project/vllm-ascend/pull/15098。

## Key surfaces（VERIFIED from file list）

- `vllm_ascend/dynamic_spec.py`、`worker/v2/spec_decode/hardware_aware.py`
- DFlash/DSpark ACLGraph hooks；`ascend_config.DynamicSpecConfig`
- UTs：`test_hardware_aware_policy.py`、`test_hardware_aware_runtime.py`

## Do-not-overgeneralize

- DIRTY / DCO fail → 非可合入证据。
- 仅 dspark/dflash；禁止外推其它 speculative methods。
- head 必须每轮重 pin。

## Evidence links

- PR: https://github.com/vllm-project/vllm-ascend/pull/14409
- Related #15098: https://github.com/vllm-project/vllm-ascend/pull/15098
- Docs: https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/speculative_decoding.html

## Retrieval queries

1. AdaptiveDraftKController physical_k ACLGraph capture_k
2. hardware_aware.py dspark dflash physical_k_scope
3. PR 14409 DIRTY head 6b458cff pin-latest-each-run
4. auto_tune batch bucket physical_k cost model
5. DynamicSpecConfig confidence_budget hardware_aware
