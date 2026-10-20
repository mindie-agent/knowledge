# MindIE Knowledge (`mindie-knowledge`) — Agent Architecture & Operating Guide

`mindie-knowledge` owns domain knowledge retrieval, deterministic transcript redaction, incremental LangMem indexing, and consent-gated Git/GitHub community contribution for MindIE Agent.

---

## 1. Submodule Map

- **Top-Level Authorities & Retrieval (`mindie_knowledge/`)**:
  - [`owned_state.py`](mindie_knowledge/owned_state.py): Authoritative SQLite opener (`open_database`, WAL mode, `5.0s` busy timeout, schema verification without silent rebuild on damage).
  - [`state_layout.py`](mindie_knowledge/state_layout.py): Domain state directory layout (`state-v4/state-v4.sqlite3`) and release-version migration guard.
  - [`consent_store.py`](mindie_knowledge/consent_store.py): Single profile-level contribution and diagnostics authority (`mindie-consent/1`, `choice_revision`, cross-process `.lock` with `5.0s` wait, atomic `os.replace`). `record_choice` is the only choice writer; `activate_reporting` derives the diagnostics policy and verifies native worker readiness. Legacy choices without a revision require an explicit unified choice and never silently authorize diagnostics upload.
  - [`redact.py`](mindie_knowledge/redact.py): Deterministic regex/structural PII, credential, IP/endpoint, and path scanner (`REDACTION_PROFILE = "mindie-redaction/1"`).
  - [`retrieval.py`](mindie_knowledge/retrieval.py): Technical tokenizer (`TechnicalTokenizerV1` preserving API/version/CJK tokens) and Markdown corpus sync/search helpers.
  - [`publication_check.py`](mindie_knowledge/publication_check.py): Streaming `git ls-tree` + `git cat-file --batch` validator for community PR commits.
- **Canonical Material Store & ReMe Index (`mindie_knowledge/materials/`)**:
  - [`store.py`](mindie_knowledge/materials/store.py): Canonical Markdown package store (`MaterialStore`), candidate staging, and `current.json` dual-channel pointer promotion.
  - [`reme_index.py`](mindie_knowledge/materials/reme_index.py): Embedded ReMe chunker (`VerifiedMarkdownFileChunker`), file graph (`StrictFileGraph`), and BM25 lexical index (`StrictBM25Index`).
  - [`summarizer.py`](mindie_knowledge/materials/summarizer.py): Bounded LangMem incremental block-indexing contract (`mindie-summary-request/1`, `mindie-summary-outcome/1`) and `SummaryLedger`.
  - [`ingest.py`](mindie_knowledge/materials/ingest.py) & [`publication.py`](mindie_knowledge/materials/publication.py): Deterministic 16 KB block splitter and frozen outbox batch staging.
- **Capture, Indexing & Service Loop (`mindie_knowledge/loop/`)**:
  - [`activation.py`](mindie_knowledge/loop/activation.py): Per-session task lease and idempotent event-attempt store (`admission.sqlite3`, `mindie-admission/1`).
  - [`handoff.py`](mindie_knowledge/loop/handoff.py): Fast Stop-hook capture admission (`commit_capture`) and single-flight detached service wake (`WAKE_PROBE_SECONDS = 2.0`).
  - [`store.py`](mindie_knowledge/loop/store.py): `state-v4.sqlite3` (`mindie-store/4`) transaction coordinator for `entries`, `captures`, `cursors`, `material_streams`, `material_batches`, `votes`, and `outbox`.
  - [`engine.py`](mindie_knowledge/loop/engine.py): Background orchestrator (`Engine`) driving `mindie-capture`, `mindie-summary`, and `mindie-outbox` loops under `OperationOwner` cancellation scopes.
  - [`transcript_capture.py`](mindie_knowledge/loop/transcript_capture.py) & [`transcript_redaction.py`](mindie_knowledge/loop/transcript_redaction.py): Incremental transcript capture, pinned Gitleaks `8.30.1` + regex/HMAC redaction (`REDACTION_TIMEOUT_SECONDS = 30.0`), and single-batch summary worker execution (`MINDIE_SUMMARY_TIMEOUT_SECONDS = 315.0`; `summary_budget.native_timeout()` shares the remaining parent budget and reserves `5s` for cleanup).
  - [`settings.py`](mindie_knowledge/loop/settings.py): Shared community contribution config (`mindie-community-config/1`) re-read at every gate.
- **Outbound Community Publication (`mindie_knowledge/community/`)**:
  - [`publish.py`](mindie_knowledge/community/publish.py): Idempotent Git + GitHub PR state machine (`submit_batch`, `reconcile_batch`) backed by [`ledger.py`](mindie_knowledge/community/ledger.py).
  - [`batch.py`](mindie_knowledge/community/batch.py), [`gitops.py`](mindie_knowledge/community/gitops.py), [`transport.py`](mindie_knowledge/community/transport.py): Outbound privacy scan, local Git staging repo operations, and GitHub REST transport.

---

## 2. Core Data Contracts & Invariants

1. **Canonical Markdown Material Packages (`MaterialStore`)**:
   - Durable task bodies live exclusively in ordinary Markdown files under `materials/`:
     - `tasks/<64-hex-task_id>/index.md`: YAML frontmatter (`schema: mindie-material-task/1`, `entry` with `schema: mindie-entry/3`, `material_digest`, `revision`, ordered `blocks` descriptors, `navigation`, `status`) + generated navigation Markdown body.
     - `tasks/<64-hex-task_id>/blocks/<64-hex-block_id>.md`: Immutable `<= 16 KB` (`MAX_BLOCK_BYTES = 16384`) UTF-8 redacted source blocks (`schema: mindie-material-block/1`) bound by `sha256` and `source_range`.
   - **Two-Phase Pointer Promotion (`current.json`)**:
     - New revisions stage under `candidates/<revision>/` first; SQLite commits queue/header metadata inside `Store._write_txn()`; `MaterialStore.retain_current(task_id, {"draft": rev, "feed": rev})` atomically promotes `current.json` pointers and retires unreferenced candidate directories.
     - `.reme-index/` is a disposable derived BM25 + file-graph checkpoint bound to `catalog.generation`; it never stores authoritative state that cannot be rebuilt from `current.json` and `tasks/`.
2. **Incremental Transcript Capture & Redaction**:
   - Stop hooks call `handoff.handoff()` to record lightweight `captures` rows without opening transcripts or calling models.
   - `transcript_capture.capture()` reads complete transcript increments, carries open PEM private-key state (`{'private_key': ...}`) across chunk boundaries, unions Gitleaks + `redact.scan_text` + private path spans, and commits redacted blocks with `model_calls=0`.
3. **External Write Receipts (`completed` / `failed` / `unknown`)**:
   - **Paid LLM Indexing (`SummaryLedger`)**: `material_summary_attempts` records `prepared -> invoking -> returned / failed / outcome_unknown`. Once `returned` is persisted via `on_result=save_result`, any subsequent local redaction or file-promotion failure records `local_failure` and retries local application only—never replaying the LLM call. If the worker crashes or times out after spawn without `not_started` proof, the attempt stays `outcome_unknown` until explicit operator retry.
   - **GitHub PR Publication (`community/publish.py` & `Ledger`)**: Records `intent` and step receipts before each external Git push or GitHub API write. Interrupted writes stay `unknown` and resolve strictly via read-only reconciliation (`_resolve_unknown`). If a remote write succeeds (`submitted` / `updated`) and the local receipt update fails, the succeeded remote result is preserved on the error (`mindie_publication_result` / `recording_failed=True`) rather than overwritten as failed.

---

## 3. Timeout & Anti-Over-Defensive Rules (`v0.0.2` Four Iron Rules)

1. **Lightweight SQLite & Lock Discipline**:
   - `open_database` configures `PRAGMA journal_mode=WAL` and `timeout=5.0s` once per connection; `ConsentError(state="locked")` and `StartLock` use bounded waits (`5.0s`, `WAKE_PROBE_SECONDS = 2.0`). Never run per-`SELECT` `.owner` `lstat` loops or `:memory:` schema reflection on hot paths (`_declared_columns` is `@lru_cache`d).
2. **Reserve `unknown` Strictly for External Writes — No Local Self-Bricking**:
   - Only external paid LLM invocations (`SummaryLedger`) and remote Git/GitHub mutations (`Ledger` / `outbox`) enter `unknown` / `outcome_unknown` reconciliation.
   - Local transient failures (`GateFault`, `ScannerUnavailable`, `AdmissionUnavailable`) park work with bounded backoff (`30s`) and recover automatically when the local dependency/authority is restored; consecutive failure counters in `Admission` are diagnostic only and never self-brick a session lease.
3. **No Cross-Repo Fallback Imports or `ctypes` ACL/DPAPI Ceremonies**:
   - Do not import internal helpers from sibling repos (e.g., `mindie_diagnostics.fallback`). Use standard `Path.mkdir(mode=0o700)` and atomic `os.replace()`.
   - Do not reintroduce ancestor `is_symlink()` directory-walking loops on temp/test paths or `ctypes` Win32 ACL / DPAPI ceremonies.
4. **Module Header Contract & Pre-PR Review**:
   - Core modules maintain a top-of-file docstring declaring `[Role]`, `[Invariants]`, `[Timeouts & Locks]`, and `[Side-Effect Policy]`.
   - Before submitting or updating any PR, complete the [MindIE Agent Pre-PR Review Checklist](../docs/design-principles.md#三开发-pr-提交前自查清单pre-pr-review).

---

## 4. Verification & Test Commands

Run tests using the repository's isolated Python environment:

```bash
# Fast core unit & component contract tests (excludes standing live NPU tests)
python -m pytest -q -k "not live_npu"

# Domain retrieval precision/recall & summary quality gate
python -m pytest -q tests/test_domain_retrieval_and_summary_quality.py

# Standing live A2 (173.125.1.2) / A3 (192.168.13.165) NPU knowledge loop integration test
python -m pytest -q tests/test_live_npu_knowledge_loop.py
```
