# MindIE Knowledge (`mindie-knowledge`) — State-Service Architecture & Operating Guide

`mindie-knowledge` (`state-service` class) owns pure domain knowledge retrieval (`mindie-graded-unit/1` + `MaterialStore`), hierarchical shard routing, evidence-driven distillation (`knowledge_distill`), deterministic transcript redaction (`mindie-redaction/1` + Gitleaks `8.30.1`), unified consent authority (`mindie-consent/1`), component release/runtime inventories, and consent-gated GitHub PR publication.

---

## 1. Domain Boundaries & Submodule Map

- **Top-Level Authorities & Lazy Surface (`mindie_knowledge/`)**:
  - [`__init__.py`](mindie_knowledge/__init__.py): Lazy attribute table (`__getattr__`) preserving all 21 `__all__` exports with `< 20ms` cold import and zero eager heavy dependencies.
  - [`owned_state.py`](mindie_knowledge/owned_state.py) & [`state_layout.py`](mindie_knowledge/state_layout.py): Authoritative SQLite opener (`open_database`: WAL + `busy_timeout=5000` once per connection, never on WSL2 DrvFS or read-only inspection paths) and `FORMAT = 1` / `state-v4.sqlite3` state layout.
  - [`consent_store.py`](mindie_knowledge/consent_store.py): Single profile-level contribution and diagnostics consent authority (`mindie-consent/1`, monotonic `choice_revision`, cross-process `.lock`, atomic `os.replace`).
  - [`runtime_inventory.py`](mindie_knowledge/runtime_inventory.py), [`component_releases.py`](mindie_knowledge/component_releases.py) & [`component_runtime.py`](mindie_knowledge/component_runtime.py): Preserves exact v1 9-component `COMPONENTS` envelope by default while supporting v2 `include_substrate=True` (`COMPONENTS_V2` adding `"mindie-harness": "mindie-agent/harness"`) and `allow_wheel_provenance=True`.
  - [`redact.py`](mindie_knowledge/redact.py), [`git_layout.py`](mindie_knowledge/git_layout.py) & [`publication_check.py`](mindie_knowledge/publication_check.py): Deterministic `r3` privacy scanner, worktree-aware `resolve_git_dir` (supporting bare repos, `.git` directories, and linked `.git` worktree files), and streaming `git cat-file --batch` validator.
- **Pure Graded Retrieval vs. RSI Optimization Substrate (`mindie_knowledge/graded/`)**:
  - [`units.py`](mindie_knowledge/graded/units.py), [`index.py`](mindie_knowledge/graded/index.py), [`repository.py`](mindie_knowledge/graded/repository.py), [`explore.py`](mindie_knowledge/graded/explore.py) & [`explore_support.py`](mindie_knowledge/graded/explore_support.py): Pure knowledge unit validation (`L1–L4`), hierarchical shard routing, de-biased `reuse`/`diagnose`/`explore` ranking, `assess_stagnation`, and `subagent_fact_pack` (never writing query audit logs into unowned `Path.cwd()`).
  - [`rsi_loop.py`](mindie_knowledge/graded/rsi_loop.py), [`experiment_ledger.py`](mindie_knowledge/graded/experiment_ledger.py) & [`rsi_crossover.py`](mindie_knowledge/graded/rsi_crossover.py): **Compatibility bridges** that delegate to canonical `mindie_harness.rsi.*` when `mindie-harness` is installed and fall back locally during Wave A->B rolling upgrades. New RSI loop/crossover logic belongs in `mindie-harness`.
  - [`distill.py`](mindie_knowledge/graded/distill.py), [`ablation.py`](mindie_knowledge/graded/ablation.py) & [`publication.py`](mindie_knowledge/graded/publication.py): Evidence-backed `knowledge_distill`, `mindie-bench-ablation/2` verification, and deterministic proposal staging.
- **Canonical Material Store (`mindie_knowledge/materials/`)**:
  - [`store.py`](mindie_knowledge/materials/store.py), [`file_source.py`](mindie_knowledge/materials/file_source.py), [`reme_index.py`](mindie_knowledge/materials/reme_index.py), [`summarizer.py`](mindie_knowledge/materials/summarizer.py) & [`publication_contract.py`](mindie_knowledge/materials/publication_contract.py): Lossless 16 KiB Markdown task packages (`DATA_FORMAT_V1` + `"graded"`), canonical `realpath`-bounded symlink-safe file reads inside `root`, disposable `.reme-index/` BM25 checkpoints, and `SummaryLedger` paid-call receipts.
- **Capture Admission, Service Loop & Community Outbox (`mindie_knowledge/loop/` & `community/`)**:
  - [`activation.py`](mindie_knowledge/loop/activation.py) & [`handoff.py`](mindie_knowledge/loop/handoff.py): Manual per-session `entry_enabled = 1` lease (`P2.1`) and single-deadline `Stop` admission (`STOP_TIMEOUT_SECONDS = 6.0`; zero synchronous LLM or heavy transcript parsing on `Stop`).
  - [`store.py`](mindie_knowledge/loop/store.py), [`engine.py`](mindie_knowledge/loop/engine.py), [`publish.py`](mindie_knowledge/community/publish.py) & [`ledger.py`](mindie_knowledge/community/ledger.py): `state-v4.sqlite3` coordinator and idempotent GitHub PR publication with read-only reconciliation for `unknown` network writes.

---

## 2. State-Service Invariants & Five Iron Rules (`SVC-*`)

1. **`SVC-CONSENT` (`P2.1` vs. `P2.7`)**: Strict session admission (`entry_enabled = 1`) and `mindie-consent/1` (`choice_revision`) govern `knowledge_*`, `Stop` transcript capture, and outbound GitHub exports—never deterministic engineering tools. Withdrawing consent stops pending exports without backfilling unconsented history.
2. **`SVC-STATE-COMPAT` (Rollback-Safe Schemas)**: Preserve `state_layout.FORMAT = 1`, `state-v4.sqlite3`, `MaterialStore` `current.json` two-phase promotion, `DATA_FORMAT_V1` + `"graded"`, and Wave A `langmem`/`reme-ai` runtime probe compatibility. Configure SQLite WAL + `busy_timeout=5000` once in `open_database`.
3. **`SVC-LIFETIME` (Bounded Budgets & Symlink/Worktree Honesty)**: Keep core modules `< 800` lines with `[Role]` / `[Invariants]` / `[Timeouts & Locks]` / `[Side-Effect Policy]` headers. Enforce one monotonic outer `deadline` across `Stop` admission (`accept_stop` / `run_stop_hook`). Support linked Git worktrees (`resolve_git_dir`) and symlinked files whose canonical `realpath` stays inside the configured root.
4. **`SVC-EXPORT` (Receipt Honesty)**: Precede every `except OSError:` with `except TimeoutError: raise`. Reserve `unknown` / `outcome_unknown` strictly for external LLM calls (`SummaryLedger`) and remote Git/GitHub writes (`Ledger`), reconciling via read-only checks without blind replay and preserving succeeded remote results (`recording_failed=True`) if local receipt updates fail.

---

## 3. Isolated Verification & PR Gates

Isolate `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `XDG_*`, `MINDIE_CONFIG_HOME`, `MINDIE_DATA_HOME`, `MINDIE_DIAGNOSTICS_ROOT`, `UV_CACHE_DIR`, and `TMPDIR` in all tests:

```bash
MINDIE_FRAMEWORK_SOURCE=tests/fixtures/production-parsers python3 -B -m pytest -q -n auto --dist worksteal -m "not live_npu"
python3 -B -m mindie_knowledge.redact --check README.md README.en.md CONTRIBUTING.md CONTRIBUTING.en.md docs mindie_knowledge/skills
python3 .github/scripts/check_lf_eol.py
python3 .github/scripts/check_pr_checklist.py --profile state-service --body-file <pr_body.md>
```
