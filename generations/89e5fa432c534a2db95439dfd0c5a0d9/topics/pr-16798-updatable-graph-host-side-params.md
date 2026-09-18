# Topic: ACL Graph UpdatableGraph / host-side params（MRV1/MRV2）

- **日期**: 2026-09-18
- **关联 case**: [`cases/pr-16798-updatable-graph-host-side-params.case.md`](../cases/pr-16798-updatable-graph-host-side-params.case.md)
- **PR**: https://github.com/vllm-project/vllm-ascend/pull/16798
- **Pins**: base `3db3f931…` · head `3e33c9fd…` · OPEN BLOCKED

## 要点

- **VERIFIED**：新 `updatable_graph.py` + acl_graph / worker / spec_decode 文件面；+642/−878 / 18 files；cpu-ut/pre-commit pass sample。
- **CLAIM**：实现 issue #13058；A3/A5 手测矩阵；无 user-facing change。
- **UNVERIFIED**：全覆盖 e2e / 生产收益；本轮未跑 NPU。

## 勿过度推广

- 手测场景 ≠ 全拓扑；勿与 #16468 draft ACLGraph 集成混 pin。

## Retrieval queries

1. UpdatableGraph host-side ACL Graph Ascend MRV1 MRV2
2. PR 16798 FIA speculative decoding acl_graph refactor
3. issue 13058 UpdatableGraph design vllm-ascend
