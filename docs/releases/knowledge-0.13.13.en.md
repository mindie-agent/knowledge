# mindie-knowledge 0.13.13: Automatic RSI `reuse -> explore` Promotion, Candidate Hash Deduplication & C6 Distillation Hardening

[中文](https://github.com/mindie-agent/knowledge/blob/v0.13.13/docs/releases/knowledge-0.13.13.md) · **English**

- **First-Query `reuse` Guarantee & `candidate_sha256`-Deduplicated Automatic Promotion (`graded/index.py`)**:
  - The first `knowledge_query` of a gated workspace stays on `effective_mode="reuse"` and serves `L1–L3`. The floor counts only that workspace's own audit (`logs/knowledge_query_audit.jsonl` records whose `workspace_scope` matches). A shared `profile/` audit does not;
  - `MINDIE_BENCH_WORKSPACE` reads only `<workspace>/logs/experiment-ledger.jsonl` and does not fall through to the process cwd. Without that variable, gating applies only when a ledger file exists at `cwd/logs` or a sibling `workspace/logs` or `work/logs`. A directory named `workspace` with no ledger does not disable `explore`;
  - Refuted rows are deduped by `candidate_sha256` / `candidate_hash`, otherwise `source_digest`. After `>= 3` distinct refuted hashes since the latest progress, the next `knowledge_query(mode="reuse")` promotes to `explore` (`auto_switched_from_reuse=True`). `mode="diagnose"` stays diagnose;
  - The audit records `requested_mode`, `effective_mode`, `auto_switched_from_reuse`, `caller_initiated_explore`, `observed_candidate_hashes` (the refuted hashes that count), `distinct_candidate_hashes`, `workspace_scope`, and `explore_disabled`.
- **Physical `L4` Isolation under `MINDIE_DISABLE_RSI_EXPLORE=1` (`graded/index.py`, `graded/closure.py`)**:
  - Forces `effective_mode="reuse"`. Query results and inherited premises omit L4 bodies and L4 ids. `knowledge_explain` does not expand L4 ancestors; a directly requested L4 ref is `not_found` and its body is omitted. `held_back_l4_ids` is not populated.
- **C6 Still Requires a Verified Ablation Receipt (`graded/distill.py`)**:
  - Raw `full_candidate` / `ablate_*` lines in `logs/experiment-ledger.jsonl` are not synthesized into `c4_verification` receipts, and an L4 proposal that lacks a verified `mindie-bench-ablation/1` or `/2` receipt is rejected rather than rewritten to L2 and committed.
- **Unreadable Material Isolation (`#113`)**: Isolates single unreadable material entries during query execution.
- **16-Trial End-to-End RSI A/B Benchmark Verification (`track-RSI-E2E-v4`)**:
  - Verified across 2 hosts (Codex `gpt-6.1-sol xhigh` and DSH `deepseek-flash max`) × 4 Ascend 950DT (A5) controlled contract tasks under strict single-variable parity: Control `G2` `0/8 (0.0%)` vs Treatment `G4` `8/8 (100.0%)`, with `8/8` automatic `reuse -> explore` promotions (`caller_initiated_explore=False`, `>= 3` distinct refuted candidate hashes) and `8/8` `graded-c6` local Git commits (`completed:applied`), independently audited by Codex `gpt-6-astra (max)`.
