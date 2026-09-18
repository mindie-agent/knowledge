# Case: ACL Graph UpdatableGraph — host-side parameter update refactor (MRV1/MRV2)

- **日期**: 2026-09-18
- **状态**: OPEN not-draft / MERGEABLE / **BLOCKED** / reference-only
- **来源 PR**: https://github.com/vllm-project/vllm-ascend/pull/16798 （author `zhiyu-wa`；labels: `module:tests`, `module:core`, `ready-all`）
- **Pinned（本轮 gh 2026-09-18）**: base `3db3f931e155da9b496febe8a42d9bd7d9f52849` · head `3e33c9fdda0578787544fd4360e48ae2fed1dc4b`

## 修订说明 / Revision notes（2026-09-18，Asia/Shanghai）

- 新建 case；Refs design issue #13058（CLAIM 关系）。
- 规模 +642/−878，**18 files**；MERGEABLE **BLOCKED**。
- 文件面（VERIFIED `gh pr diff --name-only`）：`compilation/updatable_graph.py`（新）、`compilation/acl_graph.py`、attention/spec_decode/worker v1+v2 aclgraph 路径、相关 UT。
- 作者手测 CLAIM：A3（FIA/MTP/DSV4-Flash-W8A8）；A5（FIA/MTP/PA gemma4/Kimi-K2.5）× MRV1/MRV2；匹配 vLLM `84030bbe…`。
- CI 摘要（VERIFIED sample）：pre-commit/PR create/cpu-ut **SUCCESS**；多项 selected e2e 仍 pending/skip → overall **BLOCKED**。
- 用户面：**无** user-facing change（PR CLAIM）。

## Trigger — 何时想起本 case

讨论 **ACL Graph** 捕获后 host-side 参数更新、`UpdatableGraph`、FIA/SpecDecode 与 MRV1/MRV2 图复用时。

## Preconditions / environment signals

- base `main` @ `3db3f931…`；head @ `3e33c9fd…`。
- 主题：把 FIA / Speculative Decoding 接到 `UpdatableGraph`（issue #13058 CLAIM）。
- 勿与 #16726（曾 MERGED revert ACL Graph host-side — 昨日 scan）混为一谈：本 PR 为正向重构。

## Observed failure pattern（若有）

- Feature / design refactor；非单一 crash 报告。

## Fix direction（仅限本 PR，附条件）

1. 引入 `UpdatableGraph` 并重构 `acl_graph` / worker v2 aclgraph utils（VERIFIED 文件面）。
2. 适配 FIA + SpecDecode（dflash/eagle/step3p5/dspark proposer 等路径 CLAIM）。
3. 同步 UT（`test_acl_graph.py` 等）。

**条件**: head `3e33c9fdda0578787544fd4360e48ae2fed1dc4b`；OPEN blocked；手测矩阵 **CLAIM**。

## Do-not-overgeneralize

- 手测 A3/A5 子集 ≠ 全模型 / 全卡型已验证。
- blocked ≠ 可合入；勿把 CLAIM 验收当本轮 VERIFIED NPU。
- 勿与 draft #16468 的 DSpark ACLGraph 实验集成混 pin。

## Evidence links

- PR: https://github.com/vllm-project/vllm-ascend/pull/16798
- Design issue: https://github.com/vllm-project/vllm-ascend/issues/13058
- Compare: https://github.com/vllm-project/vllm-ascend/compare/3db3f931e155da9b496febe8a42d9bd7d9f52849...3e33c9fdda0578787544fd4360e48ae2fed1dc4b

## Retrieval queries

1. UpdatableGraph ACL Graph host-side parameter update Ascend
2. PR 16798 MRV1 MRV2 FIA speculative decoding acl_graph
3. vllm-ascend compilation updatable_graph issue 13058
4. ACL Graph capture replay host params FIA MTP
5. A3 A5 manual validation UpdatableGraph blocked
