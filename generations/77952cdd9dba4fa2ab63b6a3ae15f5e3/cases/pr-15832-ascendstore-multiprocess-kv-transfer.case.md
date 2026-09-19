# Case: AscendStore KV 传输放入 worker 子进程（draft；CLOSED 未合并）

- **日期**: 2026-09-16（修订）；初建 2026-09-14
- **状态**: **CLOSED** not-merged / was draft / mergeable CONFLICTING·DIRTY at close / reference-only
- **来源 PR**: https://github.com/vllm-project/vllm-ascend/pull/15832 （CLOSED；author `ChenZhuo888`；labels include `merge-conflicts`, `ready-precise`）
- **Pinned（本轮 gh 2026-09-16）**: base `26f1363f7180dfcbeac1230679976cede6010fc8` · head `b92271485c7991ae8aa36ab24d2cb7c497b5641d`

## 修订说明 / Revision notes（2026-09-16，Asia/Shanghai）

- **状态变更**：OPEN draft → **CLOSED**（`merged=false`；closed_at 2026-09-15T03:54:32Z ≈ Asia/Shanghai 11:54）。关闭原因未在公开评论中明示（历史多次 conflicts bot；作者曾触发 e2e fail）→ 关闭动机 **UNVERIFIED**。
- **重 pin**：base `cdad5a32…` → `26f1363f…`；head `22c8c589…` → `b9227148…`；规模本轮 +4609/−15，**30 files**（较上轮 33 files 收窄）。
- 保留旧永久对比：https://github.com/vllm-project/vllm-ascend/compare/cdad5a32e0a0cc0232ae29ded29636162f4fb690...22c8c589ec55976d5f77154f7655ffe53db10431
- 新对比：https://github.com/vllm-project/vllm-ascend/compare/26f1363f7180dfcbeac1230679976cede6010fc8...b92271485c7991ae8aa36ab24d2cb7c497b5641d
- 设计要点（Worker Event ownership / `use_multiprocess` default false / Mooncake mp register）仍作 **CLAIM** 参考；**勿**当已合入行为。
- 笔记保留不删（CLOSED 案例惯例）。

## 修订说明 / Revision notes（2026-09-15，Asia/Shanghai）

- **重 pin**：base `660c4582…` → `cdad5a32…`；head `cea32e97…` → `22c8c589…`（rebase onto newer main；设计叙述仍以 PR body 为准）。
- 保留旧永久对比：https://github.com/vllm-project/vllm-ascend/compare/660c4582aa580ce98edc9b681bb6ff6d03153575...cea32e97544a2f3f73d5c5310569077912963bd0
- 新对比：https://github.com/vllm-project/vllm-ascend/compare/cdad5a32e0a0cc0232ae29ded29636162f4fb690...22c8c589ec55976d5f77154f7655ffe53db10431
- 规模本轮：+5707 / −15，**33 files**；`ready-precise`；ci-gate **fail**；选测：310P 多卡 **pass**，部分 A3 **fail**（根因 **UNVERIFIED**）。
- 仍 **draft**；`use_multiprocess` default false；Event 所有权实验标 **CLAIM**。

## 修订说明 / Revision notes（2026-09-14，Asia/Shanghai）

- 新建 case：**draft** 条件全程标注；merge readiness **UNVERIFIED**。
- Event 所有权实验与「最终设计 Events 留在 Worker」标 **CLAIM**（实验叙述）；包结构 / 开关 / Mooncake pin 等可核对点标 VERIFIED。
- **勿**把 A2 Mooncake e2e 与 A3 Memcache e2e 叙述外推为全硬件等价；310P 未在本 PR 证据中等同。

## Trigger — 何时想起本 case

讨论 **AscendStore** KV connector 时：

- 进程内 transfer 线程的 Python/GIL 争用；
- `kv_connector_extra_config.use_multiprocess`；
- 跨进程 NPU Event synchronize hang（HCCL-associated producer）；
- Mooncake `register_memory(..., "npu:<device_index>")` 与 engine 版本 pin。

## Preconditions / environment signals

- 仓库：`vllm-project/vllm-ascend`；base `main` @ `26f1363f7180dfcbeac1230679976cede6010fc8`；head `feat/ascend-store-transfer-mp` @ `b92271485c7991ae8aa36ab24d2cb7c497b5641d`（CLOSED tip）。
- RFC 语境：#14143 transfer-process（**CLAIM** 动机）。
- 开关：`use_multiprocess` **default false**（可选）。
- Draft / blocked → 行为可能继续变。

## Observed failure pattern（若有）

- **[CLAIM experiment]** 跨进程 import 的 NPU Event 在 HCCL-associated producer 之后 synchronize **hung**；同进程 Event OK → 设计选择：Events 留 Worker；child 侧 `None` Event slots；本地 wait 后再 RPC 形成 happens-before。
- **[CLAIM precedents]** LMCache-Ascend HCCL channel + SGLang Ascend HiCache：Events 留在 owning process。

## Fix direction（仅限本 PR，附条件）

1. 可选 `kv_connector_extra_config.use_multiprocess`（默认 false）。
2. Worker 保留 scheduling / KV ownership；child 负责 backend I/O、registration、key construction。
3. 包 `.../ascend_store/mp/`：`process.py`（Popen+pipe）、`client.py`（ZMQ DEALER）、`npu_ipc.py`（export/import storage specs）、adapter/service/transfer/tp_mismatch/mooncake_backend。
4. `pool_worker.py` 读开关；init 顺序 KV-events vs backend；`_get_worker_global_rank` 跨 DP×TP×PP×PCP。
5. Mooncake mp：`register_memory(address, length, "npu:<device_index>")`；CI pin `mooncake-transfer-engine-npu` `0.3.11.post1` → `0.3.12.post1`（Mooncake #2191）。
6. 覆盖面 CLAIM：block / key-layerwise / GVA-layerwise、hybrid/Mamba、compressed、TP mismatch、Mooncake SSD（SSD e2e 环境缺 → 仅 UT）。

**条件**: head `b92271485c7991ae8aa36ab24d2cb7c497b5641d`；**CLOSED not-merged**；历史 draft；默认关闭 multiprocess（CLAIM）。

## Do-not-overgeneralize

- Draft ≠ 可生产默认开启。
- Hang 复现未宣称覆盖全部 CANN/torch-npu（**UNVERIFIED**）。
- 「可能减少 transfer/compute overlap」无测吞吐（CLAIM）。
- A2 one-card Mooncake ≠ A3 two-card Memcache ≠ 其它卡型；本轮 A3 选测 fail ≠ 全栈否定。

## Evidence links

- Compare（2026-09-16 tip）: https://github.com/vllm-project/vllm-ascend/compare/26f1363f7180dfcbeac1230679976cede6010fc8...b92271485c7991ae8aa36ab24d2cb7c497b5641d

- PR: https://github.com/vllm-project/vllm-ascend/pull/15832
- Compare（本轮）: https://github.com/vllm-project/vllm-ascend/compare/cdad5a32e0a0cc0232ae29ded29636162f4fb690...22c8c589ec55976d5f77154f7655ffe53db10431
- Compare（2026-09-14 永久保留）: https://github.com/vllm-project/vllm-ascend/compare/660c4582aa580ce98edc9b681bb6ff6d03153575...cea32e97544a2f3f73d5c5310569077912963bd0
- RFC ref: https://github.com/vllm-project/vllm-ascend/pull/14143

## Retrieval queries

1. AscendStore use_multiprocess KV transfer worker subprocess
2. npu_ipc export import storage specs ZMQ DEALER ascend_store mp
3. NPU Event hang HCCL cross-process synchronize Worker ownership
4. mooncake-transfer-engine-npu 0.3.12.post1 register_memory npu:device
5. PR 15832 draft feat/ascend-store-transfer-mp head 22c8c589
