# mindie-knowledge 0.13.11: Unified Single-State RSI Loop Engine, Higher Graded Retrieval Budgets & Superseded Service Retirement

[中文](https://github.com/mindie-agent/knowledge/blob/v0.13.11/docs/releases/knowledge-0.13.11.md) · **English**

- **Unified single-state RSI ledger and loop engine (`graded/experiment_ledger.py` / `graded/rsi_loop.py` / `graded/explore.py`)**: Consolidate `ExperimentLedger` as the single authoritative state store, simplify `RSILoopEngine` to the three `orient` / `propose` / `verdict` primitives, preserve round order and baseline reconciliation across `superseded` verdicts and historical record retries, and surface `active_rsi_session` directly in `explore` responses.
- **Raise graded retrieval default token budgets and ceiling (`graded/context_manager.py` / `graded/explore.py` / `loop/cli.py` / `service/server.py`)**: Increase default `token_budget` to `24000` for `explore`, `8000` for `query`, and `16000` for `explain`, with the maximum budget ceiling raised to `64000`.
- **Retire stale replaced service owners during `service replace` (`loop/cli.py` / `loop/systemd_service.py`)**: Validate and safely retire superseded `state=replaced` service owners and launch records (`service-launch.json`) so stale replaced processes no longer block service replacement (`mindie-agent-codex#85`).
