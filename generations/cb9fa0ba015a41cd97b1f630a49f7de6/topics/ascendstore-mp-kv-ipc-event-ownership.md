# [参考] AscendStore multiprocess KV transfer / IPC / Event 所有权（PR #15832）

- **日期**: 2026-09-16（修订）；初建 2026-09-14
- **状态**: **CLOSED** not-merged / was draft / DIRTY at close / reference-only（非正确性背书）
- **阅读方式**: 本轮 `gh` 元数据 + PR body（未跑 NPU；未改上游）

## Pinned（本轮委托证据 2026-09-16）

| 字段 | 值 |
|---|---|
| PR URL | https://github.com/vllm-project/vllm-ascend/pull/15832 |
| 标题 | `[KV Pool][Feature] Run AscendStore KV transfers in worker subprocesses` |
| 状态 | **CLOSED**；`merged=false`；closed ≈ 2026-09-15 11:54 Asia/Shanghai；was **draft**；mergeable **CONFLICTING**；mergeStateStatus **DIRTY** |
| base / head 分支 | `main` / `feat/ascend-store-transfer-mp` |
| **base SHA** | `26f1363f7180dfcbeac1230679976cede6010fc8` |
| **head SHA** | `b92271485c7991ae8aa36ab24d2cb7c497b5641d` |
| 作者 | `ChenZhuo888` |
| labels | `ci/build`, `module:tests`, `module:tools`, `merge-conflicts`, `ready-precise` |
| 规模 | +4609 / −15，**30 files** |
| 对比（本轮） | https://github.com/vllm-project/vllm-ascend/compare/26f1363f7180dfcbeac1230679976cede6010fc8...b92271485c7991ae8aa36ab24d2cb7c497b5641d |
| 对比（2026-09-15 tip 永久） | https://github.com/vllm-project/vllm-ascend/compare/cdad5a32e0a0cc0232ae29ded29636162f4fb690...22c8c589ec55976d5f77154f7655ffe53db10431 |
| 对比（更早 tip 永久） | https://github.com/vllm-project/vllm-ascend/compare/660c4582aa580ce98edc9b681bb6ff6d03153575...cea32e97544a2f3f73d5c5310569077912963bd0 |

### Prior pins（保留，勿当最新）

| 轮次 | base | head |
|---|---|---|
| 2026-09-14 | `660c4582aa580ce98edc9b681bb6ff6d03153575` | `cea32e97544a2f3f73d5c5310569077912963bd0` |
| 2026-09-15 | `cdad5a32e0a0cc0232ae29ded29636162f4fb690` | `22c8c589ec55976d5f77154f7655ffe53db10431` |

### CI / checks（本轮 gh）

| Check | 结论 |
|---|---|
| DCO | **pass** |
| pre-commit | **pass** |
| cpu-ut | **pass** |
| 310p selected e2e（多 part） | **pass**（CLAIM 摘要） |
| a3 selected e2e（部分） | **fail**（根因 **UNVERIFIED**） |
| ci-gate | **fail** |

## 修订说明 / Revision notes（2026-09-16，Asia/Shanghai）

1. PR **CLOSED not-merged**；关闭动机 UNVERIFIED（conflicts 历史 + e2e fail 信号，无作者关闭说明）。
2. 重 pin base/head；保留全部旧 compare 永久链接。
3. Event-ownership / multiprocess 设计仍仅作历史 CLAIM 参考。

## 修订说明 / Revision notes（2026-09-15，Asia/Shanghai）

1. Rebase 重 pin base/head；架构 CLAIM/VERIFIED 分层保持。
2. 新增 `ready-precise` 与选测混合结果；**勿**把 310P pass / A3 fail 外推为功能正确性结论。
3. 仍 draft；默认 `use_multiprocess=false`。

## 修订说明 / Revision notes（2026-09-14，Asia/Shanghai）

1. 新建 topic：mp KV transfer 架构要点 + Event 所有权 CLAIM。
2. 默认 `use_multiprocess=false` — 开启需显式配置。
3. Draft：合入形态与 hang 全版本复现 **UNVERIFIED**。

## Motivation（CLAIM）

- RFC #14143：transfer-process 削减进程内 AscendStore transfer 线程的 Python/GIL 争用。

## Architecture（VERIFIED from PR paths / body）

1. **开关**：`kv_connector_extra_config.use_multiprocess`，**default false**。
2. **职责划分**：Worker = scheduling + KV ownership；child = backend I/O、registration、key construction。
3. **包路径**：`vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/mp/`（adapter/client/npu_ipc/process/server/service/transfer/tp_mismatch/mooncake_backend）。
4. **IPC**：torch-npu IPC 重建 KV；ZMQ 命令/结果；storage handle 含 producer device index。
5. **Event 设计（CLAIM experiment → design）**：跨进程 Event synchronize 在 HCCL-associated 序列上 hung → Events 留 Worker；child Event slots 为 `None`；本地 wait 后再 RPC。
6. **Mooncake**：`register_memory(address, length, "npu:<device_index>")`；pin `0.3.12.post1`（Mooncake #2191）。

## Do-not-overgeneralize

- Draft ≠ 生产默认。
- Hang 未覆盖全部 CANN/torch-npu（**UNVERIFIED**）。
- A2 Mooncake e2e ≠ A3 Memcache ≠ 其它卡型。
- 吞吐改善未测（CLAIM）。

## Evidence links

- PR: https://github.com/vllm-project/vllm-ascend/pull/15832
- Issue/RFC: https://github.com/vllm-project/vllm-ascend/issues/14143
- Mooncake #2191: https://github.[redacted:credential-high-entropy]

## Retrieval queries

1. AscendStore multiprocess Worker Event ownership HCCL hang
2. use_multiprocess mooncake register_memory npu device_index
3. ascend_store mp npu_ipc ZMQ DEALER pool_worker
4. PR 15832 head 22c8c589 rebase pin-latest
5. ready-precise AscendStore transfer mp 310p pass a3 fail
