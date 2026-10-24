# mindie-knowledge 0.13.3: Independent Component Release Discovery, Side-by-Side Runtime Builds & Whole-Delivery Token Budget Estimation

[中文](https://github.com/mindie-agent/knowledge/blob/v0.13.3/docs/releases/knowledge-0.13.3.md) | **English**

`mindie-knowledge 0.13.3` adds independent GitHub Release discovery, benchmark-gate verification, and side-by-side isolated `uv` runtime preparation for shared components (`#101`), alongside whole-delivery token budget estimation, complete exploration premises, single-transaction deduplicated delivery accounting, and mixed-query source index binding (`#97`, `#99`, `#100`), pinned to the formal `mindie-diagnostics 0.5.2` release (`801a01b2789aed7f58e971a909205a7d40b61077`).

---

## 1. Highlights

- **Independent Component Release Discovery & Benchmark Gate Verification (`component_releases.py` & `runtime_inventory.py`)**:
  - Queries `/releases/latest` directly across the 8 official component repositories (`mindie-knowledge`, `remote-dev`, `mindie-diagnostics`, `mindie-profiler`, `mindie-sim`, `npu-top`, `mindie-coordinator`, and `mindie-bench`), advancing each installed or baseline-declared component (`runtime-requirements.txt` or `pyproject.toml`) monotonically to the highest formal version without downgrading, and raising an error if a commit SHA changes without a version bump.
  - Verifies that every selected formal Release includes non-empty `.whl`, `.tar.gz` (sdist), `SHA256SUMS`, `benchmark-results.json`, `benchmark-results.md`, and `benchmark-SHA256SUMS` assets; dereferences the tag to an immutable 40-character commit SHA and checks `pyproject.toml` package name and version at that commit; downloads `SHA256SUMS` and `benchmark-results.json` to verify SHA-256 digests; and requires `release_gate is True`, matching `version`, and clean `source` / `source_after` records (`revision` match and `dirty is False`).
- **Side-by-Side Isolated `uv` Runtime Preparation & Bounded `ETag` Cache (`component_runtime.py` & `release_http.py`)**:
  - Builds candidate runtimes outside the active environment under a generation directory (`generation/source` beside `generation/runtime/.venv`), installs from the verified exact Git commit SHAs via `uv sync --project <runtime> --no-editable --no-dev`, and runs `python -I runtime_inventory.py <components.json>` in the candidate interpreter to verify the full `uv.lock` SHA-256 and each package's `direct_url.json` commit receipt before atomically writing `components.json` (`mindie-component-runtime/1`). Completed generations are reused after verifying retained adapter source bytes, while native API probing and idle cutover remain owned by each host plugin adapter.
  - Shares a bounded public HTTP cache (`128` entries / `64 MiB` max; `86400s` TTL for immutable Git/content/asset URLs, `300s` with a GitHub token or `900s` unauthenticated for mutable Release endpoints, with `ETag` conditional revalidation) that surfaces offline or transport errors explicitly rather than masking failures with expired cache entries. Update checks and runtime builds never start a background knowledge worker or alter the manual per-session `/mindie-agent` activation gate.
- **Whole-Delivery Token Budget Estimation, Complete Exploration Premises & Single-Transaction Delivery Accounting (`#97`, `#99`, `#100`)**:
  - `graded/budget.py` enforces whole-delivery token budget estimation and trimming across the entire delivered `query`, `explain`, and `explore` response payload, reporting `budget_met = False` and a converged `minimum_tokens` floor when the protocol envelope estimate alone exceeds `token_budget`.
  - `graded/explore.py` removes fixed character slicing on exploration unit bodies, binds canonical `mindie://<domain>/units/<id>@<revision>` references to mounted units, and sheds whole units in `L4 -> L2 -> L1` order under tight budgets while recording shed units in `excluded_units` and refreshing `subagent_fact_pack`.
  - Delivered unit revisions are deduplicated and recorded within a single SQLite write transaction (`inherited_rules` are registered as readable without inflating delivery counts), and mixed queries bind budget enforcement and delivery accounting to the exact in-memory routing index snapshot used for retrieval.

---

## 2. Verification & Acceptance Boundary

- **Verified Test Results**:
  - Full local test suite: `1555 passed, 22 skipped, 3 deselected, 3 subtests`;
  - Targeted component-update and runtime-inventory tests: `58 passed`;
  - 8-module budget estimation, exploration premise, deduplicated delivery, and manual-entry regression suite: `105 passed, 1 skipped` (`13.02s`);
  - All 3 GitHub Actions CI workflows (including `mindie_knowledge.redact --check`) passed on `#101`.
- **Host Acceptance Boundary**:
  - This release pins the formal `mindie-diagnostics 0.5.2` release (`801a01b2789aed7f58e971a909205a7d40b61077`) and provides the shared component Release discovery and side-by-side runtime build layer. End-to-end native new-session auto-update acceptance across the four host plugins (Codex, Claude Code, Kimi Code, and DSH) is verified separately on the host plugin side and is not claimed as completed in this component release note.
