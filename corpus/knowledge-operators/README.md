# `knowledge-operators` Domain Mirror

**Scope**: Ascend ACLGraph bucket-padding semantics, named operator ABI binding, `FRACTAL_NZ` physical storage, and standalone single-operator reproduction.

| Unit ID | Tier | Category |
| --- | --- | --- |
| `unit-ascend-aclgraph-bucket-padding-and-varlen-axioms` | L1 | axiom |
| `unit-ascend-op-abi-binding-and-single-op-repro-sop` | L2 | sop |

`unit-ascend-aclgraph-bucket-padding-and-varlen-axioms` defines the operator-level padding classes and variable-length (`varlen`) tensor invariants under ACLGraph capture. `unit-ascend-op-abi-binding-and-single-op-repro-sop` restates the essential length and storage legality rules inline and cites the L1 padding axiom by pinned revision (`@3`) rather than `inherits_from`, keeping outward single-query delivery well inside the 3,000-token budget while remaining self-contained for standalone operator reproduction.
