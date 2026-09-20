# Community sharing: contributor-side publication

This document covers what this package actually ships: the deterministic
contributor-side publication path (`submit_batch` / `reconcile_batch` and the
`python -m mindie_knowledge.community submit|reconcile` CLI). Repository-side
review and Skill proposals are operations of the **external Grok Bot
software**, deployed and credentialed by the maintainer — see
`docs/repository-bot-contract.md`. Nothing in this package installs, invokes
or configures that bot, and no model is ever called here.

## Credentials and the live gate

- Credentials never appear in settings, receipts, logs or Git content.
  Settings carry only the *name* of the token environment variable
  (`token_env`, default `GH_TOKEN`); Git subprocesses authenticate through a
  per-command `credential.helper=!gh auth git-credential` binding, with the
  configured variable mapped to `GH_TOKEN` only inside the subprocess env.
- The sharing gate requires the actual absolute shared config path
  (`settings["config_path"]`, private, never exported) and re-reads the
  validated file before **every** outbound write: enabled, nonempty string
  generation, finite positive `enabled_at`, repository, branch, fork, account
  and project_roots must all still match. Any mismatch fails closed.
- All subprocesses run with argv only, bounded output, owned process-tree
  cleanup, and a total transaction deadline plus an operation count
  (`transaction_seconds` default 120, `operation_limit` 60).

## Publication semantics

- Batch schema `mindie-contribution/1`; paths restricted to `cases/*.md`,
  `topics/*.md`, `feedback/*.json`; per-file sha256 and the batch revision
  digest are re-verified; PR title/body/commit text are deterministic
  templates (zero model).
- Outbound redaction re-scan (core `mindie_knowledge.redact`) covers file
  content, PR title/body and the commit message; findings fail closed and are
  recorded masked.
- Idempotence keys on the content revision, not the event id. A failed or
  unresolved revision is never automatically resent; explicit retry
  (`batch["explicit_retry"] = true`) resumes from already-pushed commits.
  Unknown outcomes resolve through bounded read-only reconciliation, durably
  capped per revision.
- Our own open PR is updated in place (fast-forward only, never forced). A
  merged prior PR plus a genuine delta opens a follow-up PR on a fresh
  branch. Remote content that moved away from the declared base parks the
  batch as `needs_review`; nothing is overwritten.
- Pure vote batches publish without entries; votes merge by
  `(root_id, entry_id, revision)` — replacement updates, never double counts;
  a revision change is a distinct vote. Reasons are stored untrusted.
- Mid-flight cancellation kills the in-flight subprocess and records
  `unknown`, never a false clean refusal.

## CLI

```bash
python -m mindie_knowledge.community submit \
    --settings community.json --state-dir ~/.mindie/community --batch batch.json
python -m mindie_knowledge.community reconcile \
    --settings community.json --state-dir ~/.mindie/community --batch-id <id>
```

Both print one JSON receipt (`status` in submitted/updated/unchanged/unknown/
failed/disabled/needs_review, plus `batch_id`, `revision`, `pr_url`,
`head_sha`, bounded `detail`).

## Skill package validation helpers

`mindie_knowledge.community.skill_validation` provides deterministic data-only
checks (SKILL.md shape, native `policy.allow_implicit_invocation: false`,
allowed package paths under `plugins/mindie-agent/skills/`, bounded reference
files). They validate bytes only — no generation, no model, no repository
writes — and exist for maintainers and the external bot's proposals.
