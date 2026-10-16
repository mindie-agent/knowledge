# MindIE Knowledge

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-0284C7.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-0F766E.svg)](LICENSE)
[![Corpus License: CC-BY-4.0](https://img.shields.io/badge/corpus-CC--BY--4.0-0D9488.svg)](LICENSE-CORPUS)

[中文](README.md) | English

**MindIE Knowledge** (Python package `mindie-knowledge`, module namespace `mindie_knowledge`) is the shared domain knowledge and task-experience loop runtime for [MindIE Agent](https://github.com/mindie-agent/mindie-agent). It provides bounded public-transcript capture from admitted tasks, incremental block indexing and local BM25 retrieval, privacy-redacted community sharing through Git, and read-only synchronization plus independent usefulness feedback against canonical Git publications.

This project inherits and enforces all nine [MindIE Agent / VAWS design principles](https://github.com/mindie-agent/mindie-agent/blob/main/docs/design-principles.md): measuring efficiency by the total cost of achieving the user's goal, keeping knowledge strictly informational, activating tools on demand within single well-defined boundaries, and keeping all errors visible without silent fallbacks.

---

## Key Capabilities

- **Canonical Public Knowledge Sync (`sync`)**: Pulls immutable task packages from upstream Git publications governed by `publication-contract.json` (with SHA-256 pinning), validating blobs in bounded streaming passes and switching the active local view atomically with zero model calls.
- **Bounded Capture & Local BM25 Retrieval**: Authoritative material bodies live as ordinary Markdown files under `materials/`, while `state-v4.sqlite3` stores only compact headers, pointers, cursors, queue state, and receipts. Pinned ReMe components provide local Markdown chunking, a file citation graph, and BM25 retrieval without starting any daemon, watcher, model call, or external account service.
- **Fail-Closed Privacy Redaction**: Capture runs only when community contribution is explicitly enabled by the user. Both local intake and outbound staging enforce deterministic redaction via pinned Gitleaks and `mindie_knowledge.redact` (ruleset `r3`), failing closed on any credential, private key fragment, internal IP/hostname, or personal filesystem path.
- **Deterministic Outbox & Read-Only Reconciliation**: Coalesces indexed task drafts and publishable feedback votes into `mindie-contribution/1` batches for GitHub PR delivery. Uncertain or interrupted network writes are resolved exclusively through persisted-backoff read-only remote reconciliation, never blind write replays or repeated paid model invocations.
- **Independent Usefulness Feedback**: Records optional `up` / `down` votes with an optional one-line reason bound to the exact observed task revision (`mindie://DOMAIN/<task-id>@<observed-revision>`), with no judge model, no voting weights, and no claim that votes certify technical correctness.

---

## Architecture & Data Layers

`mindie-knowledge` separates runtime data into three orthogonal layers under `<root>/<domain>/state-v4/`:

| Layer | Authoritative Storage | Responsibilities & Boundaries | Model & Network Constraints |
|---|---|---|---|
| **Public Feed Layer** | Shallow Git tip cache + verified `tasks/<task-id>/` package view | Follows the configured content repository branch as an immutable Git commit, validates `publication-contract.json`, `mindie-material-task/1` manifests (embedding `mindie-entry/3` headers), and `mindie-material-block/1` files, then switches atomically. Upstream deletions become local withdrawal markers. | Zero model calls; read-only Git fetch validated one blob at a time via a persistent `git cat-file --batch` pipe. |
| **Private Local Material & Index Layer** | `materials/` (Markdown bodies) + `state-v4.sqlite3` (metadata/receipts) | Stores lossless UTF-8 public message blocks (up to 16 KiB per block) from `public-transcript` capture or explicit history import. The Harness-supplied `summary_command` (LangMem 0.0.30) indexes new blocks in bounded batches (up to 8 blocks / 64 KiB) into fallible titles, summaries, and navigation; a replaceable ReMe checkpoint caches compressed block chunks and BM25 term counts. | Consumer retrieval (`knowledge_query` / `knowledge_explain`) makes zero model calls and zero network requests; indexing invokes the native worker once per new block batch. |
| **Outbox & Community Sharing Layer** | `outbox/` (private frozen staging packages + publication ledger) | Active only when both `mindie-consent/1` and `mindie-community-config/1` gates allow contribution. Freezes fully indexed task packages and `feedback/*.json` (`mindie-feedback/1`) as hash-bound file views, re-scans all outbound text, and submits or reconciles upstream PRs via `mindie_knowledge.community`. | Packaging, submission, deterministic PR title/body generation, and remote reconciliation make zero model calls. |

---

## Quick Start & CLI Reference

### Requirements & Installation

Requires Python 3.11+ (with a supported SQLite runtime) and Git.

```sh
# Install the runtime package
python -m pip install -e .

# Install with test dependencies for development
python -m pip install -e '.[test]'
```

### Domain Service & Lifecycle CLI (`mindie-knowledge`)

`mindie-knowledge` (equivalent to `python -m mindie_knowledge`) provides bounded local commands taking `--config <domain.json>`:

```sh
# Run the domain knowledge service in the foreground (owns consumer.lock and local authenticated RPC)
mindie-knowledge serve --config domain.json

# Query live service status or read-only local store status (including model attempt and token counters)
mindie-knowledge status --config domain.json

# Inspect read-only community sharing status, entry counts, recent captures, coverage gaps, votes, and outbox
mindie-knowledge sharing-status --config domain.json

# Export a bounded local diagnostic snapshot (optionally scoped to a specific --session identity)
mindie-knowledge diagnostic-status --config domain.json
mindie-knowledge diagnostic-status --config domain.json --session <session-id>

# Run one standalone, model-free public knowledge sync (works with sharing off; --resume skips deferral backoff)
mindie-knowledge sync --config domain.json
mindie-knowledge sync --config domain.json --resume

# Accept a Harness Stop event envelope on stdin and request at most one detached service wake
mindie-knowledge hook --config domain.json

# Stop the running domain knowledge service
mindie-knowledge stop --config domain.json
```

### Contribution Recovery & Maintenance Commands

Manage existing contribution batches deterministically without invoking any model, resetting capture cursors, or replaying failed summary calls:

```sh
# Read-only inspection of a batch across the loop outbox and community publication ledger
mindie-knowledge contribution-inspect --config domain.json --batch <batch-id>

# Bounded read-only remote PR inspection to resolve unknown/pending outcomes and update both stores
mindie-knowledge contribution-reconcile --config domain.json --batch <batch-id>

# Explicitly resubmit one proven failed or rejected frozen batch payload (refuses unknown outcomes)
mindie-knowledge contribution-retry --config domain.json --batch <batch-id>

# Remove private staging files for a confirmed batch while keeping the local candidate until feed sync
mindie-knowledge contribution-compact --config domain.json --batch <batch-id>
```

### Contributor Submission, Publication Validator & Redaction CLIs

Standalone module entry points support contributor batch submission, content repository CI validation, and source-side privacy checks:

```sh
# Contributor-side batch submission and read-only reconciliation (prints a JSON receipt)
python -m mindie_knowledge.community submit     --settings community.json --state-dir ~/.mindie/community --batch batch.json
python -m mindie_knowledge.community reconcile     --settings community.json --state-dir ~/.mindie/community --batch-id <batch-id>

# Content repository CI validator: stream-validates immutable Git commits against contract, schemas, and redaction rules
python -I -m mindie_knowledge.publication_check     --repo <checkout> --revision <head-40-sha>     --domain vllm-ascend --base-revision <base-40-sha>     --contract-sha256 <expected-contract-sha256>

# Deterministic privacy redaction scanner (--check exits non-zero on findings; --profile prints the active ruleset id)
python -m mindie_knowledge.redact --check README.md README.en.md CONTRIBUTING.md CONTRIBUTING.en.md docs mindie_knowledge/skills
python -m mindie_knowledge.redact --profile
```

### Harness Loopback RPC & Shared Library Surface

Native Harness adapters ([Codex plugin](https://github.com/mindie-agent/mindie-agent-codex), Claude Code, Kimi) own native MCP dispatch and session identity verification, forwarding calls to the authenticated local loopback RPC:

- **`knowledge_query(query, limit?, conditions?, continuation?)`**: Searches current material blocks and folds single-source citation chains into groups (`limit` 1 to 20), returning each match's readable block `ref`, current `task_ref`, observed `feedback_ref`, original material excerpts, and `related_next` continuation tokens.
- **`knowledge_explain(ref)`**: Accepts two full canonical reference forms:
  - `mindie://DOMAIN/TASK`: returns current fallible task navigation, `current_revision`, `block_count`, and `first_block_ref` (no assembled whole-task body);
  - `mindie://DOMAIN/TASK/blocks/<block-id>@<file-sha256>`: verifies current manifest membership and exact SHA-256 before returning the block's `content`, `previous_block_ref`, and `next_block_ref`.
- **`knowledge_feedback(ref, rating, reason?)`**: Records one current `up` or `down` vote (with an optional reason up to 1000 chars) bound to `mindie://DOMAIN/<task-id>@<observed-revision>`. Rejects read references so delayed votes never attach to newer revisions.
- **`mindie_knowledge.loop.history_import.import_transcript`**: Shared intake behind explicit historical transcript import, applying the same redaction, lossless block splitting, and full-snapshot admission gate as live capture.
- **`mindie_knowledge.releases`**: Parses `mindie-plugin-release.json` assets and `mindie-harness-compatibility/1` registries so Harness updaters select the highest compatible stable release.

---

## Privacy, Redaction & Boundary Guarantees

1. **Single Explicit Opt-In & Zero Capture When Off**: The native adapter asks for a contribution choice once after installation and persists it in `mindie-consent/1`. Community contribution defaults to off (`enabled=false`); when off, the runtime performs no capture, extraction, sanitization, or summary model calls at all, while read-only retrieval, updates, and feed sync continue working normally.
2. **Live Authority Re-Verification**: The shared settings file and the absolute `mindie-consent/1` document are re-read before reading any transcript, before every model spawn, and before every outbound write. Missing, unreadable, or corrupt authority files fail closed and report an explicit authority fault — never triggering first-time onboarding.
3. **Public-Message Projection & Immutable Material Bodies**: Only authorized user inputs, visible assistant progress, and final answers are captured; tool calls/results, hidden reasoning, system/developer prompts, and foreign sessions are excluded. Block bodies are produced strictly by lossless UTF-8 chunking and deterministic redaction; the summary worker (LangMem) produces fallible index metadata and can never rewrite source bodies.
4. **Strict Separation of Content and Code**: Every public feed declares `publication-contract.json`. Both outbound publication and inbound sync verify the contract and its pinned SHA-256 before proceeding. Candidate repositories are inspected strictly as immutable Git blobs; Python code is never imported or executed from a content PR or feed checkout.
5. **State Durability Boundary (`state-v4`)**: `mindie_knowledge.state_layout` declares the persistent `FORMAT` (`4`, under `<root>/<domain>/state-v4`) and product boundary `RELEASE_VERSION = "0.0.1"`. Once opened by a release, compatible upgrades preserve existing material, cursors, model attempts, and publication receipts; missing or damaged layout metadata raises an explicit error rather than resetting state.

---

## Documentation Index

Detailed architecture, protocol, and platform contracts are maintained in [`docs/`](docs/README.en.md) in both Chinese and English:

| Topic | English Document | 中文文档 | Scope & Summary |
|---|---|---|---|
| **Documentation Index** | [docs/README.en.md](docs/README.en.md) | [docs/README.md](docs/README.md) | Overview and navigation for all runtime and contract documents |
| **MindIE Domain Loop Contract** | [mindie-loop.md](docs/mindie-loop.md) | [mindie-loop.zh.md](docs/mindie-loop.zh.md) | End-to-end runtime contract: gates, capture, task packages, BM25 retrieval, citation groups, feedback, and sync |
| **Public Transcript Capture & Indexing** | [public-transcript-capture.md](docs/public-transcript-capture.md) | [public-transcript-capture.zh.md](docs/public-transcript-capture.zh.md) | `public-transcript` pipeline, Gitleaks redaction, LangMem summary ledger, and operation ownership |
| **Explicit Historical Transcript Import** | [history-import.md](docs/history-import.md) | [history-import.zh.md](docs/history-import.zh.md) | `import_transcript` snapshot admission, prefix validation, resumable intake, and remote continuation |
| **Community Sharing (Contributor Side)** | [community-sharing.md](docs/community-sharing.md) | [community-sharing.zh.md](docs/community-sharing.zh.md) | `mindie_knowledge.community` credential isolation, idempotent batch submission, reconciliation, and skill validation |
| **Repository Bot Contract** | [repository-bot-contract.md](docs/repository-bot-contract.md) | [repository-bot-contract.zh.md](docs/repository-bot-contract.zh.md) | `mindie-content-review/2` external bot review, bounded redaction edits, withdrawal, and skill proposal rules |
| **Release Compatibility & File Locks** | [release-compatibility.md](docs/release-compatibility.md) | [release-compatibility.zh.md](docs/release-compatibility.zh.md) | `mindie-plugin-release/1` selection, source verification, and cross-platform OS file locking |
| **Service & Attempt Process Lifetime** | [service-process-lifetime.md](docs/service-process-lifetime.md) | [service-process-lifetime.zh.md](docs/service-process-lifetime.zh.md) | Platform process containment across Windows Task Scheduler, Linux systemd 254+, and macOS owner pipes |
| **Summary Process & Scratch Ownership** | [summary-process-ownership.md](docs/summary-process-ownership.md) | [summary-process-ownership.zh.md](docs/summary-process-ownership.zh.md) | `MINDIE_SUMMARY_SCRATCH` parent/worker contract, tree termination order, and private scratch cleanup |
| **Windows Knowledge Service Host** | [windows-service-host.md](docs/windows-service-host.md) | [windows-service-host.zh.md](docs/windows-service-host.zh.md) | Windows Task Scheduler host, current-user DPAPI one-use context transfer, and windowless stream guards |
| **Windows Process Ownership** | [windows-process-ownership.md](docs/windows-process-ownership.md) | [windows-process-ownership.zh.md](docs/windows-process-ownership.zh.md) | `spawn_owned` suspended creation, `KILL_ON_JOB_CLOSE` Job assignment, and regression matrix |
| **Framework Stability & Verification** | [framework-stability.md](docs/framework-stability.md) | [framework-stability.zh.md](docs/framework-stability.zh.md) | Everyday recovery semantics, Windows delete-sharing atomic config rename, and acceptance boundaries |

---

## Development & Testing

Run the test suite and privacy scanner from the repository root:

```sh
# Run the test suite (uses committed parser fixtures when MINDIE_FRAMEWORK_SOURCE is unset)
MINDIE_FRAMEWORK_SOURCE=tests/fixtures/production-parsers python -m pytest -q tests

# Check public documentation and packaged skills against the deterministic redaction ruleset
python -m mindie_knowledge.redact --check README.md README.en.md CONTRIBUTING.md CONTRIBUTING.en.md docs mindie_knowledge/skills
```

> **Note**: When `MINDIE_FRAMEWORK_SOURCE` is unset, `tests/conftest.py` uses the committed `tests/fixtures/production-parsers` directory and verifies all three parser files exist. An explicit path that is missing or incomplete exits pytest immediately with a setup error rather than searching a production install. `MINDIE_PARSER_KIMI`, `MINDIE_PARSER_CC`, and `MINDIE_PARSER_CODEX` can override individual parser files.

Before opening or updating a pull request, read [CONTRIBUTING.en.md](CONTRIBUTING.en.md) and complete the mandatory nine design-principles review.

---

## License

- Source code, scripts, and runtime components are licensed under the [MIT License](LICENSE).
- Knowledge corpus materials and documentation specifications are licensed under [CC-BY-4.0](LICENSE-CORPUS).
