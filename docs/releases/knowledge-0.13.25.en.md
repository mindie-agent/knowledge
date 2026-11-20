# mindie-knowledge 0.13.25: Out-of-the-Box Read-Only Service Startup, Pre-Persist Distillation Redaction, and Dual-Layer Windows PE GUI Guard

[中文](knowledge-0.13.25.md) | **English** · [Back to Documentation Center](../README.en.md)

`mindie-knowledge v0.13.25` enables zero-prerequisite read-only service startup out of the box, closes pre-persist scope redaction on `knowledge_distill` while keeping `graded:related:<hash>` 100% rollback-compatible with `v0.13.24`, and hardens Windows background launches with dual-layer `pyvenv.cfg` PE GUI subsystem verification.

---

## 1. Out-of-the-Box Read-Only Service Startup & `capture_mode="disabled"`

Fresh or read-only installations—before Gitleaks or a host `transcript_adapter` is installed, or in self-test / candidate checks without `admission_path`—can now start the domain knowledge service immediately for public retrieval and sync while keeping strict fail-closed gates on capture and distillation:

- **Structural Config Validation & Cold Read-Only Startup (`mindie_knowledge/loop/cli.py`, `mindie_knowledge/loop/engine.py`, `mindie_knowledge/loop/transport.py`)**: `validate_config` / `config_at`, `Engine`, `_serve()`, and `Service._identify` now support `capture_mode="disabled"` alongside omitted or `None` `redactor_executable`, `transcript_adapter`, and `admission_path` (while still rejecting non-`None` relative or non-`.py` paths). Public `knowledge_query`, `knowledge_explain`, local `knowledge_feedback` (`publishable=False`), and `knowledge_sync` run out of the box with zero `admission.sqlite3` files created.
- **Consent-Decoupled Route Upgrades (`mindie_knowledge/loop/domain_catalog.py`)**: Keeping `validate_config` purely structural on the configuration dictionary allows `domain_catalog.prepare` and `domain_catalog.retire_changed` to read and upgrade existing read-only domain routes cleanly after a user switches consent from `read-only` to `local-only` or `contribute`. `_serve()` also defers missing `.py` `transcript_adapter` loading via `transcript_adapter_path` until capture is enabled.
- **Fail-Closed Capture & Distillation Enforcement (`mindie_knowledge/loop/handoff.py`, `mindie_knowledge/loop/transcript_increment.py`)**: `Stop` admission and wake (`accept_stop`, `request_wake`), background capture processing (`Engine._process_capture`), and `knowledge_distill` strictly enforce `validate_capture_config`, active capture leases, an absolute `.py` `transcript_adapter`, and an installed absolute `redactor_executable` (`gitleaks`). When capture consent is off, `Stop` returns `stage="inert"` (`reason="sharing-disabled"`) with zero faults and zero SQLite writes; when capture consent is enabled without capture prerequisites, `Stop` reports `stage="unavailable"` (`reason="configuration"`).

---

## 2. Rollback-Safe Pre-Persist Distillation Redaction (`graded:related-meta`)

Caller-supplied distillation scope metadata (`topic` or `target_signature`) is now guaranteed to pass full privacy redaction before any snapshot state touches SQLite, even when `inputs == []`:

- **Scope Redaction on Empty Inputs (`mindie_knowledge/graded/distill.py`)**: `prepare_request` now invokes `redact_tree` (Gitleaks + `r4` + private-path masking) whenever `selected or history or scope` is non-empty, covering `inputs == []` calls carrying `topic` or `target_signature` and failing closed before any SQLite write if the scanner is missing or unavailable.
- **Redact-Before-Commit & Companion Metadata Key (`mindie_knowledge/graded/worker.py`)**: `worker.run` resolves the candidate snapshot in memory (`_load_related_snapshot`) and runs `prepare_request` *before* opening the write transaction to commit `graded:related:<hash>` (`related_units`, `rejected_history`, `_history_scope`). Pre-redaction `history_scope_digest` and merged `redaction_rules` are stored under the companion key `graded:related-meta:<hash>`, keeping `graded:related:<hash>` 100% rollback-compatible with `v0.13.24` while preserving deterministic `request_digest` values across cache hits and concurrent writers.

---

## 3. Dual-Layer Windows `pyvenv.cfg` PE GUI Guard

On Windows, a virtual environment's `Scripts\pythonw.exe` shim (`Subsystem == 2`) delegates execution to the base `home\pythonw.exe` declared in `pyvenv.cfg`; a missing or console-subsystem (`Subsystem == 3`) base binary would still pop up a visible console window:

- **Target + Base `pythonw.exe` PE Verification (`mindie_knowledge/windows_interpreter.py`)**: `require_windowless(python)` now runs `_verify_pe_gui` (`MZ` magic, bounded `64 <= e_lfanew <= 16 MiB`, `PE\0\0`, `PE32`/`PE32+` `0x10B`/`0x20B`, and `IMAGE_OPTIONAL_HEADER.Subsystem == 2` / `IMAGE_SUBSYSTEM_WINDOWS_GUI`) on both the target `pythonw.exe` launcher and any base `home\pythonw.exe` declared in `pyvenv.cfg`.
- **CPython 3.13 `venvlauncher.c` Precedence Alignment**: Resolution checks sibling `target.parent / "pyvenv.cfg"` before parent `target.parent.parent / "pyvenv.cfg"` (matching CPython 3.13 `PC/venvlauncher.c` and `mindie-harness` §2.4), rejecting missing, malformed, or console binaries with `ServiceHostError(stage="interpreter", code="windowless_interpreter_missing" | "windowless_interpreter_invalid")` while re-raising `TimeoutError` directly.

---

## 4. Verification

- **Full Isolated Test Suite**: `3,969 passed` under full host-directory sandbox isolation (`-m "not live_npu"`).
- **Scanner-Backed R2 Suites**: `82/82 passed` across graded distillation snapshot redaction, read-only / `capture_mode="disabled"` service lifecycle, `domain_catalog` route upgrades, and dual-layer Windows `pyvenv.cfg` PE checks (`tests/test_readonly_capture_disabled.py`, `tests/test_windows_interpreter.py`, and paired suites).
- **Hygiene & Privacy Gates**: `mindie_knowledge.redact --check` (`r4` profile) reported `0` findings; Unix LF line-ending, `state-service` PR checklist, and cross-repo code/doc & release hygiene gates all passed.
