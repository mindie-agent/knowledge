---
category: measurement
conditions:
  accelerator: ascend
  cann: '>=9.0,<10'
  execution_mode:
  - aclgraph
  - eager
  measurement_gear:
  - fast
  - deep
  soc:
  - Ascend910B
  - Ascend910B4
  - Ascend910_93
  - Ascend950
domain_path: measurement/steady-state/cann9
evidence_strength: hardware_spec
id: unit-cann9-steady-state-capture-and-clock-hygiene
inherits_from: []
provenance:
  evidence_refs:
  - mindie-profiler@f7dbca5e643db52807e12649e02291069f3d9d82:mindie_profiler/native_capture.py
  - mindie-profiler@f7dbca5e643db52807e12649e02291069f3d9d82:mindie_profiler/_cann9_capture_shim.c
  source_digest: 0718130f23b0414f7ffb0652495d24f705e3e03c88cf3959ae312928026f02a3
  source_locator: mindie-profiler@f7dbca5e643db52807e12649e02291069f3d9d82:mindie_profiler/native_capture.py
  source_type: profiler_gate_and_capture_receipt_contract
revision: 3
schema: mindie-graded-unit/1
status: active
supersedes: []
tier: L1
title: CANN 9 steady-state capture — implemented gate, boundaries, and clock evidence
triggers:
  error_signatures:
  - first profiled step is 2x slower because NPUGraph capture or aclrtMalloc is in
    the window
  - AIC frequency drops during capture and every kernel looks uniformly slower
  - deep-gear absolute latency disagrees with the production SLA trace
  - step time has a monotonic drift that no operator change explains
  keywords:
  - mindie-profiler capture
  - --gear fast
  - --gear deep
  - SteadyStateGate
  - min-warmup-steps
  - steady-window-steps
  - max-cv-pct
  - NPUGraph capture warmup
  - aclrtMalloc jitter
  - npu-smi clock
  - aic_frequency
  - thermal throttling
  - P50 P90
---
# CANN 9 steady-state capture — implemented gate, boundaries, and clock evidence

## Condition

Applies to CANN 9.x on Ascend 910B, A3, and A5 whenever `mindie-profiler capture` feeds `mindie-sim` alignment or candidate promotion.

## Implemented `SteadyStateGate`

Default `capture` flags are `--min-warmup-steps 5`, `--steady-window-steps 5`, `--steps 10`, `--max-cv-pct 15.0`, `require_zero_alloc_steps=true` (`--allow-alloc-churn` disables), `--replays-per-step 1`, and `--min-step-interval-us 50`. `_cann9_capture_shim.c` admits a window iff:

```text
active_graph_captures == 0
and total_steps >= min_warmup_steps + steady_window_steps
and (not require_zero_alloc_steps or no alloc within the last window)
and ring_count >= steady_window_steps
and CV(last window) = (stdev/mean) * 1_000_000 <= max_cv_ppm
```

Scale the dimensionless ratio `stdev/mean` by `1,000,000` before comparing against `max_cv_ppm` (`15.0% = 150,000 ppm`): for `[1,1,1,1,3]`, mean `1.4` and population stdev `0.8` yield `571,428 ppm`, which rightly fails `150,000` (an unscaled ratio check would falsely pass).

Receipts persist `gate_policy` (`min_warmup_steps`, `steady_window_steps`, `max_cv`, replays, zero-alloc flag), `transient_contaminated`, and per-worker window counters. Post-capture `audit_worker_steady_state` flags `incomplete_capture_steps`, `graph_capture_during_window`, `hbm_malloc_churn_during_window`, `mspti_records_dropped`, and `captured_step_cv_spike` above `1.5x` the gate CV. Note that there is no implemented MAD/P50 filter, drift gate, or thermal boolean in the shim—compute P50/P90 and drift externally over admitted samples rather than fabricating shim fields.

## Step Boundaries & Production Fidelity

Eager mode marks step boundaries on `aclrtSynchronizeStream`, while graph replay counts replays divided by `--replays-per-step`; keep that boundary sync without inserting intra-step kernel barriers. Retain recurring production work (`npu_format_cast`, `.item()`, `.cpu()`) and never drop a recurring outlier—isolate cold-start, capture, and allocation transients instead of hiding real regressions.

## Clock Hygiene & Procedure

Sample `npu-smi info -t clock|power|temperature|memory` before and after the timed window (`<= 1 Hz`), recording `unknown`/`unavailable` for unsupported telemetry rather than nominal clocks, and apply clock scaling only to calibrated compute-bound regions. Warm up all buckets first, use `--gear fast` for SLA/ranking and `--gear deep` strictly for attribution, and fix contaminated captures before tuning models.
