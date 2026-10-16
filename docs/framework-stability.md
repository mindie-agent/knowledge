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

On Windows, if a reader process holds an open handle to a configuration file without delete-sharing, Python's standard `os.replace` can fail with `PermissionError` or cause readers to observe partial writes.

To provide POSIX-grade atomic configuration updates on Windows, `mindie-knowledge` core and all plugin bootstrap helpers share a unified implementation:
1. **Delete-Sharing Reads**: Every internal reader opens configuration files with `FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE`;
2. **Native Atomic Rename**: Writers flush the complete new document to a temporary file in the same directory and atomically replace the target using native Win32 rename semantics.
- **Result**: Active readers finish reading the complete old document while new readers immediately see the complete new document—with zero lock contention or partial JSON reads (see [Windows `_FILE_RENAME_INFORMATION` semantics](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information)).

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
