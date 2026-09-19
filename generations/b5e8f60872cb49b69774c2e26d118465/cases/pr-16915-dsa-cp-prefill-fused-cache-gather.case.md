# Case: DSA-CP prefill — fused cache gather + token-sharded O-proj + block-copy writes

- **日期**: 2026-09-19
- **状态**: OPEN not-draft / MERGEABLE / **BLOCKED** / reference-only
- **来源 PR**: https://github.com/vllm-project/vllm-ascend/pull/16915 （author `lrf-vm`；labels: `module:tests`, `module:ops`）
- **Pinned（本轮 gh 2026-09-19）**: base `c8addbb24fe8fa820bb79685c116e43ba89b77e9` · head `383fbad9ad2dbce36d788c8ce94edf3cba5487df`

## 修订说明 / Revision notes（2026-09-19，Asia/Shanghai）

- 新建 case；规模 +1071/−24，**15 files**；MERGEABLE **BLOCKED**。
- 文件面（VERIFIED `gh pr diff --name-only`）：`attention/cache_store.py`、`attention/context_parallel/sfa_cp.py`、`attention/indexer.py`、`attention/sfa_v1.py`、`device/hardware_profile.py`、`ops/mla.py`、`patch/worker/patch_deepseek_v2.py` + UT/e2e ops。
- 动机 CLAIM：大 DSA-CP prefill 分离 main/indexer gather、hidden gather 后再 shard、full output reduce-scatter、generic scatter cache write 成本高（profiled 8192-row INT8 ≈1.65 ms CLAIM）。
- 三向修复 CLAIM：(1) raw-bytes 一次 pack gather main KV + indexer K/scale；(2) eligible eager DSA-CP MoE backbone 保持 token-sharded 经 attention/full O-proj；(3) 大支持写入复用 block-copy（阈值≥2048；A2/A3 capability/dtype/contiguous/scratch guard；否则 fallback scatter）。
- 校验 CLAIM：5457 CPU tests pass sample；9 real Ascend **A3** NPU tests；多轮 main/fix 256/256；**本轮未复跑** → NPU 结果 **CLAIM**。
- 用户面 CLAIM：**无**新配置；MTP top-k 策略保留。

## Trigger — 何时想起本 case

讨论 **DSA-CP / SFA-CP prefill** 集体通信与 cache gather/store 开销、fused gather、token-sharded O-proj 或 block-copy vs scatter 时。

## Preconditions / environment signals

- base `main` @ `c8addbb2…`；head @ `383fbad9…`。
- 硬件门控 CLAIM：A2/A3 capability + dtype + contiguous + scratch + 2048-token threshold。
- MTP / unsupported/compiled layouts 保留既有 execution path（CLAIM）。

## Observed failure pattern（若有）

- Performance / overhead；非单一 correctness crash（作者陈述 CLAIM）。

## Fix direction（仅限本 PR，附条件）

1. Fused raw-bytes cache gather（main + indexer）。
2. Token-sharded residual/layout through attention + O-proj（eligible paths）。
3. Block-copy kernel for large supported cache writes；else scatter。

**条件**: head `383fbad9ad2dbce36d788c8ce94edf3cba5487df`；OPEN blocked；A3 手测 **CLAIM**。

## Do-not-overgeneralize

- A3 9-test 子集 ≠ 全卡型/全拓扑；A2 guard ≠ A5 已验。
- Prefill 优化 ≠ decode 路径等价收益。
- 勿与 #16656 SFA DCP LSE 数值修复混 pin。

## Evidence links

- PR: https://github.com/vllm-project/vllm-ascend/pull/16915
- Compare: https://github.com/vllm-project/vllm-ascend/compare/c8addbb24fe8fa820bb79685c116e43ba89b77e9...383fbad9ad2dbce36d788c8ce94edf3cba5487df
- Workflow sample（CLAIM in body）: https://github.com/vllm-project/vllm-ascend/actions/runs/35391287307

## Retrieval queries

1. DSA-CP prefill fused cache gather Ascend
2. PR 16915 SFA CP block-copy scatter threshold
3. token-sharded O-proj DSA-CP MoE backbone
4. indexer main KV raw-bytes gather MTP
5. A3 NPU DSA-CP prefill overhead reduction
