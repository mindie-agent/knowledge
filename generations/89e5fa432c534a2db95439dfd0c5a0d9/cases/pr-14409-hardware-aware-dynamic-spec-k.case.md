# Case: Hardware-aware dynamic speculative decoding（physical K）

- **日期**: 2026-09-17（修订）；初建 2026-09-14
- **状态**: OPEN not-draft / **CONFLICTING·DIRTY** / reference-only；**head 仍在移动 — 每轮需重 pin**
- **来源 PR**: https://github.com/vllm-project/vllm-ascend/pull/14409 （OPEN；author `momo609`；labels: `module:tests`, `module:core`, `merge-conflicts`；title now `[MRV2]`）
- **Pinned（本轮 gh 2026-09-17）**: base `0b345a675599bcd31eb3907a57921a9cedaf4ef2` · head `cede05131b37f9df4e36b17645fa91ae07ab9188`

## 修订说明 / Revision notes（2026-09-17，Asia/Shanghai）

- **重 pin head**：`6ee85790…` → `cede05131b37f9df4e36b17645fa91ae07ab9188`（base 仍 `0b345a675599bcd31eb3907a57921a9cedaf4ef2`）。
- 相对上轮 tip 增量（VERIFIED commit headlines）：`perf(spec-decode): make the hardware-aware draft-K cost model usable`（`70d275160b5a…`）；`perf(spec-decode): bypass adaptive policy for small batches`（`cede05131b37…`）。
- 规模本轮 +2159/−68，**10 files**；仍 **DIRTY/CONFLICTING**。
- 保留旧对比：https://github.com/vllm-project/vllm-ascend/compare/0b345a675599bcd31eb3907a57921a9cedaf4ef2...6ee8579036c0e7e838c3829b2b7f76dba8becfd6
- 新对比：https://github.com/vllm-project/vllm-ascend/compare/0b345a675599bcd31eb3907a57921a9cedaf4ef2...cede05131b37f9df4e36b17645fa91ae07ab9188
- NPU 增益仍 **UNVERIFIED**；继续 **pin-latest-each-run**。

## 修订说明 / Revision notes（2026-09-16，Asia/Shanghai）

- **重 pin**：base `6b9b8396…` → `0b345a67…`；head `6b458cff…` → `6ee85790…`（rebase/重写后 tip；仍 DIRTY）。
- 规模本轮 +1916/−68，**10 files**（上轮 15）— 文件面收窄到 `worker/v2/spec_decode/{hardware_aware,dflash,dspark}` + `dynamic_spec.py` + UTs（VERIFIED file list）。
- 标题现为 `feat: [MRV2] add dynamic hardware-aware for dspark`。
- 保留旧对比：https://github.com/vllm-project/vllm-ascend/compare/6b9b83968f5ea81aa11750fc19ef4bd1eb383321...6b458cff751ae55fdeb3c8c30d594ef7e0293bb4
- 新对比：https://github.com/vllm-project/vllm-ascend/compare/0b345a675599bcd31eb3907a57921a9cedaf4ef2...6ee8579036c0e7e838c3829b2b7f76dba8becfd6
- PR body 性能表（Qwen3-8B V2 D-Spark FULL Graph）→ **CLAIM**；NPU 增益本轮仍 **UNVERIFIED**。
- 继续 **pin-latest-each-run**。

## 修订说明 / Revision notes（2026-09-15，Asia/Shanghai）

- **重 pin head**：`2aacea2f…` → `6b458cff…`（base 仍 `6b9b8396…`）。
- mergeable **CONFLICTING** / mergeStateStatus **DIRTY**；label `merge-conflicts`。
- 相对上轮 tip 的可见增量（compare）：`dynamic_spec.py` auto-tune 改为 **per-batch-bucket** `_auto_k_by_bucket`；`cap(..., batch_size=)`；cost model 用显式 `physical_k` 而非 `max(widths)`；新增 UT（physical_k vs logical width、small-batch full K、bucket 隔离）。
- 保留旧对比：https://github.com/vllm-project/vllm-ascend/compare/6b9b83968f5ea81aa11750fc19ef4bd1eb383321...2aacea2f2641da45d91074a4b79f38b32fa196ce
- 新对比：https://github.com/vllm-project/vllm-ascend/compare/6b9b83968f5ea81aa11750fc19ef4bd1eb383321...6b458cff751ae55fdeb3c8c30d594ef7e0293bb4
- Checks：DCO/ci-gate/pre-commit 仍 **fail**；NPU 增益 **UNVERIFIED**。

## 修订说明 / Revision notes（2026-09-14，Asia/Shanghai）

- 新建 case；注明 **pin-latest-each-run**：head 相对历史 tip 已变，下次维护必须重取 SHA。
- Checks：DCO **fail**、ci-gate **fail**、pre-commit **fail**；PR create/main **pass**。
- 性能收益、CI 失败根因、运行时 NPU 增益 → **UNVERIFIED**。
- 追随 #15098（CLAIM 关系）。

## Trigger — 何时想起本 case

讨论 **动态控制下一轮物理 draft 长度 K**（batch/acceptance hybrid），使更短 K 减少 draft 算力；以及：

- `DynamicSpecConfig` policy `confidence_budget` | `hardware_aware`；
- DFlash/DSpark + `physical_k_scope` / ACLGraph capture widths；
- `AdaptiveDraftKController` 默认阈值、hysteresis / probe、**auto_tune per-batch-bucket**。

## Preconditions / environment signals

- 仓库：`vllm-project/vllm-ascend`；base @ `0b345a675599bcd31eb3907a57921a9cedaf4ef2`；head `cede05131b37f9df4e36b17645fa91ae07ab9188`。
- Methods：**limited to dspark / dflash**（VERIFIED 委托）。
- Graph：ACLGraph capture 使用 `physical_k_capture_scope` / `extend_capture_descriptors`；PIECEWISE adaptive-verification gate wrapper。
- **DIRTY** + 多项 CI fail → 合入前证据不足。

## Observed failure pattern（若有）

- PR 动机为 **feature**（动态 K），非单一 crash log；本轮无独立现场日志。

## Fix direction（仅限本 PR，附条件）

1. `dynamic_spec.py`：`resolve_physical_k`；`AdaptiveDraftKController`；defaults `min_batch=8`, `acceptance=0.6`, `low_steps=4`, `high_steps=2`, `probe_interval=32`。
2. `DynamicSpecConfig`：policy `confidence_budget|hardware_aware`；`physical_k` object；methods 限 dspark/dflash。
3. `hardware_aware.py`（+711）：capture_k widths、`physical_k_scope`、buffers、PIECEWISE adaptive-verification gate wrapper、DFlash/DSpark mixins。
4. DFlash/DSpark inherit PhysicalK mixins；`propose` 包 `physical_k_scope`；ACLGraph capture：`physical_k_capture_scope` / `extend_capture_descriptors`。
5. Model runner：`adaptive_verification_gate_wrapper`；UTs：policy + runtime widths。
6. **本轮增量（VERIFIED from diff vs prior head）**：auto_tune 按 batch bucket 选 K；observe 接受 `physical_k=`；small-batch 保持 full K 路径。
7. **CLAIM**：controller 仅 CPU acceptance；hysteresis + 周期性 full-width probes。

**条件**: head `cede05131b37f9df4e36b17645fa91ae07ab9188`（可能继续移动）；OPEN **DIRTY**；DCO/ci-gate/pre-commit fail。

## Do-not-overgeneralize

- 禁止把 dynamic K 说成已验证 NPU 吞吐提升（**UNVERIFIED**）。
- 禁止外推到非 dspark/dflash 方法。
- 禁止假设 head 固定；须 **pin-latest-each-run**。
- DIRTY ≠ 可合入；追随 #15098 ≠ #15098 行为已在本 PR 复述完整。

## Evidence links

- PR: https://github.com/vllm-project/vllm-ascend/pull/14409
- Compare（本轮）: https://github.com/vllm-project/vllm-ascend/compare/0b345a675599bcd31eb3907a57921a9cedaf4ef2...cede05131b37f9df4e36b17645fa91ae07ab9188
- Compare（2026-09-16 tip 永久）: https://github.com/vllm-project/vllm-ascend/compare/0b345a675599bcd31eb3907a57921a9cedaf4ef2...6ee8579036c0e7e838c3829b2b7f76dba8becfd6
- Compare（2026-09-15 tip 永久）: https://github.com/vllm-project/vllm-ascend/compare/6b9b83968f5ea81aa11750fc19ef4bd1eb383321...6b458cff751ae55fdeb3c8c30d594ef7e0293bb4
- Compare（2026-09-14 永久）: https://github.com/vllm-project/vllm-ascend/compare/6b9b83968f5ea81aa11750fc19ef4bd1eb383321...2aacea2f2641da45d91074a4b79f38b32fa196ce
- Related: https://github.com/vllm-project/vllm-ascend/pull/15098
- Spec decoding docs: https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/speculative_decoding.html

## Retrieval queries

1. hardware_aware dynamic speculative decoding physical_k AdaptiveDraftKController
2. DynamicSpecConfig confidence_budget hardware_aware dspark dflash
3. physical_k_scope physical_k_capture_scope extend_capture_descriptors ACLGraph
4. PR 14409 head 6ee85790 DIRTY MRv2 hardware_aware pin-latest
5. adaptive_verification_gate_wrapper PIECEWISE min_batch acceptance probe_interval
