# [参考] vllm-ascend PR #15645：310P MTP+ACLGraph 捕获期 D2H / 107027

- **日期**: 2026-09-14
- **状态**: CLOSED unmerged / reference-only（非正确性背书；不以 merge / 热度 / 生成摘要当证据）
- **阅读方式**: 本轮委托提供的 PR 元数据 + body/diff 信号（未跑 NPU；未改上游）

## Pinned（本轮委托证据）

| 字段 | 值 |
|---|---|
| PR URL | https://github.com/vllm-project/vllm-ascend/pull/15645 |
| 标题 | `[BugFix][310P] fix server crash opening MTP+ACLGraph on the 310P` |
| 状态（读到时） | **CLOSED**；`mergedAt=null`（**unmerged**）；labels: `module:tests` |
| closedAt | 2026-09-03（委托） |
| base 分支 | `main` |
| head 分支 | `310p_mtp_fix` |
| **base SHA** | `b1c539250d505d223dfb90a3c96f699f0deab364` |
| **head SHA** | `6730f85fd0fcde8e59f4d208ba45aa7f4041c382` |
| mergeable | MERGEABLE / mergeStateStatus **BLOCKED** |
| 作者 | `Thiagor2002` |
| 规模 | +182 / −19，**7 files** |
| 对比永久链接 | https://github.com/vllm-project/vllm-ascend/compare/b1c539250d505d223dfb90a3c96f699f0deab364...6730f85fd0fcde8e59f4d208ba45aa7f4041c382 |

### CI / checks（委托）

| Check | 结论 |
|---|---|
| DCO | **pass** |
| PR create | **pass** |
| pre-commit | **pass** |
| ci-gate | **fail** |

## 修订说明 / Revision notes（2026-09-14，Asia/Shanghai）

1. 新建 topic：记录 CLOSED unmerged #15645 的 310P MRv1 MTP+ACLGraph D2H/107027 修复意图与 diff 可核对点。
2. **不确定性**：合入 blocked 后关闭；最终是否落地其它分支 **UNVERIFIED**；ci-gate fail 含义 **UNVERIFIED**。
3. 与 #15816 交叉引用（共享 host mask / 107027 守卫模式）；**不**合并为一案。
4. **310P ≠ 910 A2/A3**。

## Problem / symptoms（有证据分层）

1. **[CLAIM from PR body]** 310P MRv1 `NPUModelRunner310`、Qwen3.5 hybrid FULL_DECODE_ONLY+MTP，ACLGraph capture 崩溃：`aclrtMemcpy` 107030 / Not allow to synchronize captured-stream 107027。
2. **[VERIFIED from body+diff]** MTP dummy/verify `q_len=1+K` 映射 splitfuse；uncompressed NZ mask 曾在 Python forward 内 D2H `query_start_loc`/`seq_lens`（被图录制）；GDN flatten 对 device `seq_lens` `torch.all` → 同步捕获流。

## Control-flow / diff analysis（head `6730f85fd0fcde8e59f4d208ba45aa7f4041c382`）

### VERIFIED from body+diff signals

1. **Host-side splitfuse NZ mask**：`[redacted:credential-high-entropy].build()` 用 `query_lens_cpu` / `seq_lens_cpu` 在图外构建。
2. **`_bind_splitfuse_mask`**：稳定 NPU buffer，capture/replay 保持同一 `data_ptr`。
3. **`forward_chunked_prefill_310`**：消费 `attn_metadata.attn_mask`；capturing 且缺 mask / 需 D2H → raise。
4. **`AttentionMaskBuilder310._host_query_lens` / `_host_seq_lens`**：拒 NPU D2H，`RuntimeError` 提及 107027。
5. **GDN `_flatten_state_indices`**：shape 契约 `total_tokens == num_seqs * q_per_seq`，替代捕获期 device `torch.all`。
6. **`PrefillCacheHit`** 加入 `splitfuse_states`（`build()`）。
7. **e2e**：`test_qwen3_5_mtp_tp1_full_decode_only_graph` — `VLLM_USE_V2_MODEL_RUNNER=0`，FULL_DECODE_ONLY，mtp K=1，capture sizes `[1,2]`。

### CLAIM / boundary（代码 alone 或本轮未独立证明）

1. 作用域：**310P MRv1**（无 FIA / 无 splitfuse_v2）；**910 A2/A3 MTP 不变** — 作边界 CLAIM，除非另证。
2. 症状错误码与「server crash」叙述来自 PR body。

### UNVERIFIED / open questions

1. 生产 CANN / torch-npu 版本组合。
2. CLOSED 后修复是否在其它 PR/分支 soft-land。
3. ci-gate **fail** 的具体原因与是否阻塞合入。
4. 本轮未跑 UT/E2E/NPU。

## What the PR changes（文件级，据委托）

| 文件域 | 角色 |
|---|---|
| `attention_mask.py` | host lens 守卫 / 拒 D2H |
| `attention_v1.py` | forward 消费预绑定 mask；capturing 缺 mask raise |
| `metadata_builder.py` | 图外 host CPU lens → splitfuse NZ；`_bind_splitfuse_mask`；PrefillCacheHit |
| `gdn_310.py` | flatten 用 shape 检查替代 device `torch.all` |
| tests… | e2e FULL_DECODE_ONLY + MTP graph |

## Applicability boundaries

**较明确适用（仍需目标环境复验）**

- `vllm-ascend` 310P **MRv1** MTP + ACLGraph；Qwen3.5 hybrid FULL_DECODE_ONLY 语境（测例命名）
- head `6730f85fd0fcde8e59f4d208ba45aa7f4041c382` 上的上述守卫逻辑

**不应外推**

- 910 A2/A3；FIA / splitfuse_v2；MRv2（见 #15816）
- CLOSED unmerged ≠ 生产已部署

## Verification scope

- 本轮：委托 body+diff 信号；**未**运行 UT/E2E/NPU。
- Checks：DCO/PR create/pre-commit pass；ci-gate fail。

## Related

- Case: [`cases/pr-15645-310p-mtp-aclgraph-d2h-107027.case.md`](../cases/pr-15645-310p-mtp-aclgraph-d2h-107027.case.md)
- Sibling MRv2 MTP: https://github.com/vllm-project/vllm-ascend/pull/15816 · [`topics/pr-15816-310p-mrv2-mtp-adaptation.md`](pr-15816-310p-mrv2-mtp-adaptation.md)
- ACL Graph docs: https://docs.vllm.ai/projects/ascend/en/latest/developer_guide/Design_Documents/ACL_Graph.html

## Retrieval queries

1. `vllm-ascend PR 15645 310P MTP ACLGraph 107027 D2H`
2. `splitfuse NZ mask query_lens_cpu seq_lens_cpu graph capture`
3. `_bind_splitfuse_mask stable data_ptr capture replay`
4. `test_qwen3_5_mtp_tp1_full_decode_only_graph VLLM_USE_V2_MODEL_RUNNER=0`
5. `310P MRv1 NPUModelRunner310 vs 910 A2 A3 MTP boundary`

## Explicit uncertainty markers

- `[CLOSED-UNMERGED]` mergedAt null；BLOCKED。
- `[CLAIM-SYMPTOM]` 107030/107027 现场来自 PR body。
- `[BOUNDARY-310P-MRV1]` 910 不变未独立证明。
- `[UNVERIFIED-RUNTIME]` 无 NPU 实测；ci-gate fail 未解。
- `[[redacted:ticket-id]]` 共享模式，分案。
