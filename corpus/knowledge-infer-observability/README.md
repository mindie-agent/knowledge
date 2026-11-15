# `knowledge-infer-observability` Domain Mirror

**Scope**: Steady-state NPU trace capture gates and simulator-versus-hardware gap attribution.

| Unit ID | Tier | Category |
| --- | --- | --- |
| `unit-cann9-steady-state-capture-and-clock-hygiene` | L1 | measurement |
| `unit-mindie-sim-capture-diff-trace-attribution-workflow` | L3 | workflow |

`unit-cann9-steady-state-capture-and-clock-hygiene` documents the implemented `SteadyStateGate` contract (warmup steps, window length, parts-per-million CV bound, and zero-allocation enforcement) rather than an unimplemented MAD/P50 filter. `unit-mindie-sim-capture-diff-trace-attribution-workflow` inherits that L1 measurement axiom and decomposes observed-versus-predicted step time using step-clipped device-wide interval unions and signed replay deltas. In `mode="explore"`, the L3 attribution workflow is stripped while its L1 measurement foundation remains mounted.
