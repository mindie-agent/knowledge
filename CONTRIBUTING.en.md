# Contributing to `mindie-knowledge`

[中文](CONTRIBUTING.md) | **English** · [Back to Homepage](README.en.md)

Thank you for contributing to **MindIE Knowledge** (`mindie-agent/knowledge`, repository class: `state-service`)! This repository powers graded domain knowledge retrieval (`L1–L4`), hierarchical shard routing, lossless task material packages (`MaterialStore`), double fail-closed privacy redaction, the unified consent store (`mindie-consent/1`), and community PR reconciliation.

---

## 1. Core Engineering Rules (`state-service` Checklist Contract)

1. **`SVC-CONSENT` — Zero-Binding Public Reads & Strict Capture/Export Consent Scope (`P2.7` / `P2.8` & `P2.1`)**:
   Public knowledge reads and sync (`knowledge_query`, `knowledge_explain`, local `knowledge_feedback`, and `knowledge_sync`) work out of the box over standard MCP `tools/call` with zero pre-call session-binding gates (`P2.7` / `P2.8`). Community sharing remains opt-in by default: `Stop` transcript capture, `knowledge_distill`, and outbound GitHub PR exports are strictly governed by `mindie-consent/1` (monotonic `choice_revision` across `contribute` / `local-only` / `read-only`), authorized `project_roots`, and active capture leases (`origin_tier="local-only"` is never exported). Withdrawing consent cancels unsent exports immediately, and re-enabling never backfills unconsented sessions.
2. **`SVC-STATE-COMPAT` — Rollback-Safe On-Disk Schemas & Pure Knowledge Scope**:
   Keep `state_layout.py` (`FORMAT = 1`, `state-v4.sqlite3`), `MaterialStore` two-phase `current.json` promotion, and `mindie-graded-unit/1` rollback-safe across rolling upgrades. Canonical RSI evolution (`rsi_loop`, `experiment_ledger`, `rsi_crossover`) lives in `mindie_harness.rsi`; the matching modules in `mindie_knowledge.graded` serve strictly as Wave A->B compatibility shims. Never write query audit logs relative to unowned `Path.cwd()`.
3. **`SVC-LIFETIME` — Single Monotonic Deadlines, Symlink/Worktree Support & Cohesive Modules (`< 800` LOC)**:
   `Stop` capture admission enforces a single monotonic outer deadline without synchronous LLM calls or heavy transcript parsing, reporting degraded capture honestly when budget expires. Validate canonical `realpath` target boundaries rather than rejecting legitimate user/dotfile symlinks or linked Git worktree `.git` files. Keep core modules `< 800` lines with `[Role]` / `[Invariants]` / `[Timeouts & Locks]` / `[Side-Effect Policy]` headers, and keep cold `import mindie_knowledge` lazy (`< 20ms`).
4. **`SVC-EXPORT` — Fail-Closed Redaction & Receipt Honesty**:
   Every outbound export passes Gitleaks + `mindie_knowledge.redact` (`r4`). Distinguish `completed`, `failed`, and `unknown` (`outcome_unknown`) external effects: reconcile interrupted network writes via read-only queries without blind replay, always place `except TimeoutError: raise` before `except OSError:`, and preserve succeeded remote publication receipts if local receipt updates fail.

---

## 2. Local Isolated Testing & Required Gates

All tests must isolate host paths (`HOME`, `USERPROFILE`, `XDG_*`, `MINDIE_*`, `UV_CACHE_DIR`, `TMPDIR`) without touching real user state:

```bash
# 1. Install editable package and test dependencies
python3 -m pip install -e '.[test]'

# 2. Run isolated unit and contract tests (excluding live NPU fixtures)
MINDIE_FRAMEWORK_SOURCE=tests/fixtures/production-parsers python3 -B -m pytest -q -n auto --dist worksteal -m "not live_npu"

# 3. Scan public docs and packaged skills for accidental private data (use <user>@<host> or 192.0.2.x in examples)
python3 -B -m mindie_knowledge.redact --check README.md README.en.md CONTRIBUTING.md CONTRIBUTING.en.md docs mindie_knowledge/skills

# 4. Check Unix LF line endings and the state-service PR checklist
python3 .github/scripts/check_lf_eol.py
python3 .github/scripts/check_pr_checklist.py --profile state-service --body-file <pr_body.md>
```

---

## 3. Submitting a Pull Request

Complete [`.github/pull_request_template.md`](.github/pull_request_template.md) with concrete verification notes for `SVC-CONSENT`, `SVC-STATE-COMPAT`, `SVC-LIFETIME`, and `SVC-EXPORT`. If an item does not apply (for example, a documentation-only change), write `not-applicable: <specific reason>` rather than leaving placeholders like `N/A` or `TODO`.
