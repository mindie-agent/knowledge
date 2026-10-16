# MindIE Domain Loop Overview

[中文](mindie-loop.zh.md) | **English** · [Back to Documentation Center](README.en.md)

This guide explains how `mindie-knowledge` runs the complete domain knowledge and task-experience loop—from user consent gates and public conversation capture to sub-second local BM25 retrieval, revision-bound usefulness feedback, and two-way synchronization with public corpora such as [`knowledge-vllm-ascend`](https://github.com/mindie-agent/knowledge-vllm-ascend).

---

## 1. End-to-End Loop at a Glance

```mermaid
sequenceDiagram
  autonumber
  actor Dev as Developer
  participant Host as Host Adapter (Claude / Codex / Kimi)
  participant Core as mindie-knowledge Local Core
  participant Remote as Public Corpus (GitHub)

  Note over Dev,Remote: Phase A: Everyday Retrieval & Use (Zero LLM cost; works with community sharing off)
  Dev->>Host: Asks an Ascend NPU / vLLM-Ascend debugging question
  Host->>Core: knowledge_query(query)
  Core-->>Host: Returns BM25 block excerpts, citation groups & navigation refs
  Host->>Core: knowledge_explain(ref) to read specific blocks on demand
  Core-->>Host: Verifies SHA-256 and returns the full redacted block body
  Host-->>Dev: Delivers verified engineering solution
  Host->>Core: (Optional) knowledge_feedback(feedback_ref, "up"|"down", reason)

  Note over Dev,Remote: Phase B: Capture & Community Sharing (Only when Opt-in is enabled for this project scope)
  Host->>Core: Stop Hook forwards session identity & transcript path
  Core->>Core: 1. Re-verifies mindie-consent/1 & project scope gate
  Core->>Core: 2. Projects visible public turns + Gitleaks & redact(r3) scan + 16 KiB lossless blocks
  Core->>Core: 3. Invokes small native worker to index block titles & index.md navigation
  Core->>Remote: 4. Outbox re-scans, packs mindie-contribution/1 batch & opens PR (or reconciles read-only)
```

---

## 2. One-Time Explicit Choice, Persistent Across Sessions

Developers are never nagged with repeated consent prompts on every session:
- **Single Onboarding Choice**: The first time you invoke the native entry after installation (`/mindie-agent` in Claude Code / Kimi, or `$mindie-agent` in Codex), it asks once whether you want to enable community sharing and saves that choice in `mindie-consent/1`.
- **Zero Capture by Default (Opt-in)**: Community sharing defaults to off (`enabled=false`). While off, the Stop Hook short-circuits immediately—**performing zero transcript reads, zero redaction scans, zero summary model calls, and creating zero local drafts**—while read-only retrieval (`knowledge_query` / `knowledge_explain`) and public feed sync (`sync`) remain fully available.
- **Change Anytime Without Backfill**: You can toggle community sharing on or off at any time through the same entry. Turning sharing off mid-task immediately cancels queued/running maintenance and unsent batches while keeping already saved local drafts intact; turning it back on later captures only new turns and **never backfills transcripts from the disabled period**.

---

## 3. The Live Authority Gate

To guarantee that damaged or mismatched configuration files can never trigger unintended external writes, `mindie-knowledge` re-reads both the shared settings file and the absolute `mindie-consent/1` file **before every transcript read, before every summary model spawn, and before every outbound write**:

$$\text{capture\_allowed} = \text{active adapter lease} \land \text{community enabled} \land \text{project\_root in scope} \land \text{saved contribute consent}$$

- **Distinguishing Disabled State from Authority Faults**: If the settings file or `mindie-consent/1` is missing, unreadable, or corrupt, the runtime closes all capture/model/write paths and reports an explicit **authority fault**. Already received captures and returned model outputs are parked safely with bounded backoff until the file is repaired—**corrupt authority is never treated as first-time onboarding**.

---

## 4. Task Packages & Two-Stage Reading (`knowledge_explain`)

Both local drafts and synchronized public entries share the same **manifest + immutable block** structure:

- **Task Navigation Manifest (`tasks/<task-id>/index.md`)**:
  Uses `mindie-material-task/1` with an embedded `mindie-entry/3` header, binding the ordered list of block hashes (`sha256`), block titles/summaries, and task-level navigation.
- **Lossless Redacted Material Blocks (`tasks/<task-id>/blocks/<block-id>.md`)**:
  Use `mindie-material-block/1` to store slices of deterministically redacted public conversation (up to 16 KiB per block). **Ordinary Markdown files in `materials/` are always the source of truth for body bytes**, while `state-v4.sqlite3` stores only compact headers, pointers, cursors, queue state, and receipts.

### How `knowledge_explain(ref)` Prevents Context Bloat

`knowledge_explain(ref)` accepts two canonical reference forms:
1. **Task Outline (`mindie://DOMAIN/TASK`)**: Returns the task's current navigation summary, `current_revision`, `block_count`, and `first_block_ref`—without concatenating all blocks into a giant response.
2. **Single Material Block (`mindie://DOMAIN/TASK/blocks/<block-id>@<file-sha256>`)**: Verifies that the block belongs to the current manifest and matches its exact `sha256` digest, then returns the block's `content` along with `previous_block_ref` and `next_block_ref` for step-by-step traversal.

> [!IMPORTANT]
> **Explicit Read Error Codes**: If a block was replaced in a newer revision, the call returns `removed_or_superseded`; if the task was deleted upstream, it returns `withdrawn`; if a required file is missing or its hash mismatches, it fails explicitly as `material_corrupt` rather than returning an empty response.

---

## 5. Local BM25 Retrieval & Citation Grouping (`knowledge_query`)

`mindie-knowledge` embeds a pinned ReMe Markdown chunker, file citation graph, and BM25 lexical index:
- **Full-Block Search with Incremental Checkpoints**: Every current block body is indexed (preserving technical identifiers and Chinese token boundaries), not just short summaries. Each modified block checkpoints its compressed chunks and term counts into a single SQLite row, so service restarts restore the index without re-chunking unchanged files.
- **Single-Source Citation Grouping**: When block B cites a canonical reference (`mindie://DOMAIN/<task-id>@<revision>`) to source task A in the same domain, and source task A independently matches all query terms matched in B, `knowledge_query` folds them into a citation group anchored on A (with up to 2 related match previews and a `related_next` pagination token).
- **Pagination via Continuation Tokens**: Call `knowledge_query(continuation=related_next, limit=20)` (omitting `query` and `conditions`) to fetch remaining matches in a group.
- **Navigation, Not Certification**: Citation grouping and `related_count` help Agents navigate related troubleshooting sessions; **citation counts never boost search scores and never certify that a claim is true**.

---

## 6. Independent Usefulness Feedback (`knowledge_feedback`)

After testing a retrieved entry in real work, an Agent can call `knowledge_feedback(ref, rating, reason?)` to record an `up` or `down` vote:
- **Pinned to the Observed Revision**: You must pass the exact `feedback_ref` returned during retrieval (`mindie://DOMAIN/<task-id>@<observed-revision>`). Read references without a revision are rejected so delayed votes never attach to newer edits.
- **Privacy-Preserving Export**: Public feedback files (`feedback/*.json`) carry only a random opaque `root_id`—raw local session IDs never leave the store. Votes recorded while community sharing is off stay local (`publishable=0`) and are never backfilled.

---

## 7. Deterministic Outbox & Public Feed Sync

### Coalescing Outbox & Read-Only Reconciliation
- When the idle timer fires (default 300s) or a task deactivates, the outbox packs changed draft revisions and publishable votes into a single `mindie-contribution/1` batch. A final per-entry redaction scan quarantines any unsafe entry individually without blocking clean entries in the same batch.
- If a network interruption leaves a push or PR creation in an `unknown` state, the frozen package is retained and resolved strictly through **persisted-backoff read-only remote reconciliation** (verifying the remote PR head or Git ancestry), never blind write replays.

### Standalone Model-Free Sync (`mindie-knowledge sync`)
- `sync --config` runs with zero model calls and works even when community sharing is off.
- It streams candidate Git blobs one at a time through a persistent `git cat-file --batch` pipe, verifying `publication-contract.json`, package topology, UTF-8/LF encoding, block SHA-256 digests, and privacy rules while checkpointing verified paths per commit (resuming where it left off if interrupted).
- The active local view switches atomically in one short local transaction only after the entire candidate commit validates; upstream task deletions automatically become local withdrawal markers (`withdrawn`).
