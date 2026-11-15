# `knowledge-vllm-ascend` Domain Mirror

**Scope**: `vLLM-Ascend` HBM ownership accounting, launch-to-SOL performance modeling, MLAPO ablation verification, and Day-0 decode graph optimization.

| Unit ID | Tier | Category |
| --- | --- | --- |
| `unit-vllm-ascend-hbm-memory-hidden-assassins-axioms` | L1 | axiom |
| `unit-vllm-ascend-launch-to-sol-and-counterfactual-sop` | L2 | sop |
| `unit-vllm-ascend-mlapo-prefetch-ablation-protocol` | L3 | workflow |
| `unit-ascend-day0-vector-fusion-mlapo-prefetch-multistream-mutator` | L4 | mutator |

`unit-vllm-ascend-hbm-memory-hidden-assassins-axioms` serves as the domain memory foundation. The L2 launch-to-SOL SOP, L3 ablation protocol, and L4 Day-0 mutator all declare it in `inherits_from`, guaranteeing that outward `reuse` delivery never returns a tuning or modeling rule with its HBM feasibility ledger omitted. The L3 workflow defines the full seven-arm leave-one-out ablation receipt contract, while the L4 mutator provides a compact four-factor specification—preserving the foreground activation dependency and `unsupported_prefetch_path` fallback rules—and pins the L3 protocol at revision `@3`.

In `mode="reuse"`, outward `GradedRepository` delivery returns the complete child unit plus the complete HBM parent within 2,950 tokens. In `mode="explore"`, `exploration_context` currently serializes each mounted body four times (`mounted_units`, `units`, tier lists, and the subagent brief), exceeding 3,000 tokens whenever HBM (`~680` tokens) and L4 (`~320` tokens) are combined; outward `explore` delivery therefore retains the L4 body and marks HBM `budget_excluded` until engine-side deduplication is enabled.

Routing manifests under `index/` are regenerated with `mindie_knowledge.graded.maintenance.build_routing_index`.
