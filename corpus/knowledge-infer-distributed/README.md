# `knowledge-infer-distributed` Domain Mirror

**Scope**: Multi-dimensional parallelism feasibility, MoE routing skew, speculative decoding crossover, and disaggregated xPyD serving capacity.

| Unit ID | Tier | Category |
| --- | --- | --- |
| `unit-vllm-ascend-parallel-ep-knee-eplb-and-xpyd-workflow` | L3 | workflow |

Because the per-rank HBM memory ledger resides in `knowledge-vllm-ascend`, this workflow cites `mindie://vllm-ascend/units/unit-vllm-ascend-hbm-memory-hidden-assassins-axioms@2` explicitly in its body and enforces `hbm_ledger_checked: true` in `conditions` rather than declaring an unresolvable cross-repository `inherits_from` ID. Applicability further requires an observed `mrv2_pcp` runtime capability and dual-phase `pcp`/`dcp` context-parallel plans; builds matching `context_parallel.md@175ef4d` (which support DCP and legacy DSA-CP only) cleanly skip this workflow until those builders are registered.
