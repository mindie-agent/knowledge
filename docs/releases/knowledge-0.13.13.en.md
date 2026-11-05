# mindie-knowledge 0.13.13: Automatic RSI `reuse -> explore` Promotion, Candidate Hash Deduplication & C6 Distillation Hardening

[中文](https://github.com/mindie-agent/knowledge/blob/v0.13.13/docs/releases/knowledge-0.13.13.md) · **English**

- **First-Query `reuse` Guarantee & `candidate_sha256`-Deduplicated Automatic Promotion (`graded/index.py`)**:
  - Guarantees that the first `workspace_gated` `knowledge_query` in a workspace serves `L1–L3` units under `effective_mode="reuse"` before evaluating automatic promotion;
  - Deduplicates refuted (`verdict="refuted"`) ledger entries in `logs/experiment-ledger.jsonl` by distinct `candidate_sha256` / `candidate_hash` values so repeated runs of the same candidate cannot inflate failure counts; once `>= 3` distinct candidate hashes are refuted, the subsequent `knowledge_query(mode="reuse")` automatically promotes to `effective_mode="explore"` (`auto_switched_from_reuse=True`) and mounts `L4` Mutator units;
  - Records `requested_mode`, `effective_mode`, `auto_switched_from_reuse`, `caller_initiated_explore`, `observed_candidate_hashes`, and `explore_disabled` in `knowledge_query_audit.jsonl`.
- **Physical `L4` Isolation under `MINDIE_DISABLE_RSI_EXPLORE=1` (`graded/index.py`)**:
  - Forces `effective_mode="reuse"`, strips all `L4` units, and suppresses `held_back_l4_ids` when `MINDIE_DISABLE_RSI_EXPLORE=1` is set for A/B control runs (`G2`).
- **Non-C4 `L4` Ablation Receipt Normalization for C6 Distillation (`graded/distill.py`)**:
  - Normalizes `full_candidate` and leave-one-out (`ablate_*`) entries from `logs/experiment-ledger.jsonl` into `c4_verification` receipts so `graded-c6` Stop distillation seamlessly admits verified multi-factor repairs into the local Git repository.
- **Unreadable Material Isolation (`#113`)**: Isolates single unreadable material entries during query execution.
- **16-Trial End-to-End RSI A/B Benchmark Verification (`track-RSI-E2E-v4`)**:
  - Verified across 2 hosts (Codex `gpt-6.1-sol xhigh` and DSH `deepseek-flash max`) × 4 Ascend 950DT (A5) controlled contract tasks under strict single-variable parity: Control `G2` `0/8 (0.0%)` vs Treatment `G4` `8/8 (100.0%)`, with `8/8` automatic `reuse -> explore` promotions (`caller_initiated_explore=False`, `>= 3` distinct refuted candidate hashes) and `8/8` `graded-c6` local Git commits (`completed:applied`), independently audited by Codex `gpt-6-astra (max)`.
