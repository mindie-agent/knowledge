# [参考] Ascend Mooncake EC connector（MRv1）（PR #16555）

- **日期**: 2026-09-16（修订）；初建 2026-09-15
- **状态**: OPEN / blocked / reference-only
- **阅读方式**: `gh` + PR body/files（未跑 NPU）

## 修订（2026-09-17）

- base `10b4fb2a…` / head `f9f35266…`；rebase pin；仍 BLOCKED；V2 out of scope。

## Pinned（本轮 2026-09-16）

| 字段 | 值 |
|---|---|
| PR | https://github.com/vllm-project/vllm-ascend/pull/16555 |
| 标题 | `[Feature][EC Connector] Add Mooncake EC transfer support for ModelRunner V1` |
| state | OPEN；MERGEABLE；BLOCKED |
| base / head | `26f1363f7180dfcbeac1230679976cede6010fc8` / `f9f352666160e2cd1e467a4cc3a6bd97ca3aec7c` |
| 规模 | +408 / −12，**7 files** |
| 对比（本轮） | https://github.com/vllm-project/vllm-ascend/compare/26f1363f7180dfcbeac1230679976cede6010fc8...f9f352666160e2cd1e467a4cc3a6bd97ca3aec7c |
| 对比（2026-09-15 tip 永久） | https://github.com/vllm-project/vllm-ascend/compare/26f1363f7180dfcbeac1230679976cede6010fc8...e396378001f37be2b0ec785e14e6735ee5f6e683 |

### Prior pins

| 轮次 | head |
|---|---|
| 2026-09-15 | `e396378001f37be2b0ec785e14e6735ee5f6e683` |

### CI

| Check | 结论 |
|---|---|
| DCO / PR create | **pass**（摘要） |
| ci-gate / pre-commit | **fail** / blocked（摘要） |

## Architecture highlights（VERIFIED paths + CLAIM semantics）

- Adapter under `vllm_ascend/distributed/ec_transfer/mooncake.py`；plugin register（本轮 head 含 alias 防 name shadowing）。
- Staging/receive 2 MiB alignment；reject direct source registration without staging（CLAIM）。
- V1 runner overrides must surface EC completion on encoder-only / no-forward paths。
- V2 **out of scope**。

## Retrieval queries

1. Ascend ECMooncakeConnector ec_transfer mooncake.py
2. EPD Mooncake encoder cache NPU Event staging
3. PR 16555 ModelRunner V1 EC completion no-forward
4. issue 6026 Mooncake cross-node encoder-cache
5. head e634788d mypy alias KV connector registration
