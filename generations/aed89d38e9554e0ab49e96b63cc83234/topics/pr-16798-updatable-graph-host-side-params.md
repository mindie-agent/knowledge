# Topic: ACL Graph UpdatableGraph host-side params（MRV1/MRV2）

- **日期**: 2026-09-20（修订）；初建 2026-09-18
- **关联 case**: [`cases/pr-16798-updatable-graph-host-side-params.case.md`](../cases/pr-16798-updatable-graph-host-side-params.case.md)
- **PR**: https://github.com/vllm-project/vllm-ascend/pull/16798
- **Pins**: base `b255ab59…` · head `9f4ac9d1…` · OPEN BLOCKED；+650/−877，18 files

## 要点

- **2026-09-20 VERIFIED**：rebase 到新 base；head tip `fix nightly`；仍 BLOCKED；文件面含 `updatable_graph.py` / attention 大删改。
- **CLAIM**：FIA/SpecDecode 接到 UpdatableGraph；A3/A5 手测矩阵。
- **UNVERIFIED**：本轮 NPU e2e。

## 勿过度推广

- blocked ≠ 可合入；勿与 draft #16468 DSpark ACLGraph 混 pin。

## Retrieval queries

1. UpdatableGraph ACL Graph host-side Ascend
2. PR 16798 MRV1 MRV2 FIA speculative decoding
3. compilation updatable_graph issue 13058
