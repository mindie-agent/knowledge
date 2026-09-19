# Case: 310P MTP+ACLGraph 捕获期 D2H / aclrtMemcpy 107027

- **日期**: 2026-09-14
- **状态**: CLOSED unmerged / reference-only
- **来源 PR**: https://github.com/vllm-project/vllm-ascend/pull/15645 （CLOSED，mergedAt null；mergeable MERGEABLE 但 mergeStateStatus BLOCKED；labels: `module:tests`）
- **Pinned（本轮委托证据）**: base `b1c539250d505d223dfb90a3c96f699f0deab364` · head `6730f85fd0fcde8e59f4d208ba45aa7f4041c382`

## 修订说明 / Revision notes（2026-09-14，Asia/Shanghai）

- 新建 case：CLOSED unmerged #15645；closedAt 2026-09-03；**勿**当作已合入 main。
- 证据分层：body+diff 中可核对的修复路径标 VERIFIED；PR 症状叙述与「910 A2/A3 不变」标 CLAIM/边界；生产 CANN/torch-npu、合入去向、ci-gate fail 含义标 UNVERIFIED。
- 与 #15816 共享 host splitfuse mask / 107027 拒 D2H 模式，但 **分案记录**，不合并。
- **310P ≠ 910 A2/A3**：不得把本修复外推为其它 Ascend 代际等价。

## Trigger — 何时想起本 case

在 **vllm-ascend** **Ascend 310P**、**MRv1**（`VLLM_USE_V2_MODEL_RUNNER=0`）、**Qwen3.5** hybrid **FULL_DECODE_ONLY + MTP** 打开 **ACLGraph** 时：

- 捕获期出现 `aclrtMemcpy` **107030** / 「Not allow to synchronize captured-stream」**107027**；
- MTP dummy/verify `q_len=1+K` 映射到 310P splitfuse；
- 讨论 uncompressed NZ mask 是否在 Python forward 内做 query_start_loc/seq_lens **D2H**，或 GDN flatten 是否对 device `seq_lens` 做 `torch.all`（同步捕获流）。

## Preconditions / environment signals

- 仓库：`vllm-project/vllm-ascend`；base `main` @ `b1c539250d505d223dfb90a3c96f699f0deab364`；head `310p_mtp_fix` @ `6730f85fd0fcde8e59f4d208ba45aa7f4041c382`。
- 硬件路径：**310P** + **MRv1** `NPUModelRunner310`；body 称无 FIA / 无 splitfuse_v2（**边界 CLAIM**，未在本轮独立证明 910 路径）。
- Graph：ACLGraph capture；MTP K（e2e 测例 K=1，capture sizes `[1,2]`）。
- Attention：`AttentionMaskBuilder310` / `[redacted:credential-high-entropy]`；GDN：`gdn_310.py`。

## Observed failure pattern（若有）

- **[CLAIM from PR body]** Ascend 310P MRv1、Qwen3.5 hybrid FULL_DECODE_ONLY+MTP 在 ACLGraph capture 中死机：`aclrtMemcpy` 107030 / Not allow to synchronize captured-stream 107027。
- **[VERIFIED from body+diff]** MTP dummy/verify `q_len=1+K` → 310P splitfuse；此前 uncompressed NZ mask 在 Python forward 内对 query_start_loc/seq_lens 做 D2H，被 MRv1 `ACLGraphWrapper` 录进图；GDN flatten 对 device `seq_lens` 用 `torch.all`（同步捕获流）。

## Fix direction（仅限本 PR，附条件）

1. 在 `[redacted:credential-high-entropy].build()` **图外**用 host `query_lens_cpu` / `seq_lens_cpu` 构建 splitfuse NZ mask。
2. `_bind_splitfuse_mask`：稳定 NPU buffer，capture/replay 同一 `data_ptr`。
3. `forward_chunked_prefill_310` 消费 `attn_metadata.attn_mask`；capturing 且 mask 缺失 / 需要 D2H 时 **raise**。
4. `AttentionMaskBuilder310._host_query_lens` / `_host_seq_lens`：拒绝 NPU D2H，`RuntimeError` 文案提及 **107027**。
5. GDN `_flatten_state_indices`：用 shape 检查 `total_tokens == num_seqs * q_per_seq`，捕获期不用 device lengths 上的 `torch.all`。
6. `build()` 中将 `PrefillCacheHit` 加入 `splitfuse_states`。
7. e2e：`test_qwen3_5_mtp_tp1_full_decode_only_graph`（`VLLM_USE_V2_MODEL_RUNNER=0`，FULL_DECODE_ONLY，mtp K=1，capture `[1,2]`）。

**条件**: head `6730f85fd0fcde8e59f4d208ba45aa7f4041c382`；**CLOSED unmerged**；合入形态未知；ci-gate **fail**。

## Do-not-overgeneralize

- 禁止把 310P MRv1 修复当作 910 A2/A3 MTP 已验证变更（body 称不变 → **CLAIM/边界**）。
- 禁止假设 FIA / splitfuse_v2 / MRv2 路径已覆盖（见 #15816 另案）。
- CLOSED ≠ 已 soft-land elsewhere；合入去向 **UNVERIFIED**。
- UT/e2e 文件存在 ≠ 本轮已跑真机；生产 CANN/torch-npu **UNVERIFIED**。

## Evidence links

- PR: https://github.com/vllm-project/vllm-ascend/pull/15645
- Compare: https://github.com/vllm-project/vllm-ascend/compare/b1c539250d505d223dfb90a3c96f699f0deab364...6730f85fd0fcde8e59f4d208ba45aa7f4041c382
- Related CLOSED/OPEN sibling (MRv2): https://github.com/vllm-project/vllm-ascend/pull/15816
- Docs ACL Graph: https://docs.vllm.ai/projects/ascend/en/latest/developer_guide/Design_Documents/ACL_Graph.html

## Retrieval queries

1. `310P MTP ACLGraph aclrtMemcpy 107027 D2H query_start_loc seq_lens`
2. `[redacted:credential-high-entropy] host query_lens_cpu splitfuse NZ mask`
3. `AttentionMaskBuilder310 _host_query_lens refuse NPU D2H 107027`
4. `GDN _flatten_state_indices total_tokens vs torch.all device seq_lens`
5. `PR 15645 vllm-ascend CLOSED unmerged 310p_mtp_fix 6730f85`
