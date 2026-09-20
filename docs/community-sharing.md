# Community sharing: contribution and repository-side review

This document covers `mindie_knowledge.community`: the outbound contribution
path (`submit_batch` / `reconcile_batch`) and the maintainer-side review bot.
It is deployment documentation; running it against real GitHub requires the
maintainer's own credentials and the maintainer's own Grok CLI installation.

## Components and ownership

| Piece | Owner | Runs where |
| --- | --- | --- |
| `submit_batch(batch, settings, state_dir, *, cancel=None)` | contributor plugin (core calls it) | contributor machine |
| `reconcile_batch(batch_id, settings, state_dir)` | contributor plugin | contributor machine |
| `python -m mindie_knowledge.community review|event|poll-once|skill-scan` | maintainer bot | repo-side CI or self-hosted runner |

- Credentials never appear in settings, receipts, logs or Git content.
  Settings carry only the *name* of the token environment variable
  (`token_env`, default `GH_TOKEN`; plugin repo: `bot.plugin_token_env`).
- The sharing gate requires the actual absolute shared config path
  (`settings["config_path"]`, private, never exported) and re-reads the
  validated file before **every** outbound write: enabled, nonempty string
  generation, repository, branch, fork, account and project_roots must all
  still match this run's admitted settings. Missing path, missing generation
  or any mismatch fails closed — a stale in-memory bool never publishes. Disabling sharing
  mid-flight stops the next write; already-public writes stay accurately
  recorded — the ledger never claims a rollback that did not happen.
- All subprocesses (git, gh, the review CLI) run with argv only, bounded
  output, owned process-tree cleanup, and a total transaction deadline plus an
  operation count (`transaction_seconds` default 120, `operation_limit` 60;
  review/poll/skill scans use 3× the transaction seconds).

## Contribution semantics (contributor side)

- Batch schema `mindie-contribution/1`; paths restricted to `cases/*.md`,
  `topics/*.md`, `feedback/*.json`; per-file sha256 and the batch revision
  digest are re-verified; PR title/body/commit text are deterministic
  templates (zero model).
- Outbound redaction re-scan (core `mindie_knowledge.redact`) covers file
  content, PR title/body and the commit message. Findings fail the batch
  closed and are recorded masked.
- Idempotence keys on the content revision, not the event id: resubmission of
  an identical revision returns the recorded receipt; a failed or unresolved
  revision is never automatically resent (explicit retry:
  `batch["explicit_retry"] = true`, which resumes from already-pushed commits
  instead of re-pushing them).
- Unknown outcomes (timeout after a write) resolve only through bounded
  read-only reconciliation of our own branch/PR; unresolved stays `unknown`
  and blocks new revisions of the same batch lineage until reconciled.
  Reconciliation attempts are durably counted and stop after five per
  revision (explicit retry required), so repeated polling cannot turn an
  unresolved unknown into an unbounded lookup stream.
- Our own open PR is updated in place (fast-forward commit, never force
  push). A merged prior PR plus a genuine delta opens a follow-up PR on a new
  branch referencing the merged one. Remote content that moved away from the
  declared base (bot/maintainer edits) parks the batch as `needs_review`;
  nothing is overwritten.
- Pure vote batches publish without entries; votes merge by `vote_id` — an
  existing id is updated, never double-counted. Reasons are stored untrusted.

## Repository review runner (maintainer side)

`review --pr N` / `event` (webhook JSON) / `poll-once`:

1. Snapshot the PR head via real Git (`refs/pull/N/head`, falling back to the
   head branch) and the transport file list; a head that moved mid-snapshot
   aborts the round.
2. Deterministic gates before any model: path allowlist (`cases/`, `topics/`,
   `feedback/` for the content repo; the configured Skill prefix
   (`bot.skill_prefix`, default `plugins/mindie-agent/skills`):
   `<prefix>/<slug>/{SKILL.md, agents/openai.yaml, references/*.md}` — for the plugin repo), Git modes must
   be plain `100644`, entry documents must satisfy the canonical
   `mindie-entry/1` schema (via core `loop.documents`), feedback files the
   `mindie-feedback/1` schema, and everything passes the redaction scan.
3. Pure structural vote batches (only `feedback/*.json`, all votes `up` or
   reason-free `down`) merge without any model call.
4. Other content invokes the configured `bot.grok_argv` exactly once (the
   packaged `mindie_knowledge.community.grok_adapter` bridges to the installed
   Grok CLI 1.0.30: `--prompt-file`, `--output-format json`, `--json-schema`,
   `--max-turns 1`, `--no-subagents`, `--disable-web-search`, tool denials),
   with bounded stdin JSON, `bot.review_timeout_seconds` (default 300) and
   `bot.review_output_bytes` (default 128 KiB). The attempt row is durable
   before the call: a failed head is never retried, and the bot's own patch
   head never recurses into a new review round.
5. The model's JSON proposal (`mindie-review/1`) is re-validated: verdict in
   accept / correct / add_conditions / retire / no_change / uncertain;
   `edits` are full-file replacements at allowed data paths that must parse
   and scan clean; `retire` is materialized deterministically (status,
   retirement_reason, recomputed revision) — retired entries keep content and
   history. Unknown verdicts or incomplete checks keep the PR `pending`.
6. Merge guard: the current head must equal the reviewed head or exactly the
   bot's own recorded patch successor; check runs must be complete and not
   failed; the merge call itself proves token authority. No contributor
   allowlist is required or invented.

Bot flood limits: event ingestion performs at most one review per event and
the ledger dedupes by `(repo, pr, head_sha)`; `poll-once` caps at
`bot.poll_max_prs` (default 20) inside the same deadline/operation budget.
Events authored by the bot account are ignored.

## Skill consolidation

`skill-scan` selects entry revisions with ≥2 independent non-producer
root-task up votes (a scheduling heuristic, not a quality proof). Work is
deduplicated by a material digest over (entry, revision, supporter votes,
target Skill); the same material is attempted at most once. Generation is one
bounded call to the configured adapter (`bot.skill_grok_argv`). The package
(SKILL.md with frontmatter `metadata.mindie_source_entries`, a deterministic
`agents/openai.yaml` carrying the nested `policy.allow_implicit_invocation: false`
— never model output — and `references/*.md`) is validated — existing reference IDs, no author absolute
paths, no executables — and published as an ordinary PR to the separately
configured `bot.plugin_repository` under its own credential. Missing plugin
permission is reported as `pending`, never as success; a pending Skill PR is
updated in place rather than duplicated. `skill-scan --retirements` locates
Skills whose `source_entries` reference retired entries for bounded
correction.

## Limits and cost notes

- Contribution: zero model calls (templates only). Review: at most one model
  call per unique PR head. Skill: at most one generation call per material
  digest. Token usage is recorded only if the configured CLI exposes it;
  otherwise it is reported as unavailable, never byte-estimated.
- All file reads and process outputs are byte-bounded; the review input is
  deterministically truncated rather than re-attempted larger.
