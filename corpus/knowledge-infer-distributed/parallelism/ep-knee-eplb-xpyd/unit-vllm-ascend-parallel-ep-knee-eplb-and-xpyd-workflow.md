---
category: workflow
conditions:
  accelerator: ascend
  cann: '>=9.0,<10'
  decode_cp_plan: dcp
  deployment: pd_disaggregated
  execution_mode: aclgraph
  framework:
  - vllm-ascend>=0.10,<0.12
  hbm_ledger_checked: true
  parallel_plan:
    dcp: 4
    dp: 2
    ep: 16
    pcp: 2
    pp: 1
    tp: 8
  prefill_cp_plan: pcp
  runtime_capability: mrv2_pcp
  soc:
  - Ascend910_93
  - Ascend950
  topology: a3_superpod
  workload_phase: decode
domain_path: parallelism/ep-knee-eplb-xpyd
evidence_strength: hypothesis
id: unit-vllm-ascend-parallel-ep-knee-eplb-and-xpyd-workflow
inherits_from: []
provenance:
  evidence_refs:
  - next-release-design@2026-11-14:v2-pillars-4-to-7-design.md#13
  - vllm-project/vllm-ascend@175ef4d49c2072f9233619636f0309f4f88b4e2c:docs/source/developer_guide/Design_Documents/context_parallel.md
  source_digest: 6077d6a1f70cc9cce0abb55b01a1c4a6367516e9bae36973011c4cbefe306ad4
  source_locator: next-release-design@2026-11-14:v2-pillars-4-to-7-design.md#13
  source_type: analytical_parallelism_and_serving_model
revision: 4
schema: mindie-graded-unit/1
status: active
supersedes: []
tier: L3
title: Joint DP/TP/EP/PCP/DCP feasibility, EP knee, EPLB, MTP, and xPyD sizing
triggers:
  error_signatures:
  - EP64 decode latency is worse than EP32 even though per-rank expert compute fell
  - CombineV3 waits on one rank while all ranks moved the same bytes
  - enabling EPLB adds HBM and rebalance jitter without improving P99 TPOT
  - MTP improves TPOT at small batch but regresses at large batch
  - disaggregated P99 TPOT jitter correlates with KV transfer traffic
  keywords:
  - EP knee
  - EPLB
  - num_redundant_experts
  - routing skew
  - eta_skew
  - xPyD
  - PCP
  - DCP
  - DSA-CP
  - MTP crossover
  - goodput
  - expert-byte amortization
  - fabric-probe
---
# Joint DP/TP/EP/PCP/DCP feasibility, EP knee, EPLB, MTP, and xPyD sizing

## Scope

Apply this workflow after verifying per-rank memory feasibility against `mindie://vllm-ascend/units/unit-vllm-ascend-hbm-memory-hidden-assassins-axioms@2` and before committing a parallel plan or disaggregated xPyD pool ratio. Calibrate every parameter on the target fabric and workload.

## Stage 0 — Algebraic Plan Feasibility

```text
W = PP * PCP * DP * TP
EP * expert_tp * expert_dp = DP * TP;   EP <= DP * TP;   (DP * TP) % EP == 0
E % EP == 0                              # E routed experts; redundant R separate
TP % DCP == 0 and DCP <= TP
H_q % TP == 0 and H_kv % TP == 0         # or explicit KV-head replication
PCP divides W and prefill sequence work
```

Worked check (`PP=1, DP=2, TP=8, PCP=2, DCP=4, EP=16, expert_tp=expert_dp=1, E=64`): `W=32`, `EP=16 = DP*TP`, `E % EP == 0`, and `TP % DCP == 0` pass, whereas `EP=64` fails `64 > 16`. `DCP` subdivides `TP` with zero extra ranks; during decode, `PCP` lanes either remap as decode replicas or stay parked (excluded from throughput but still charged in cost and HBM).

- **Legacy DSA-CP:** Sets `PCP=1, DCP=1` under `enable_dsa_cp` with its dedicated TP query-split / full-KV / all-gather builder; it is neither mrv2 PCP nor a dual-phase plan.
- **mrv2 PCP:** `prefill_cp_plan=pcp` (`decode_cp_plan=none`) requires registered attention builders declaring query/KV/index halos and separate `pcp_index_k_gather` / `_pcp_kv_gather` payloads (`sum_r A_r = A` over exact admissible pairs rather than dense FLOPs `/ PCP`).
- **Dual-phase `PCP+DCP`:** Coexists only when both builders are registered; keep `dsa_cp+dcp` gated until a layout bridge is verified.
- **DCP:** Shards valid KV across TP ranks and merges `(m_r, z_r, u_r)` via `m = max m_r`, `z = sum exp(m_r - m) z_r`, `u = sum exp(m_r - m) u_r` (`u / z`), charging online log-sum-exp reductions rather than full-KV all-gather.
- **Evidence gate:** Pinned `context_parallel.md@175ef4d` supports DCP and legacy DSA-CP only; gate mrv2 PCP on reported runtime builders.

## Stage 1 — EP Straggler Knee & EPLB Placement

```text
T_step       = T_attn + T_expert_straggler + T_comm + T_non_moe
T_attn       = max(Cube_attn / P_cube, KV_bytes / B_hbm, TP_allreduce)
n            = B * top_k;   mean = n / EP
eta_skew     = max_r n_r / mean          # balanced = 1; n = 0 => null
T_expert_str = eta_skew * mean_service   # never (1 + eta_skew) * mean_service
```

Prefer captured expert histograms over Dirichlet-multinomial surrogates. For EPLB with `R = sum_e(replica_count_e - 1)` redundant experts, solve integer assignments `x_(e,j) >= 0` (`sum_j x_(e,j) = n_e`) minimizing max-rank service time; never scale skew by `eta_skew * E/(E+R)` (which can dip below `1`, e.g. `1.1 * 64/80 = 0.88`), and include extra weight HBM plus periodic rebalance overhead `c/I` in P99.

## Stage 2 — MTP Yield & Batch Crossover

```text
p_j = P(first j draft tokens accepted), 1 >= p_1 >= ... >= p_K >= 0
g   = 1 + sum_{j=1..K} p_j              # g = sum_{j=0..K} a^j for independent a
TPOT_MTP(B,K) = T_cycle(B,K) / g;   reversal where T_cycle(B,K) >= g * T_cycle(B,0)
```

Account full `B*(K+1)` verification rows plus draft, host, collective, and rollback cost. Under linear `T_cycle = T_fixed + (alpha + beta*K)*B`, the crossover batch `B* = T_fixed*(g-1) / (beta*K - alpha*(g-1))` is positive iff `beta*K > alpha*(g-1)` (sweep the actual batch grid rather than assuming monotonicity; e.g. at `K=2, a=0.8, g=2.44`, `B=16` drops `100 -> 73.77 us/token` whereas `B=128` regresses `300 -> 327.87 us/token`).

## Stage 3 — xPyD Pool Sizing in Completed Requests/s

Prefix hits shrink prefill compute (`Q = ISL - hit_tokens`) while decode retains the full prompt plus generated tokens:

```text
KV_D = ISL + OSL                 # per active request before page-rounding/sharding
mu_P = 1 / T_prefill_request;   mu_D = B_D * g / (OSL * T_D)
ceiling = min(N_P * mu_P, N_D * mu_D),  require rho < 1 and SLO admission
```

`KV_D` equals `ISL + OSL` (e.g., `32768 + 128 = 32896` positions, never `128`), sharded by DCP/TP with separate `index_k`, recurrent state, and once-counted shared prefixes. Compare `mu_P` and `mu_D` strictly in completed requests/s—never `min(input tok/s, output tok/s)`—and keep `mu_P` finite even at `Q = 0` (`100%` cache hit). Example (`N_total=16`, 4-rank instances, `mu_P=10`, `mu_D=6`, `rho=0.9`): `(N_P,N_D) = (1,3), (2,2), (3,1)` yield `9.0, 10.8, 5.4` req/s, shifting to `(1,3)` if KV-transfer contention drops `mu_D` to `4` (`7.2 < 9.0`). Budget simultaneous P-reads, D-writes, staging, and NIC links over unique missing pages `S_transfer`, and evaluate TTFT/TPOT P99 from end-to-end samples rather than summed component P99s.

## Stage 4 — Fabric Calibration & Convergence

`fabric-probe`, `sweep-ep-knee`, and `size-xpyd` are version-gated simulator subcommands; check `<cmd> --help` first (falling back to `mindie-sim sweep`/`skew` and never passing unlisted flags such as `--duration-s`).

```text
t(bytes) = alpha_us + beta_ns_per_byte * bytes / 1000 + congestion_delay_us
```

Convert `ns` to `us` once (`bytes / 1000`), restrict piecewise linear fits to calibrated byte ranges (`out_of_range` outside), and re-probe after topology or firmware updates. Confirm candidate plans on `--gear fast` across identical buckets and clocks, attribute residuals via `unit-mindie-sim-capture-diff-trace-attribution-workflow`, and re-verify the HBM ledger after EPLB or xPyD adjustments.

## Boundary

This workflow provides analytical sizing equations rather than an unconditional guarantee; always pair it with the HBM ledger, `SteadyStateGate` captures, and live A/B verification.
