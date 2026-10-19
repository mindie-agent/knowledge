# Community Sharing & Contributor Outbox

[中文](community-sharing.zh.md) | **English** · [Back to Documentation Center](README.en.md)

This guide explains how `mindie_knowledge.community` handles **contributor-side publication** on the developer's machine: isolating GitHub credentials, packaging indexed local task drafts and usefulness votes into Pull Requests, and recovering safely from interrupted network writes via read-only reconciliation.

> [!NOTE]
> **Component Boundary**: This module ships only the deterministic contributor-side publisher (`submit_batch` / `reconcile_batch`, with **zero model calls**). Repository-side review and auto-merge are performed by the maintainer's external review bot under the [Repository Bot Contract](repository-bot-contract.md).

---

## 1. Credential Isolation & Live Consent Gate

1. **Environment Variable Names Only, Never Raw Tokens**:
   Credentials never appear in settings files, receipts, logs, or Git history. The settings file stores only the *name* of the token environment variable (`token_env`, defaulting to `GH_TOKEN`). Git subprocesses authenticate through a per-command `credential.helper=!gh auth git-credential` binding with the token variable mapped only inside the subprocess environment.
2. **Re-Verifying Live Authority Before Every Write**:
   Before any outbound write, the publisher re-reads the private absolute config file (`settings["config_path"]`) and `mindie-consent/1`, verifying that `enabled`, `generation`, `enabled_at`, `repository`, `branch`, `fork`, `account`, and `project_roots` all still match. Any mismatch fails closed immediately.
3. **Bounded Subprocess Execution**:
   All Git / `gh` commands run as pure `argv` lists (no shell interpolation) with owned process-tree cleanup, bounded output buffers, a total transaction deadline (`transaction_seconds`, default 120s), and an operation count cap (`operation_limit`, default 60).

---

## 2. Batch Packaging & Idempotent PR Lifecycle (`mindie-contribution/1`)

### Trust-Boundary Re-Verification & Zero-Model Templates
- Outbound batches use schema `mindie-contribution/1` and carry complete `tasks/<task-id>/index.md` manifests (`mindie-material-task/1` + `mindie-entry/3`) with their exact referenced `tasks/<task-id>/blocks/*.md` files (`mindie-material-block/1`), plus optional `feedback/*.json` files (`mindie-feedback/1`).
- Before mutating Git state, `mindie_knowledge.community` re-verifies every file's SHA-256 digest and path allowlist, generates the commit message and PR title/body from deterministic templates (zero LLM involvement), and re-scans **all file contents, PR text, and commit messages** with `mindie_knowledge.redact` (`r3`).

### PR State Transitions

```mermaid
stateDiagram-v2
  [*] --> Staging: Local indexing complete + outbound redaction clean
  Staging --> Submitted: Pushes to contributor fork & opens upstream PR
  Submitted --> Updated: Appends new revision to open PR (fast-forward only)
  Submitted --> Merged: Upstream merges PR (future deltas branch from latest main)
  Submitted --> Rejected: Upstream closes PR unmerged (quarantines batch; never re-opens)
  Submitted --> NeedsReview: Remote file edited by maintainer/bot (parks on base conflict)
  Staging --> Unknown: Network timeout or interruption mid-flight
  Unknown --> Submitted: Read-only reconciliation confirms commit reached PR
```

- **Idempotence Keyed on Content Revision**: Retries and deduplication key on the content revision digest rather than transient event IDs.
- **Respecting Upstream Maintainer Edits & Withdrawals**: The upstream repository (`main`) is the sole content authority; the contributor's fork is merely a write staging destination.
  - Existing open PRs owned by the contributor are updated strictly via fast-forward (never force-pushed);
  - If a remote file was modified by a maintainer or the review bot (diverging from the last confirmed send receipt) or withdrawn upstream, the candidate parks as `needs_review` rather than overwriting remote edits;
  - If an upstream PR is closed without merging, the batch is recorded as `rejected` and its entries are quarantined from future automatic batches rather than resurrected in a new PR.

---

## 3. Handling Interrupted Network Writes (`unknown` vs. `unavailable`)

To prevent duplicate PRs or accidental overwrites after a network drop, the publisher strictly separates pre-write environment failures from mid-flight uncertainty:
- **Pre-Write Environment Failure (`unavailable`)**: If `git` or `gh` is missing or fails to spawn before any remote mutation starts, the batch is marked `unavailable` and retried automatically with persisted backoff once the local environment recovers.
- **Mid-Flight Interruption (`unknown`)**: If a timeout, broken connection, or cancellation happens after a write may have reached GitHub, the batch is recorded as `unknown`. Subsequent scheduler passes run **bounded read-only remote reconciliation (`reconcile`)**—checking whether the expected commit SHA is the PR head or an ancestor of the PR branch—and never blindly replay the write.

---

## 4. CLI & Skill Validation Helpers

```bash
# Submit a frozen contribution batch (prints a single-line JSON receipt)
python3 -m mindie_knowledge.community submit \
    --settings community.json --state-dir ~/.mindie/community --batch batch.json

# Reconcile an unknown or pending batch via read-only remote inspection
python3 -m mindie_knowledge.community reconcile \
    --settings community.json --state-dir ~/.mindie/community --batch-id <batch-id>
```

Additionally, `mindie_knowledge.community.skill_validation` provides deterministic, model-free validation helpers (checking `SKILL.md` structure, `policy.allow_implicit_invocation: false`, and allowed paths under `plugins/mindie-agent/skills/`) for maintainers and external bot Skill proposals.
