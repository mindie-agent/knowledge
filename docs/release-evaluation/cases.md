# Case inventory

Defined in `tests/acceptance_release/cases.py`. Sources are repo-public files or
isolated fixtures. None of these is synthetic NPU/Windows evidence.

| ID | Model? | Distinguishes | Source |
| --- | --- | --- | --- |
| migration-mcp-stdio | no | Public spec maps to knowledge | `corpus/references/mcp-stdio-newline-delimited-jsonrpc.md` |
| migration-910b4-peaks | no | Theoretical peaks stay conditioned | `corpus/references/ascend910b4-cann-9.0.0-platform-config-peaks.md` |
| migration-conflict-versions | no | Conflicting CANN conditions both kept | isolated `fixtures/notes/gate-v1` vs `gate-v2` |
| migration-unmappable-and-private | no | Unmappable reported; private unpublished | isolated notes |
| migration-feed-layout | no | topics/cases/maintenance split | isolated `fixtures/feed/` |
| no-hit | no | No invented hit | `tests/fixtures/retrieval-evaluation.json` query `nonexistent_operator_zz991` |
| version-inapplicable | no | CANN 8 must not apply CANN 9.0.0 knowledge | packaged 910B4 peaks |
| hit-unused | no | Lexical hit without `knowledge_use` | retrieval fixture vs unused peak table |
| consumption-echo | no | Consumer is not a new producer/vote | `Store.add` after `use` |
| abc-protocol | no | A→B→judge→C ledger exists; effect unknown | loop tests / local Store |
| success-no-contribution | **yes** | Success ≠ contribution | cites `tools/knowledge-intake/ACCEPTANCE-2026-09-13.md` as historical tool evidence, not NPU |
| failure-hypothesis-excluded | **yes** | Failed task, hypothesis correctly dropped | authored aclgraph/eager fixture text |
| abc-effect | **yes** | Independent C with original artifacts | Codex only |
| negative-repeat-correction | **yes** | unknown / negative / later correction | Codex only |

Windows column in the runner is `not_verified` or `historical_record_not_reverified`.
NPU column is `not_executed_this_round` or `not_applicable`.
