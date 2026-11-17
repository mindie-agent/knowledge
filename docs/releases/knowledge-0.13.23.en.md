# mindie-knowledge 0.13.23: Pure Knowledge Domain Convergence, `mindie-harness` RSI Bridges, `< 1ms` Lazy Import, and Substrate Inventory

[中文](knowledge-0.13.23.md)

`mindie-knowledge v0.13.23` converges `mindie-knowledge` onto its pure graded-knowledge domain boundary and updates its `mindie-diagnostics` pin to `v0.5.16`:

- **Pure Knowledge Boundary & Backward-Compatible `mindie-harness` RSI Forwarding**: Delegates host-neutral `ExperimentLedger` and `RSILoopEngine` execution to `mindie-harness` (`mindie_harness.rsi`) via thin compatibility shims, and converts top-level `mindie_knowledge` imports to `< 1ms` PEP 562 lazy loading.
- **`mindie-harness` Runtime Inventory & Handoff Race Hardening**: Extends `runtime_inventory` and `opencode` release metadata support for seamless old/new updater interoperability, closes the `consumer.lock` creation TOCTOU race in `loop/handoff.py`, tolerates self-closing stale owner sockets in `loop/cli.py`, and eliminates Windows workflow self-cancellation.
- **Dependency Pin & `state-service` Governance**: Updates `mindie-diagnostics` to `v0.5.16`, keeps all core modules `< 800` lines, and enforces `--profile state-service` PR governance.
