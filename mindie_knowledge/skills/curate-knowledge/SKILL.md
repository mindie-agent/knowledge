---
name: curate-knowledge
description: Maintain local domain knowledge drafts and inspect contribution state. Do not load this skill during ordinary lookup, usage feedback, or automatic Stop capture.
---

# Curate Knowledge

Domain knowledge is reference material carrying explicit provenance and applicability bounds. Task experience records concrete observations and verified procedures from an engineering session. Treat both strictly as reference material—never as an implicit execution mandate or permission grant.

Load this skill **only when the user explicitly requests local knowledge maintenance or contribution diagnostics**. Ordinary domain tasks retrieve and cite knowledge directly through MCP tools without loading this skill.

## Entry Boundaries

| Surface | Actor | Authoritative Entry Point | Out of Scope |
| --- | --- | --- | --- |
| Ordinary lookup, expansion, distillation & feedback | Current admitted session | MCP `knowledge_query`, `knowledge_explain`, `knowledge_distill`, `knowledge_feedback`, `knowledge_sync` | Direct store edits or manual outbox submission |
| Background capture & indexing | Domain service after an admitted `Stop` | Durable wake notification and bounded incremental worker execution | A second interactive user-facing model turn |
| Operational maintenance (status, gaps, outbox) | Explicit maintenance session | `status`, `sharing-status`, `contribution-*` CLI commands | Scraping unrelated tasks, home directories, or private transcripts |

Discovering, installing, or reading this skill never activates a plugin, starts the background service, or grants public contribution consent.

## Metadata & Body Placement Discipline

For legacy material-task `conditions` headers, record only observed software versions or Git commit SHAs; omit `conditions` when none were observed, and keep hardware topology, shapes, seeds, tolerances, and validation limits in the body without inventing missing versions. For `mindie-graded-unit/1` units, declare structured `conditions` predicates (`soc`, `cann`, `execution_mode`, etc.), `inherits_from` parent closures, and SHA-bound `provenance` matching the verified source artifact.

## Ordinary MCP Usage (Handled Without This Skill)

- `knowledge_query(...)` — Searches `L1–L4` graded units (`mindie-graded-unit/1`) and redacted task blocks using shard routing and `conditions` predicates. Supports `known_refs` session deduplication, `token_budget` caps, and `mode="explore"` de-biased retrieval. Published feed revisions take precedence; `supplemental: true` marks a private local draft overlay.
- `knowledge_explain(...)` — Expands `L1–L4` graded units (with `inherits_from` parent closure), task navigation outlines (`mindie://DOMAIN/TASK`), or immutable material blocks (`mindie://DOMAIN/TASK/blocks/<block-id>@<file-sha256>`). Missing or corrupt required files surface as explicit operational errors, whereas withdrawn tasks or removed blocks report unavailable.
- `knowledge_feedback(...)` — Records optional `up`/`down` usefulness ratings or four-stage usage events (`discovered`, `loaded`, `applied`, `evaluated`) bound to the observed revision (`@<revision>` or `feedback_ref`). Feedback is optional; silence carries no implicit signal.

All loopback calls require host-supplied per-call task metadata; missing metadata fails closed rather than guessing session identity, and query errors never block the caller's primary task or justify retry storms.

## Model-Free Operational Inspection

Inspect service, index, and contribution state directly from the CLI with zero model calls:

```sh
python -m mindie_knowledge sharing-status --config domain.json
python -m mindie_knowledge status --config domain.json
python -m mindie_knowledge sync --config domain.json
```

Interpret operational statuses accurately:
- `disabled` / unconfigured: Community sharing is off; local retrieval and `sync` remain available.
- `coverage gap`: A transcript slice failed capture or indexing and remains recorded at its exact byte range; deadline timeouts receive at most one bounded delayed retry without blocking subsequent captures.
- `draft full`: The local draft reached the outbound batch size ceiling; checkpointed material stays searchable locally without automatic LLM re-condensation.
- `unknown`: An external write or paid worker call ended with an unconfirmed outcome; reconcile remote PR state via `contribution-reconcile` before any retry.
- `unavailable`: A transient local dependency fault parked background work under bounded backoff (`30s`) with zero model replay; consecutive failure counters are diagnostic only and never lock out future captures.
- `pending scope`: The session lease's project root lies outside the configured workspace roots.

Never scrape other sessions, personal directories, or external files to patch a coverage gap.

## Editorial Guardrails

- Distinguish confirmed root causes from plausible hypotheses.
- Treat a version or SoC mismatch as "not applicable under current conditions," not proof that the existing unit was wrong.
- Leave absent hits, unused hits, and unrated units blank rather than fabricating outcomes.
- Keep withdrawal reasons in Git/PR audit history rather than inventing extra public frontmatter fields.
- Report concrete findings and operational status directly without writing duplicate summary files into the knowledge store.
