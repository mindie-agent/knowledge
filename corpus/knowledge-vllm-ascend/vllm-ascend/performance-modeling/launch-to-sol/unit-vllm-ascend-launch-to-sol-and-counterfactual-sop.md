---
category: sop
conditions:
  accelerator: ascend
  cann: '>=9.0,<10'
  deployment:
  - colocated
  - pd_disaggregated
  execution_mode: aclgraph
  framework:
  - vllm-ascend>=0.10,<0.12
  model_family:
  - deepseek-v3
  - deepseek-v4
  soc:
  - Ascend910B
  - Ascend910B4
  - Ascend910_93
  - Ascend950
  workload_phase:
  - prefill
  - decode
domain_path: vllm-ascend/performance-modeling/launch-to-sol
evidence_strength: hypothesis
id: unit-vllm-ascend-launch-to-sol-and-counterfactual-sop
inherits_from:
- unit-vllm-ascend-hbm-memory-hidden-assassins-axioms
provenance:
  evidence_refs:
  - nr_s1_launch_sol@worktree-snapshot-20261114:mindie_sim/feature_reports.py
  source_digest: 6f7b7424d88920ed3dfb539f635dbc4d3d01cc36b0fae744856b99318854c1f9
  source_locator: nr_s1_launch_sol@worktree-snapshot-20261114:mindie_sim/feature_reports.py
  source_type: simulator_cli_and_report_contract
revision: 7
schema: mindie-graded-unit/1
status: active
supersedes: []
tier: L2
title: Launch-to-SOL and feature deltas with gated mindie-sim surfaces
triggers:
  error_signatures:
  - inspect-launch reports an unsupported flag instead of silently defaulting it
  - both enable_dsa_cp and pcp_size are set on a sparse-attention launch
  - sol predicts a host bound while the capture shows a cube-bound step
  - feature deltas are multiplied although counterfactuals_additive is false
  keywords:
  - mindie-sim inspect-launch
  - mindie-sim sol
  - feature-deltas
  - launch-to-sol
  - counterfactual
  - mrv2
  - pcp_size
  - dcp_size
  - enable_dsa_cp
  - all2all_backend
  - compilation_config
  - roofline
---
# Launch-to-SOL and feature deltas with gated mindie-sim surfaces

## Capability Gate

`inspect-launch`, `sol`, and `feature-deltas` are version-gated `mindie-sim` subcommands that may be absent from older builds. Probe availability first via `mindie-sim sol --help >/dev/null 2>&1 || echo unavailable`. When unavailable, fall back to `mindie-sim compose`, `mindie-sim predict <case> --scope body --phase decode`, and `mindie-sim efficiency <case>` with explicit manual launch normalization, and never fabricate CLI flags or output keys.

## Normalize Launch Facts and Solve SOL

```bash
mindie-sim inspect-launch launch.sh --input-format shell \
  --framework-version 0.11.0 --output facts.json
mindie-sim sol launch.sh --phase decode --scope model --tokens 1 \
  --context 4096 --sequences 32 --dispatch ideal_zero --output sol.json
```

`inspect-launch` emits `launch-parse/1` containing `status`, `source` (`name`/`revision`/`sha256`), `applied`, `neutral_operational`, `unmodeled_or_unsupported`, `required_inputs`, `case`, `coverage`, and `diagnostics`. Unrecognized flags land in `unmodeled_or_unsupported` and mark the parse partial rather than silently defaulting; always pin `source.sha256`.

`sol` emits `sol-comparison/1` with `context`, `bound` (`status`, `dispatch_policy`, `critical_path_ms`, `resource_load_ms`, `sol_ms`, `resources`, `local_bounds`), `prediction` (`predicted_ms`, `includes_host_submission`), `efficiency_vs_sol_pct`, `gap_evidence`, `counterfactuals`, and `diagnostics` (`--scope body` selects `body_step`; `--scope model` selects `model_step`). Read `bound.sol_ms = max(critical_path_ms, resource_load_ms)` and `prediction.predicted_ms` directly (the schema does not emit `t_math`, `t_comm`, `t_host`, `t_step`, `mfu`, or `hbm_util` keys), and treat `bound_exceeds_prediction` as an invalid report rather than clipping it.

## Counterfactual Feature Deltas

```bash
mindie-sim feature-deltas launch.sh --features ACLGraph,MLAPO,MTP,PCP/DCP \
  --phase decode --output cf.json
```

Each `Counterfactual` entry reports `feature`, `status`, `predicted_ms`, `sol_ms`, `saved_ms`, `sol_saved_ms`, and `reason` alongside top-level `counterfactuals_additive` (no `delta_ms`, `delta_pct`, `direction`, or `interaction_residual` keys exist). Rank same-baseline rows by signed `saved_ms`—retaining negative regressions—and evaluate explicit pairwise combinations whenever `counterfactuals_additive` is `false` instead of multiplying isolated speedups.

## PCP / DCP / DSA-CP Matrix

- Pinned `context_parallel.md@175ef4d` supports DCP (sequence-sharded KV within a TP group, adding zero extra ranks) and legacy DSA-CP; prefill context parallel is **not** supported at that commit.
- Legacy DSA-CP sets `enable_dsa_cp` with `--prefill-context-parallel-size 1`: `vllm serve <m> --tensor-parallel-size N --prefill-context-parallel-size 1 --decode-context-parallel-size N --block-size B --cp-kv-cache-interleave-size B --additional-config '{"enable_dsa_cp":true}'`. It is distinct from mrv2 PCP.
- Dual-phase `PCP+DCP` (`prefill_cp_plan=pcp`, `decode_cp_plan=dcp`) requires registered mrv2 builders (`W = PP * PCP * DP * TP`, `TP % DCP == 0`, `EP * expert_tp * expert_dp = DP * TP`). Gate activation on the runtime's reported builder capability rather than version numbers alone.

## Closed-Loop Alignment & Boundary

Compare `bound.sol_ms` against steady-state P50 on identical launch facts, capture buckets, and clock windows: gaps `<= 5%` sit near roofline, while gaps `> 10%` escalate to `unit-mindie-sim-capture-diff-trace-attribution-workflow`. Enforce the inherited HBM ledger before ranking plans, and treat modeled deltas as analytical priors until confirmed on hardware.
