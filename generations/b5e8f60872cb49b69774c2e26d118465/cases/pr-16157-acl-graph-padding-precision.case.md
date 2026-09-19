# Case: Kimi K3 DSpark 开 ACL Graph FULL 时的 Query-T / padding / 长度契约

- **日期**: 2026-09-13
- **状态**: draft / reference-only
- **来源 PR**: https://github.com/vllm-project/vllm-ascend/pull/16157 （OPEN, WIP, merge-conflicts, DCO fail）
- **Pinned（本轮 gh）**: base `fb2820b9b6598f888d7dc56260c03d3352cc08e9` · head `debfa9bd48fefcc90added07b28540daee8b5435`

## 修订说明 / Revision notes（2026-09-13，Asia/Shanghai）

- base 从 `f2ca70a5d27bb6ef5e03029001b466d459023684` **更新为** `fb2820b9b6598f888d7dc56260c03d3352cc08e9`；head 仍为 `debfa9bd48fefcc90added07b28540daee8b5435`。
- 补充 checks：DCO **fail**；PR create action **pass**；`mergeable=CONFLICTING`。
- **门控表述收紧**：`_is_k3_dspark` 经验证只切换 FULL 下专用 padding/dispatch/seq_lens 分支；**不能**仅凭该名或 eager 注释断言非 K3 DSpark 已被强制 eager。GLM 另有 `_is_glm_model` → `use_cuda_graph=False`。
- 与 topic 同步 VERIFIED / CLAIM / UNVERIFIED 分层。

## Trigger — 何时想起本 case

在 **vllm-ascend** 上为 **Kimi K3 + `method=dspark`** 启用 **ACL Graph FULL**，并讨论：

- draft 图参数键 vs target 宽度（`B×N` vs `B×(1+N)`）；
- padding 行 KV（`slot_mapping=-1` / `num_actual_tokens`）；
- MLA/DSA host 长度双重计数或 FULL 覆盖 corrected `seq_lens`；
- 「非 K3 是否真的进不了 FULL」。

## Preconditions / environment signals

- 仓库/分支：`vllm-ascend`，base `releases/v0.26.0rc` @ `fb2820b9b6598f888d7dc56260c03d3352cc08e9`（**勿默认等于旧 tip `f2ca70a5d27bb6ef5e03029001b466d459023684` 或 main**）。
- Spec：`dspark`；`K3DSparkConfig` / `_is_k3_dspark == True` 才走 K3 专用分支。
- Graph：`runner._use_aclgraph()` 且非 `enforce_eager` → `use_cuda_graph`；再叠加 FULL dispatcher。
- Backend：`AscendMLABackend` vs `AscendDSABackend`。

## Observed failure pattern（若有）

PR **未附** 现场日志。从描述/测试命名可推断（标记推断）：

- `[INFERRED]` Query-T 键错 → 捕获/更新错配；
- `[INFERRED]` 填充行未丢弃 → 错误 KV；
- `[INFERRED]` N 双重计数 / FULL 覆盖 corrected lengths。

**无** 已证实 fp16/bf16 数值误差报告。

## Fix direction（仅限本 PR，附条件）

1. 去掉 DSpark eager 硬锁（head 上 proposer 内未见 `use_cuda_graph=False`）；缓冲按 `B×(1+N)`。
2. `get_graph_query_num_tokens` 统一注册/捕获/update 的 Query-T（K3：`graph_num_reqs * num_query_per_req`）。
3. `pad_query_start_loc_for_graph`；padding `slot_mapping=-1`；`num_actual_tokens` 限真实 query。
4. MLA vs DSA 长度契约；K3 FULL 保留 corrected seq lenses。
5. `_is_k3_dspark`：**专用分支**门控，不是总 eager 开关（见 topic VERIFIED）。

**条件**: head `debfa9bd48fefcc90added07b28540daee8b5435`；WIP + conflicts；合入后需重读。

## Do-not-overgeneralize

- 禁止假设其它 NPU/CANN/版本等价。
- 禁止把注释里的「非 K3 eager」当成已由 `_is_k3_dspark` 强制实现。
- 禁止把长度契约说成浮点精度修复。
- `mla_v1.py` 中间改动不在聚合 diff 净结果中。
- UT ≠ 生产 ACL Graph 已验证。

## Evidence links

- PR: https://github.com/vllm-project/vllm-ascend/pull/16157
- Compare: https://github.com/vllm-project/vllm-ascend/compare/fb2820b9b6598f888d7dc56260c03d3352cc08e9...debfa9bd48fefcc90added07b28540daee8b5435
- Head `dspark_proposer.py`: https://github.com/vllm-project/vllm-ascend/blob/debfa9bd48fefcc90added07b28540daee8b5435/vllm_ascend/spec_decode/dspark_proposer.py
- Head `llm_base_proposer.py`: https://github.com/vllm-project/vllm-ascend/blob/debfa9bd48fefcc90added07b28540daee8b5435/vllm_ascend/spec_decode/llm_base_proposer.py
- Docs ACL Graph: https://docs.vllm.ai/projects/ascend/en/latest/developer_guide/Design_Documents/ACL_Graph.html
- Docs speculative decoding: https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/speculative_decoding.html

## Retrieval queries

1. `Kimi K3 dspark ACLGraph FULL Query T padding slot_mapping=-1`
2. `fix double N parallel_draft_seq_lens_cpu MLA DSA vllm-ascend`
3. `_is_k3_dspark vs use_cuda_graph _use_aclgraph FULL path`
4. `set_draft_graph_params DSpark FULL capture_descs get_graph_query_num_tokens`
5. `PR 16157 vllm-ascend base fb2820b9 head debfa9bd`
