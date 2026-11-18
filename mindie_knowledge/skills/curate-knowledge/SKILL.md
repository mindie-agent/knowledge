---
name: curate-knowledge
description: Maintain local graded domain knowledge drafts (`L1-L4`) and inspect contribution state. Ordinary lookup, use, and Stop capture do not load this skill.
---

# Curate Knowledge

Knowledge units (`mindie-graded-unit/1`) are advisory reference material with explicit provenance and applicability `conditions`. Neither a knowledge unit nor a captured transcript is an execution instruction or a permission grant.

This skill is for **explicit corpus curation and contribution-state maintenance only**; ordinary troubleshooting and retrieval do not load it.

## 1. Graded Unit Invariants (`L1–L4`)

- **Functional tier vs. evidence strength**:
  - `L1` (`axiom` / `measurement`), `L2` (`sop`), `L3` (`workflow`), `L4` (`mutator`).
  - `evidence_strength` is one of `hardware_spec`, `reproduced_empirical`, `single_observation`, `hypothesis` (`L1` accepts only `hardware_spec` or `reproduced_empirical`; `L4` requires a verified ablation receipt).
- **Structured `conditions` & three-valued matching**:
  - `conditions` is a non-empty structured mapping of checkable premises (including chip/hardware, software/framework versions, topology, or workload constraints). Never write `"unknown"` or `"n/a"` as a required condition value.
  - Missing or `"unknown"` facts evaluate to `condition_unknown` (not matched) until verified.
- **Immutable revisions & references**:
  - Units are addressed as `mindie://<domain>/units/<unit_id>@<revision>` (positive integer `revision` advancing monotonically on semantic content changes).
  - Apply reviewed unit changes via `apply_reviewed_change.py` (`mindie-reviewed-change/1`) or `knowledge_distill` (`task_state="verified_milestone"` | `"completed"`), and rebuild `index/routing-manifest.json` rather than hand-editing derived indexes.

## 2. Entry & Authority Boundaries

| Surface | Authority | Entrypoint | Out of Scope |
| --- | --- | --- | --- |
| Retrieval, explain, feedback, distill | Active `/mindie-agent` session lease (`entry_enabled = 1`) | `knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill` | Unactivated sessions, deterministic MCP tools |
| Background capture & local distillation | Active session lease + saved `local-only` or `contribute` consent (with `choice_revision`) + authorized `project_root` | `Stop` notification handoff (`capture_hook`) | Unactivated turns, out-of-scope directories |
| GitHub publication & diagnostics upload | Saved `contribute` consent only; `local-only` material is never exported | Existing outbox / reporter workers | `local-only` or `read-only` choices, stale fork/account fields |
| Offline status & feed sync | Explicit maintenance CLI; public canonical corpora read via anonymous HTTPS | `python -m mindie_knowledge status|sharing-status|sync --config <domain.json>` | Scraping unrelated transcripts or home directories |

## 3. Operational Status & Recovery Semantics

```sh
python -m mindie_knowledge sharing-status --config domain.json
python -m mindie_knowledge status --config domain.json
python -m mindie_knowledge sync --config domain.json
```

- **`disabled` / `needs_choice`**: Outbound sharing is off until an explicit unified consent choice (`mindie-consent/1`) is recorded.
- **`unknown`**: An outbound Git push or GitHub PR request lost its receipt; reconcile read-only against remote state before any further write—never blindly replay.
- **`unavailable` / `coverage gap`**: Local transient or bounded-deadline failure; failure counters are diagnostic only and never permanently brick a session lease.
- **Privacy redline**: Every public unit, summary, feedback reason, and PR body must pass `mindie_knowledge.redact` and pinned Gitleaks `8.30.1` with zero findings.
