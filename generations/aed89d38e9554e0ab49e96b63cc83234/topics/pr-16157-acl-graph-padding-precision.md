# [参考] vllm-ascend PR #16157：Kimi K3 DSpark ACL Graph / padding / 长度契约

- **日期**: 2026-09-13
- **状态**: draft / reference-only（非正确性背书；不以 merge / 热度 / 生成摘要当证据）
- **阅读方式**: **本轮** 用本机已登录 `gh`（GitHub API + `gh pr view/checks/diff`）+ head blob 源码控制流阅读

## Pinned（本轮 gh 实际读到的元数据）

| 字段 | 值 |
|---|---|
| PR URL | https://github.com/vllm-project/vllm-ascend/pull/16157 |
| 标题 | `[wip]Kimi k3 acl graph for dspark 0.26` |
| 状态（读到时） | **OPEN**；`isDraft=false`；labels: `module:tests`, **`merge-conflicts`** |
| base 分支 | `releases/v0.26.0rc` |
| head 分支 | `kimi_k3_acl_graph_for_dspark_0.26` |
| **base SHA（本轮）** | `fb2820b9b6598f888d7dc56260c03d3352cc08e9` |
| **head SHA（本轮）** | `debfa9bd48fefcc90added07b28540daee8b5435` |
| mergeable | **CONFLICTING** / mergeStateStatus **DIRTY** |
| 作者 | `ghphotoframe`（JiangWeixiang） |
| commits | 3：`9c7e8949…` → `8bf4443c…` → `debfa9bd…` |
| 规模 | +867 / −51，**5 files** |
| 对比永久链接 | https://github.com/vllm-project/vllm-ascend/compare/fb2820b9b6598f888d7dc56260c03d3352cc08e9...debfa9bd48fefcc90added07b28540daee8b5435 |

### CI / checks（`gh pr checks` / statusCheckRollup，事实）

| Check | 结论 |
|---|---|
| PR create action | **pass** / SUCCESS（completed 2026-09-09T09:17:55Z） |
| DCO | **fail** / ACTION_REQUIRED（https://probot.github.io/apps/dco/） |

未见其它 NPU e2e / UT workflow 出现在本轮 rollup 中。

## 修订说明 / Revision notes（2026-09-13，Asia/Shanghai）

相对上一稿（pinned base `f2ca70a5d27bb6ef5e03029001b466d459023684` · head `debfa9bd48fefcc90added07b28540daee8b5435`；当时 `gh` 未登录、靠 HTML/diff）：

1. **重新 pin base**：`releases/v0.26.0rc` tip 已从 `f2ca70a5d27bb6ef5e03029001b466d459023684` 变为 `fb2820b9b6598f888d7dc56260c03d3352cc08e9`（commit message: Unify negate_sin… #15940）。**head 未变**仍为 `debfa9bd48fefcc90added07b28540daee8b5435`。
2. **聚合 diff 与本轮 `gh pr diff` / pinned compare 一致**（相对原始本地 run 包附件中的 raw diff 字节级相同；本地 `_raw/` 仅存在于该 run 包 attachment，**不属于本 reference feed**）。稳定对比：https://github.com/vllm-project/vllm-ascend/compare/fb2820b9b6598f888d7dc56260c03d3352cc08e9...debfa9bd48fefcc90added07b28540daee8b5435
3. **本轮用 `gh` API 取到 mergeable=CONFLICTING、DCO fail、PR create pass**；上一稿未写 DCO。
4. **控制流重读**（见下节）：纠正「仅凭 `_is_k3_dspark` / 注释即可声称非 K3 DSpark 已正确门控为 eager」的表述。标注 **VERIFIED / CLAIM / UNVERIFIED**。
5. 阅读方式字段更新为已认证 `gh`。

## Problem / symptoms（有证据）

PR 描述与代码注释共同指向（**不是** fp16/bf16 dtype 精度开关；聚合 diff **未出现** `precision` / `fp16` / `bf16`）：

1. DSpark draft 路径此前曾硬锁 eager（PR 称删 `use_cuda_graph = False`）；本轮 head 上 `AscendDSparkProposer.__init__` **未见** 对该字段赋 `False`。
2. ACL Graph FULL 固定宽度 vs K3 Query T：`B×N`（draft）vs `B×(1+N)`（target / descriptor）。
3. padding 行靠 `slot_mapping=-1`；`num_actual_tokens=num_query_total`。
4. MLA/DSA 长度契约 / “修双重计数”。

**未在 PR 正文看到**：现场错误日志、数值漂移基准、硬件复现步骤。

## Control-flow analysis（head `debfa9bd48fefcc90added07b28540daee8b5435`）

### VERIFIED from control flow / executable code

1. **`use_cuda_graph` 赋值**（`llm_base_proposer.py`）：`self.use_cuda_graph = self.runner._use_aclgraph() and not self.speculative_config.enforce_eager`。**不**依赖 `_is_k3_dspark`。
2. **强制 eager 的代码路径**：仅见 `_is_glm_model(...)` 时 `self.use_cuda_graph = False`。**未见** 对「非 K3 的 DSpark（如 DeepSeek V4）」的对称强制 eager 赋值。
3. **`_is_k3_dspark`**：`return method == "dspark" and isinstance(draft_hf_config, K3DSparkConfig)`；`__init__` 写一次布尔；**门控的是图模式下的专用分支**，不是 `use_cuda_graph` 总开关：
   - `use_cuda_graph` 且 `_is_k3_dspark`：`graph_dispatch_tokens = batch_size * (1 + N)`；否则 dispatch 用 `num_tokens`。
   - `aclgraph_runtime_mode == FULL` 且 `_is_k3_dspark`：走 `get_graph_query_num_tokens` + `pad_query_start_loc_for_graph`，并保留 `set_inputs_first_pass` 已写的 device/host `seq_lens`；否则走 `_pad_query_start_loc_for_fia` / optimistic `seq_lens` 分支。
4. **当 `_is_k3_dspark` 为 False 且 `use_cuda_graph` 为 True**：仍进入 `cudagraph_dispatcher.dispatch`；第二次 dispatch 可得到 `CUDAGraphMode.FULL`，即 **非 K3 仍可能到达 FULL 路径**，只是不走 K3 专用 padding/长度保留。
5. **`AscendDSparkProposer.get_graph_query_num_tokens`**：非 `K3DSparkConfig` 时 `return num_input_tokens`；K3 返回 `graph_num_reqs * num_query_per_req`。
6. **`model_runner_v1` `set_draft_graph_params`**：条件是 `method == "dspark"` 且 `isinstance(drafter, AscendDSparkProposer)`（**全体 DSpark proposer**，非仅 K3）；对 FULL descriptors 调用 `get_graph_query_num_tokens` 换算键。
7. **`dummy_run` FULL 分支**（`dspark_proposer.py`）：由 `aclgraph_runtime_mode == FULL` 与 `self.use_cuda_graph` 驱动，**未**再包一层 `_is_k3_dspark`。
8. **`drafter.dummy_run(...)`** 由 runner capture 路径调用，传入当时的 `aclgraph_runtime_mode`。

### CLAIM from comments / PR body / bot reviews（代码 alone 未证明）

1. 函数 docstring / 分支注释写：「Kimi K3 是目前唯一 FULL graph 的 DSpark；DeepSeek V4 / GLM5.2 走 eager」——**GLM 有代码强制 eager；DeepSeek V4「必然 eager」未被 `use_cuda_graph=False` 赋值证明**。
2. 注释「by construction, not merely by runtime scheduling」——实际 construction 只门控了专用分支；总图开关仍看 `_use_aclgraph()` / `enforce_eager` / GLM。
3. PR 正文表格「K3 门控」条目；Gemini / github-actions 机器人评论（需 UT+e2e、有 conflicts）——**≠** 运行时正确性。

### UNVERIFIED / open questions

1. 生产配置下非 K3 DSpark + ACL graph 是否会实际打开 `use_cuda_graph`（调度/配置层是否另有限制）——本轮未跑、未搜全仓库其它门闩。
2. 非 K3 若进入 FULL，通用 FIA padding + optimistic seq_lens 是否正确——**未验证**。
3. merge-conflicts 相对新 base `fb2820b9b6598f888d7dc56260c03d3352cc08e9` 的最终合入形态；WIP 行为可能继续变。
4. e2e / 真机 ACL capture+replay；Gemini 提到的 backend `isinstance` / `get_forward_context()` 风险——未复现。
5. 中间 commit 曾改 `mla_v1.py` 但聚合 diff 净零——意图未核。

## What the PR changes（文件级）

| 文件 | 角色（据 diff + head） |
|---|---|
| `vllm_ascend/spec_decode/dspark_proposer.py` | 缓冲 `B×(1+N)`；MLA/DSA 标志；`pad_query_start_loc_for_graph` / `get_graph_query_num_tokens`；FULL `dummy_run` |
| `vllm_ascend/spec_decode/llm_base_proposer.py` | `_is_k3_dspark`；K3 dispatch 宽度与 FULL 元数据分支 |
| `vllm_ascend/worker/model_runner_v1.py` | DSpark `set_draft_graph_params`：FULL-only + Query-T |
| `tests/ut/spec_decode/test_dspark_proposer.py` | padding / Query-T / K3 gate 单测 |
| `tests/ut/spec_decode/test_llm_base_proposer.py` | FULL 下保留 corrected lengths |

Head blobs：

- https://github.com/vllm-project/vllm-ascend/blob/debfa9bd48fefcc90added07b28540daee8b5435/vllm_ascend/spec_decode/dspark_proposer.py
- https://github.com/vllm-project/vllm-ascend/blob/debfa9bd48fefcc90added07b28540daee8b5435/vllm_ascend/spec_decode/llm_base_proposer.py
- https://github.com/vllm-project/vllm-ascend/blob/debfa9bd48fefcc90added07b28540daee8b5435/vllm_ascend/worker/model_runner_v1.py

## Applicability boundaries

**较明确适用（仍需目标环境复验）**

- `vllm-project/vllm-ascend`，base 语境 `releases/v0.26.0rc` @ `fb2820b9b6598f888d7dc56260c03d3352cc08e9`
- `method == "dspark"` 且 draft HF config 为 `K3DSparkConfig`
- FULL ACL graph（`CUDAGraphMode.FULL` / `use_aclgraph`）

**不应外推**

- 不可仅凭函数名断言「所有非 K3 DSpark 已被代码强制 eager」
- 非 `dspark` 方法；不同 CANN / 硬件 / 版本不可等价
- 非浮点 dtype 精度补丁；PIECEWISE 注册被刻意避开空槽

## Verification scope

- 可见 UT 文件扩展；本轮 **未** 运行 UT/E2E/NPU。
- Checks：仅 DCO fail + PR create pass（见上）。

## Related official sources（≤3）

1. ACL Graph 设计文档：https://docs.vllm.ai/projects/ascend/en/latest/developer_guide/Design_Documents/ACL_Graph.html
2. Speculative Decoding 指南：https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/speculative_decoding.html
3. Head `dspark_proposer.py`：https://github.com/vllm-project/vllm-ascend/blob/debfa9bd48fefcc90added07b28540daee8b5435/vllm_ascend/spec_decode/dspark_proposer.py

## Retrieval queries

1. `vllm-ascend PR 16157 Kimi K3 DSpark ACL Graph FULL padding Query T`
2. `pad_query_start_loc_for_graph get_graph_query_num_tokens B×N vs B×(1+N)`
3. `_is_k3_dspark use_cuda_graph _use_aclgraph 非K3 FULL 门控`
4. `DSpark MLA parallel_draft_seq_lens_cpu DSA max_seqlen_kv double N`
5. `vllm-ascend releases/v0.26.0rc set_draft_graph_params FULL only dspark`

## Explicit uncertainty markers

- `[UNVERIFIED-RUNTIME]` 无 NPU 实测。
- `[CONFLICTS]` merge-conflicts；base 已相对旧稿前移。
- `[NOT-FP-PRECISION]` 此处 “precision” = 长度/元数据契约。
- `[GATE-NOT-EAGER-SWITCH]` `_is_k3_dspark` ≠ `use_cuda_graph=False`。
- `[BOT-REVIEW-ONLY]` Gemini 等未独立验证。
