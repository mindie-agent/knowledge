# Case: DSA local token metadata — fixed-capacity Triton（消除逐步 re-JIT）

- **日期**: 2026-09-17（修订）；初建 2026-09-16
- **状态**: OPEN not-draft / MERGEABLE / **BLOCKED** / reference-only
- **来源 PR**: https://github.com/vllm-project/vllm-ascend/pull/16629 （author `zhangxiaoshanha`；labels: `module:tests`, `module:ops`）
- **Pinned（本轮 gh 2026-09-17）**: base `714dd1d1ba032972e6a734254a816ca13c302da1` · head `cefa37aba3d1b33cbac9dfc7fbd56812855a2dea`

## 修订说明 / Revision notes（2026-09-17，Asia/Shanghai）

- **重 pin head**：`70540b56…` → `cefa37aba3d1b33cbac9dfc7fbd56812855a2dea`（base 仍 `714dd1d1ba032972e6a734254a816ca13c302da1`）。
- 相对上轮 tip 增量（VERIFIED）：`[Test] Fix implicit string concat flagged by pre-commit formatter`（`cefa37ab…`）；此前还有 review fix drop OOB dummy write（`202fad6b…`）。
- 规模 +368/−25，**3 files**；仍 MERGEABLE **BLOCKED**。
- 保留旧对比：https://github.com/vllm-project/vllm-ascend/compare/714dd1d1ba032972e6a734254a816ca13c302da1...70540b56fd5ff19314292376429cfaf9cd7fe625
- 新对比：https://github.com/vllm-project/vllm-ascend/compare/714dd1d1ba032972e6a734254a816ca13c302da1...cefa37aba3d1b33cbac9dfc7fbd56812855a2dea
- re-JIT 收益仍 **CLAIM**。

## 修订说明 / Revision notes（2026-09-16，Asia/Shanghai）

- 新建 case；与 #16542（`build_dspark_swa_indices` Triton fuse）同属 DSA Triton 固定容量思想，但 **分案**：本 PR 针对 `_build_local_token_metadata` / CP local metadata。
- 性能动机（re-JIT 秒级 vs ~1µs kernel）→ **CLAIM**；本轮未复跑。
- CI：ci-gate / pre-commit **fail**（VERIFIED 摘要）；DCO/PR create/main **pass**。

## Trigger — 何时想起本 case

讨论 DSA context-parallel **local token metadata** 每步随 `num_reqs` 变化触发 Triton re-specialize，或对比 #16542 fixed-capacity indices 模式时。

## Preconditions / environment signals

- base `main` @ `714dd1d1…`；head @ `cefa37ab…`。
- 文件（VERIFIED）：新 `ops/triton/dsa_local_metadata.py`；`attention/context_parallel/dsa_cp.py`；nightly UT `test_build_local_metadata_triton.py`。
- 旧路径 CLAIM：`ops/triton/dsa_cp.py` 的 `build_local_metadata_triton` 以 live request count 定 BLOCK。

## Observed failure pattern（若有）

- Performance / compile-bound（非 crash）：`num_reqs` bucket 变化 → 重新 JIT（PR CLAIM）。

## Fix direction（仅限本 PR，附条件）

1. 固定 launch shape 到 scheduler `max_num_seqs`（default 512）；runtime mask + `do_not_specialize`。
2. 单 program `grid=(1,)`；超出 `num_reqs` 的 lane 写零；去掉 host `fill_(0)` pre-pass（CLAIM）。
3. 融合 clamp → inclusive-cumsum → `local_seq_lens` → optional `start_pos`；in-kernel cumsum via 2D column-major view。
4. `num_reqs==0` host short-circuit（对齐 `resample.py` 先例 CLAIM）。
5. Ascend lowering CLAIM：int32→fp32 for clamp/compare（910B int32 min/max/compare → AiCPU）；token offsets < 2^24 exact roundtrip；`start_pos` 留 int32。

**条件**: head `cefa37aba3d1b33cbac9dfc7fbd56812855a2dea`；OPEN blocked。

## Do-not-overgeneralize

- Fixed-capacity metadata ≠ #16542 SWA indices kernel。
- 910B lowering 叙述 ≠ 全卡型等价。
- 禁止把 CLAIM 编译耗时当本轮实测。

## Evidence links

- PR: https://github.com/vllm-project/vllm-ascend/pull/16629
- Compare（本轮）: https://github.com/vllm-project/vllm-ascend/compare/714dd1d1ba032972e6a734254a816ca13c302da1...cefa37aba3d1b33cbac9dfc7fbd56812855a2dea
- Compare（2026-09-16 tip 永久）: https://github.com/vllm-project/vllm-ascend/compare/714dd1d1ba032972e6a734254a816ca13c302da1...70540b56fd5ff19314292376429cfaf9cd7fe625
- Related: https://github.com/vllm-project/vllm-ascend/pull/16542
- Head blob: https://github.com/vllm-project/vllm-ascend/blob/cefa37aba3d1b33cbac9dfc7fbd56812855a2dea/vllm_ascend/ops/triton/dsa_local_metadata.py

## Retrieval queries

1. DSA local_token_metadata fixed-capacity Triton re-JIT 16629
2. dsa_local_metadata max_num_seqs do_not_specialize
3. build_local_metadata_triton dsa_cp context parallel
4. PR 16629 vs 16542 Triton capacity grid Ascend
5. int32 fp32 clamp cumsum 910B AiCPU metadata
