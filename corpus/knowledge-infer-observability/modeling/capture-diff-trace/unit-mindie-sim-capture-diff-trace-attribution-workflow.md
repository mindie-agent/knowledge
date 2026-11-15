---
category: workflow
conditions:
  accelerator: ascend
  cann: '>=9.0,<10'
  execution_mode: aclgraph
  framework:
  - vllm-ascend>=0.10,<0.12
  measurement_gear: deep
  soc:
  - Ascend910B
  - Ascend910B4
  - Ascend910_93
  - Ascend950
  workload_phase:
  - decode
  - prefill
domain_path: modeling/capture-diff-trace
evidence_strength: hypothesis
id: unit-mindie-sim-capture-diff-trace-attribution-workflow
inherits_from:
- unit-cann9-steady-state-capture-and-clock-hygiene
provenance:
  evidence_refs:
  - knowledge-infer-observability@4b3dd86b365c9e588a1eb47df7dcc6437fdf8a89:measurement/msprof/overlap/unit-msprof-analyze-step-overlap-clock-and-intervals.md
  - next-release-design@2026-11-14:v2-pillars-4-to-7-design.md#15.3
  source_digest: 6077d6a1f70cc9cce0abb55b01a1c4a6367516e9bae36973011c4cbefe306ad4
  source_locator: next-release-design@2026-11-14:v2-pillars-4-to-7-design.md#15.3
  source_type: simulator_attribution_contract
revision: 3
schema: mindie-graded-unit/1
status: active
supersedes: []
tier: L3
title: Four-way diff-trace — signed replay, device-wide intervals, and one-bucket
  ownership
triggers:
  error_signatures:
  - mindie-sim sol predicts 20 ms but the steady-state capture is 27 ms on the same
    launch facts
  - stream idle gaps align with Python or ATen dispatch on Kunpeng ARM64
  - CombineV3 time tracks the slowest rank rather than the modeled bytes
  - every operator is uniformly slower and the AIC clock is below nominal
  keywords:
  - mindie-sim diff-trace
  - prediction gap
  - graph lowering delta
  - host dispatch delta
  - Kunpeng ARM64
  - collective congestion
  - MoE routing skew
  - eta_skew
  - per-op roofline
  - repro-op pointer
  - interval union
---
# Four-way diff-trace — signed replay, device-wide intervals, and one-bucket ownership

## Capability Gate

`mindie-sim diff-trace` is a version-gated subcommand; check availability via `mindie-sim diff-trace --help >/dev/null 2>&1 || echo unavailable`. When present, run `mindie-sim diff-trace --prediction prediction.json --capture capture.json` without undocumented flags; otherwise extract step windows via `mindie-profiler analyze --root <capture> --step-window <STEP=START:END> --output step-analysis.json` (`inventory`, `correlate`, `kernels`) and evaluate the replay stages manually.

## Sequential Replay & Single-Bucket Conservation

Align one launch digest, bucket set, non-contaminated `--gear deep` steady window, rank group, and clock domain:

```text
R0 = baseline prediction
R1 = replay with captured graph/lowering structure
R2 = R1 with captured critical host release/bubble behavior
R3 = R2 with captured collective contention, routing/EPLB and KV-transfer behavior
O  = observed wall duration of the same statistic/window
d_graph = R1 - R0;  d_host = R2 - R1;  d_comm = R3 - R2;  d_op = O - R3
O = R0 + d_graph + d_host + d_comm + d_op
```

Stage deltas are signed (negative speedups are valid) and order-dependent rather than Shapley-invariant. Assign every event to exactly one bucket: added/split/cast ops to `d_graph`, host-release gaps to `d_host`, collective/straggler/KV-transfer delays to `d_comm`, and matched kernel service deltas to `d_op`—never charging an inserted `npu_format_cast` to both graph and per-op.

## Device-Wide Interval Unions

Clip all stream intervals to `[step_start, step_end)` and measure busy time over the union across all device streams rather than summing per-stream durations:

```text
host_bound_ratio = |union(NPU-idle AND causally host-ready)| / step_time
```

A stream waiting while a sibling stream executes is not host starvation; require host-ready dispatch evidence (`aclrtLaunchKernel`, `c10::`, Python frames, or blocking D2H) before attributing `d_host`. Empty windows yield an undefined ratio (never `0`), and unsynchronized cross-rank clocks mark communication attribution `partial`.

## Reconciliation & Quality Gate

```text
reconcile   = O - (R0 + d_graph + d_host + d_comm + d_op)   # timestamp-resolution check
R4_model    = R3 + roofline_clock_explained
unexplained = O - R4_model
limit       = max(abs_noise_floor, rel * max(|O|, |R0|, sum|d|))
```

`reconcile` verifies timestamp precision while `limit` gates `unexplained` against `max(|O|, |R0|, sum|d|)` so near-zero or canceling signed deltas cannot mask poor modeling.

## Bucket Triage & Boundary

- **Graph (`> 20% |O|`):** Eliminates unexpected `Cast`, `Transpose`, `Contiguous`, `npu_format_cast`, fallbacks, or unfused splits.
- **Host (`> 15% |O|`):** Removes causally verified host-dispatch bubbles.
- **Comm (`> 20% |O|` or `eta_skew > 1.1`):** Separates link congestion (`measured / (alpha_us + beta_ns_per_byte * bytes / 1000)`) from routing skew (`max_rank_tokens / mean_rank_tokens`) on non-overlapped critical time.
- **Per-op:** Ranks kernels by `t_measured - t_roofline(clock_corrected)` and hands top offenders to `unit-ascend-op-abi-binding-and-single-op-repro-sop`.

If trace coverage is incomplete or `|unexplained| > limit`, repair instrumentation before tuning kernels. `attribution.json` records signed `buckets[]`, `host_bound_ratio`, `eta_skew`, `congestion_factor`, `clock_snapshot`, and `repro_op_refs` as diagnostic attribution rather than proof of a fix.
