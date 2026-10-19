# Framework Stability & Verification Boundaries

[中文](framework-stability.zh.md) | **English** · [Back to Documentation Center](README.en.md)

This guide summarizes the stability mechanisms built into `mindie-knowledge` for everyday use: deterministic transcript parser isolation, self-healing ReMe derived indexes, non-blocking atomic configuration updates on Windows, and the clear boundary between component fixture tests and live host acceptance.

---

## 1. Everyday "Zero-Ceremony" Stability

- **Configure Once, Run Effortlessly**: The first time you invoke the native entry after installation (`/mindie-agent` in Claude Code / Kimi or `$mindie-agent` in Codex), your contribution preference is saved in `mindie-consent/1` and reused across future tasks, branches, restarts, and updates without manual re-activation. Community contribution consent and diagnostic crash reporting maintain separate, independent choices.
- **Corrupt Config Is Never Treated as First-Time Onboarding**: If a shared configuration or `mindie-consent/1` file is damaged by an external tool, `mindie-knowledge` reports an explicit fault and preserves existing state—never wiping user data or re-triggering first-time onboarding.

---

## 2. Isolating Internal Frameworks

`mindie-knowledge` confines internal parsing, indexing, and retrieval libraries to stateless or rebuildable boundaries so third-party state can never corrupt authoritative data:

| Component | Role in `mindie-knowledge` | Isolation & Stability Guarantee |
| --- | --- | --- |
| **Host Transcript Parsers** | Project visible user & assistant messages from Claude / Codex / Kimi transcripts | Verified against pinned production parser fixtures (`tests/fixtures/production-parsers`). Malformed or unrecognized transcript records fail visibly rather than storing raw JSON as material. |
| **ReMe Retrieval Library** | Provides local Markdown chunking, citation graph construction, and BM25 lexical scoring | Used strictly as an in-process library—**spawning zero background daemons, file watchers, or external network requests**. If a derived index cache is damaged or upgraded, it rebuilds cleanly from authoritative Markdown files and SQLite checkpoints. |
| **LangMem (`0.0.30`)** | Generates per-block titles, summaries, and task navigation outlines | Invoked purely as a stateless batch indexing function over the host's native small-model route; its built-in graph stores and background services are unused, and all attempt state is governed by `SummaryLedger`. |

---

## 3. Atomic Configuration Reads & Writes on Windows

Readers copy each complete document and close its handle before parsing.
Writers serialize updates under the consent or community lock, flush a complete
temporary file in the same directory, then publish it with `os.replace`.
On Windows, a concurrent reader can temporarily prevent replacement. Only
WinError 32 is retried, for at most five seconds; other errors remain immediate.
A persistent holder produces a visible error while the old document stays
intact. Writers never truncate the live document or silently discard a choice.

---

## 4. Consumer Lock Observation & Verification Boundaries

- **Reliable `consumer.lock` Liveness Detection**: The background knowledge service holds the OS-level `consumer.lock` until it has closed both its RPC listener and SQLite store. `lock_held` probes the OS lock directly without rewriting brittle PID metadata—allowing startup to detect a released service immediately even on Windows, where connecting to a closed loopback port can briefly time out before returning `ConnectionRefusedError`.
- **Deterministic Test Suite vs. Live Host Acceptance**:
  ```bash
  # Run the complete unit and integration test suite from the repository root
  MINDIE_FRAMEWORK_SOURCE=tests/fixtures/production-parsers python3 -m pytest -q tests
  ```
  - When `MINDIE_FRAMEWORK_SOURCE` is unset, `tests/conftest.py` uses the committed `tests/fixtures/production-parsers` directory and verifies all three parser files exist; passing an invalid explicit path exits immediately as a setup error rather than falling back to a global installation.
  - Repository `pytest` suites verify protocol contracts, fault injection, concurrency locks, and subprocess tree reclamation with zero model calls, while end-to-end validation inside live Claude Code / Codex / Kimi hosts and on Ascend NPU hardware is tracked separately as release acceptance evidence.
