# Case: Mooncake EC transfer adapter for ModelRunner V1（Ascend）

- **日期**: 2026-09-17（修订）；初建 2026-09-15
- **状态**: OPEN not-draft / MERGEABLE / **BLOCKED** / reference-only
- **来源 PR**: https://github.com/vllm-project/vllm-ascend/pull/16555 （author `lirx-pd`；labels: `module:tests`, `module:ops`, `module:core`）
- **Pinned（本轮 gh 2026-09-17）**: base `10b4fb2a99c4fe8f33efb591b1f2996265430ce3` · head `f9f352666160e2cd1e467a4cc3a6bd97ca3aec7c`

## 修订说明 / Revision notes（2026-09-17，Asia/Shanghai）

- **重 pin**：base `26f1363f…` → `10b4fb2a99c4fe8f33efb591b1f2996265430ce3`；head `e634788d…` → `f9f352666160e2cd1e467a4cc3a6bd97ca3aec7c`（rebase 后 tip；commit set 仍含 alias + mypy 修复）。
- 规模仍 +408/−12，**7 files**；MERGEABLE **BLOCKED**。
- 保留旧对比：https://github.com/vllm-project/vllm-ascend/compare/26f1363f7180dfcbeac1230679976cede6010fc8...e634788d126e43cc5e25a4a2b26c1276dc6cb8bc
- 新对比：https://github.com/vllm-project/vllm-ascend/compare/10b4fb2a99c4fe8f33efb591b1f2996265430ce3...f9f352666160e2cd1e467a4cc3a6bd97ca3aec7c
- V2 out of scope；bench **CLAIM**；架构要点未改。

## 修订说明 / Revision notes（2026-09-16，Asia/Shanghai）

- **重 pin head**：`e3963780…` → `e634788d…`（base 不变 `26f1363f…`）。
- 相对旧 tip 增量（VERIFIED compare）：2 commits — `Alias KV connector registration to avoid name shadowing`；`Fix mypy errors in Mooncake EC adapter type annotations`；触及 `mooncake.py` / `__init__.py` / UT。
- 规模本轮 +408/−12，**7 files**。
- 保留旧对比：https://github.com/vllm-project/vllm-ascend/compare/26f1363f7180dfcbeac1230679976cede6010fc8...e396378001f37be2b0ec785e14e6735ee5f6e683
- 新对比：https://github.com/vllm-project/vllm-ascend/compare/26f1363f7180dfcbeac1230679976cede6010fc8...e634788d126e43cc5e25a4a2b26c1276dc6cb8bc
- 架构要点未改：V2 out of scope；bench CLAIM；CI blocked。

## 修订说明 / Revision notes（2026-09-15，Asia/Shanghai）

- 新建 case；EPD / encoder-cache Mooncake 数据面；V2 runner **明示 out of scope**。
- Benchmark 表（Qwen3.5-35B-A3B on 910B）来自 PR body → **CLAIM**（单次 run；未隔离 transport）。
- CI：ci-gate/pre-commit **fail**（本轮）；本地 UT 11 passed 为作者 CLAIM。

## Trigger — 何时想起本 case

讨论 **encoder-cache（EC）** 跨实例传输、EPD、或 `ECMooncakeConnector` 在 Ascend 上的适配时：

- NPU staging / 2 MiB 对齐 / 拒绝直接注册任意 source view；
- NPU Event 源就绪与 receive-buffer 复用；
- V1 `execute_model()` 覆盖 encoder-only / no-forward 返回路径上的 EC 完成信息。

## Preconditions / environment signals

- base `main` @ `10b4fb2a…`；head @ `f9f35266…`。
- 文件（VERIFIED）：`distributed/ec_transfer/mooncake.py`、`__init__.py` 注册、`worker/model_runner_v1.py`、`worker/worker.py`、`ops/fused_moe/routed_experts.py`（MoE counter on NPU）、UT `tests/ut/distributed/ec_transfer/test_mooncake_adapter.py`。
- 用户面（CLAIM）：`ec_connector="ECMooncakeConnector"` → Ascend adapter；默认 `ascend` transport；拒绝非 Ascend protocol。

## Observed failure pattern（若有）

- Feature；动机含「no-forward 路径漏 EC completion 信息」→ scheduler 无法推进异步传输（PR CLAIM）。

## Fix direction（仅限本 PR，附条件）

1. 插件入口注册 Ascend `ECMooncakeConnector` 实现。
2. `MooncakeTransfer` + process-local Ascend TransferEngine；2 MiB 对齐 staging/receive；无 staging 时失败而非注册原 tensor。
3. NPU events 跟踪 source readiness；延迟 receive-buffer reuse。
4. Worker：模型加载后启动 EC services；shutdown 遵循 KV-then-EC。
5. V1 runner：encoder-only / no-forward 共享 helper 收集 EC output。
6. MoE load counter 直接在 NPU 分配（去 CPU 中转）。

**条件**: head `f9f352666160e2cd1e467a4cc3a6bd97ca3aec7c`；OPEN blocked；V2 不在范围。

## Do-not-overgeneralize

- Benchmark 增益（尤其大图 +82% throughput）为单次 CLAIM；TPOT 可能变差。
- Example EPD（本地共享文件系统）≠ 纯 transport 对比。
- 禁止外推到 MRv2。

## Evidence links

- PR: https://github.com/vllm-project/vllm-ascend/pull/16555
- Compare: https://github.com/vllm-project/vllm-ascend/compare/26f1363f7180dfcbeac1230679976cede6010fc8...e396378001f37be2b0ec785e14e6735ee5f6e683
- Related roadmap issue #6026: https://github.com/vllm-project/vllm-ascend/issues/6026
- Head adapter: https://github.com/vllm-project/vllm-ascend/blob/e396378001f37be2b0ec785e14e6735ee5f6e683/vllm_ascend/distributed/ec_transfer/mooncake.py

## Retrieval queries

1. ECMooncakeConnector Ascend EC transfer ModelRunner V1
2. Mooncake EPD encoder-cache NPU staging 2MiB Event readiness
3. PR 16555 e3963780 ec_transfer mooncake adapter
4. execute_model encoder-only no-forward EC completion V1
5. Qwen3.5 Mooncake EPD vs Example EPD 910B CLAIM bench
