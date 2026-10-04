# MindIE domain loop

`mindie-knowledge` owns deterministic capture, local storage and contribution.
Current adapters select `capture_mode: public-transcript`, their own
`transcript_adapter`, an installed `redactor_executable`, and an optional
`summary_command`. They also supply `root`, `domain`, `community_config` and
`admission_path`. Model choice is adapter-owned implementation, not a user
configuration step. `public-transcript` is the only capture pipeline. The removed
`agent_command` and `organize` configuration values are rejected explicitly.

## Persistent choice and internal task binding

The native adapter is the user entry: `/mindie-agent` in Kimi/Claude Code,
or `$mindie-agent` in Codex. Its first use after installation records one
choice in the profile's `mindie-consent/1` document. Subsequent tasks, forks,
restarts, upgrades and ordinary failures reuse that choice. Binding a native
task is internal bookkeeping, not a new authorization or a manual workflow.
An explicit choice change remains possible through the same entry.

All writers serialize changes to the shared authority with one canonical
lock and reload the current document while holding it. A migration stamp
uses core normalization; damaged, foreign or rejected documents keep their
bytes and report a fault. A missing first-install document can be created;
explicit managed configuration may repair parseable current-schema values.
Ordinary reads do not migrate, repair or rewrite settings. Repeating enable or
configure preserves the generation, authorization time and accepted work. Only
an actual contribution policy change starts a new generation; runtime wiring
and delivery timing do not revoke authorization.

## The gate

`capture_allowed = active adapter lease AND community enabled AND the lease's
canonical project_root inside the configured scope AND — when the shared
config carries the consent_config extension — a saved contribute choice in the
named mindie-consent/1 authority`. The shared settings file is re-read before
transcript reading, before every model spawn and before any outbound write;
the consent document named by `consent_config` (absolute path) is re-read with
it. A missing, unreadable, corrupt or non-contribute consent closes the same
capture/model/write paths while read-only helpers keep working, and the fault
is reported as itself, never as first-time onboarding. The field grants no
permission by itself: explicit `enabled=false` always wins, and a config
without the field keeps its previous read behavior until the owning adapter
wires it at install/upgrade/entry. When community contribution is off — the
default — there is no automatic capture, extraction or sanitization at all:
the Hook short-circuits and creates no capture, cursor, draft or model
call. Read-only retrieval, plugin updates and knowledge sync keep working; an
existing service or local retrieval cache does not imply capture is enabled.
Disabling mid-task cancels queued and running maintenance, the idle batch
timer and unsent batches; it never deletes drafts or published data, and
re-enabling never backfills the disabled period. An *unknown* authority state
— the settings file or the named consent document missing, unreadable,
corrupt or malformed — is a fault, not a revocation: no new read, model call
or outbound write happens, but already-received captures, pending material
indexing and returned model results are parked with a persisted bounded
backoff and unsent batches are held, all resuming once the authority is
restored and revalidates. Repairing a damaged file is the user's explicit
action and is never re-onboarding.

## Public transcript capture

Stop forwards native identity and the transcript path. A duplicate final-answer
copy in the hook is ignored and has no size veto. The hook commits the local
notification before returning, then a worker reads the owning transcript. Hook
and process deadlines bound a stalled caller; they are not text-size limits.

Each harness owns its public-message projection. User input, visible assistant
progress and final answers remain; tool calls/results, hidden reasoning,
system/developer injections and foreign task history are excluded. A complete
public message is read whole, even when larger than a page target. Paging
limits work between records without discarding data. Malformed complete records
and unverifiable ownership/timestamps do not advance the cursor.

Gitleaks and deterministic privacy rules redact locally before storage. Body,
cursor and continuation commit together. A scanner launch failure or timeout
leaves the same notification pending and automatically retries after recovery;
no new Stop is required and no unredacted body is published. No model creates,
shortens or rewrites the body. The GitHub ordinary-Git per-file 100 MiB limit
remains a visible publication constraint, not a silent truncation rule.

A required separate worker indexes complete new blocks and a short prior
navigation using LangMem 0.0.30. Its metadata cannot rewrite the body. A batch is
bounded to 64 KiB of rendered user prompt and eight complete blocks; no block is
sampled away and there is no final whole-history model call. The native system
prompt is additional to that byte cap. The Codex adapter selects the verified
`gpt-5.6-luna` / `low` native route internally, with no automatic model fallback.

The durable summary ledger distinguishes prepared, invoking, returned, complete,
failed and outcome-unknown attempts. Returned output and usage commit before
metadata redaction or application. Scanner, authority and local-apply failures
reuse that output; interrupted invocations never automatically repeat. Explicit
retry retains previous usage. Status reports invocation attempts, known input,
cached-input and output tokens, and unknown usage; it does not infer monetary
pricing. Missing configuration and incomplete, failed or uncertain indexing block
new export batches. A saved body is not yet an indexed contribution.

Explicit selected-history import uses the same writer and queue, validates its
previously consumed source prefix and admits only the new suffix. Its package
stays quarantined until the selected snapshot has been completely admitted.
Disabling sharing cancels running work and unsent publication. Unknown authority
parks work; an unchanged enable does not revoke or restart it.

## Task packages, drafts and publication

The canonical package contains `tasks/<task-id>/index.md` and the exact referenced
`blocks/<block-id>.md` set. Its navigation manifest has a `mindie-entry/3` header,
ordered block hashes and fallible metadata. Stable blocks contain all mechanically
redacted public material; each is at most 16 KiB, without a whole-task body cap.
The complete package validator rejects missing, extra, changed or noncanonical
files. Corrections change the current navigation while earlier observations
remain in the detailed source blocks. A reference is evidence to assess, not a
certification of correctness or task completion.

Ordinary Markdown in `materials/` is the body authority. `state-v4.sqlite3` holds
small headers, pointers, cursors, permissions, queue state and receipts. Candidate
files are staged before the body-pointer/cursor transaction; promotion and cleanup
follow. A post-commit cleanup failure reports that metadata already committed.
Current draft and feed revisions remain available, while obsolete manifests and
unreferenced blocks are retired. Pending publication owns an immutable staging
package and a small descriptor, not a second database body.

After main publishes a correction, continuation adopts the confirmed current
package and only provably unsent local blocks. The local candidate must preserve
all last-sent block identities; a whole-body rewrite cannot be split into safe
additions mechanically and remains a visible conflict. Remote navigation replaces
claims about removed material, while new blocks keep their own indexes. This
operation reads manifests and file identities, not unrelated block bodies. A
small preparation receipt binds the observed feed revision to the frozen send;
confirmation after a newer feed sync cannot rewrite that base. Pending or unknown
sends remain frozen, and unfinished index results settle before reorganization.
Existing open-PR edits still require their actual current head: conflicting edits
remain `needs_review` with no write, rather than using main as a replacement base.

Search follows the visible current revision and folds draft/feed lineage.
Withdrawal comes from a successful public-feed refresh: the entry leaves search
and a local draft cannot resurrect it. Query returns a directly readable block
reference and a separate `feedback_ref` for the observed task revision.
Conditions describe observed versions or commits; they do not make an experience
universally applicable.

### Reading material

`knowledge_explain(ref)` accepts two full canonical reference forms:

- `mindie://DOMAIN/TASK`: current fallible navigation, `current_revision`,
  `block_count` and `first_block_ref`; it returns no assembled body.
- `mindie://DOMAIN/TASK/blocks/<block-id>@<file-sha256>`: one current member block,
  its `content`, `previous_block_ref` and `next_block_ref`. `current_navigation`
  and `current_revision` describe the current task separately from the fixed
  block bytes. Appending blocks or revising navigation preserves unchanged
  block references.

Task IDs, block IDs and hashes are full 64-character identities. Membership is
checked in the current manifest before opening exactly the selected block.
`removed_or_superseded` means that the selected block no longer belongs to the
current package; `withdrawn` means the task was removed upstream. Missing required
files, unsafe paths and mismatching bytes fail as `material_corrupt`, never as
empty or expired material. Old files awaiting cleanup do not grant read access.
A returned `read_ref` suggests current navigation when available; it does not
substitute another body. Historical task-revision references are accepted only
for feedback, and short references and the old `offset`/`limit` reader are removed.

### Local retrieval index

The embedded ReMe Markdown chunker, file graph and BM25 index derive their data
from current material files. They search every current block, not only the short
navigation. Technical identifiers and Chinese token boundaries retain the
repository's established tokenization. The index is replaceable and explicitly
reports load/build failures; a failed refresh does not become an empty result.
There is no ReMe agent, provider, watcher, service, separate transcript store or
model invocation in consumer retrieval. The SQLite runtime must still satisfy
the package's supported runtime prerequisite checks.

### Query matches and citation groups

`knowledge_query(query, limit?, conditions?)` returns groups of current block
matches; `limit` counts groups, from 1 to 20. Each match has its own readable
block `ref`, current task `task_ref`, and separate observed `feedback_ref`.
`match_basis` distinguishes an actual body match from fallible index metadata
or an exact identity lookup. Excerpts come from material, not generated summaries.

Only literal, full canonical MindIE references in block bodies produce `cites`.
Titles, summaries and task navigation cannot create citation relationships. A
resolvable single-source chain groups matching blocks only when the source body
independently matches and covers every query term matched in the citing body.
A newly observed term absent from the source therefore keeps its own result.
The group presents the source anchor and related matches with their own excerpts,
readable references and feedback references. Failed reuse and corrections remain
readable; a citation group and its `related_count` express navigation, not factual
confidence or independent corroboration. Citation counts never increase scores.

Historical `mindie://DOMAIN/<task-id>@<observed-revision>` literals stay unchanged in `cites`.
When that revision is unavailable, `citation_status=version_unavailable` and a
separate `current_source_ref` may identify the current task for navigation and
query-specific grouping. It does not make the historical bytes readable. Multiple
source tasks, cycles, unavailable blocks, missing sources and cross-domain
citations remain ordinary matches with citation information. Failure to read or
validate a present source is an operational failure, never a missing citation.
Exact task/block references and complete task IDs preserve the requested object
instead of redirecting to its cited source.

The first group includes a bounded related-match preview, `related_count` and,
when more matches exist, `related_next`. Continue with
`knowledge_query(continuation=related_next, limit=20)`, sending only that token and
an optional `limit`; `query` and `conditions` must be omitted. The token binds the
original query, filters, anchor and current corpus. `continuation_invalid` rejects
malformed or conflicting requests; `continuation_expired` means the corpus changed
and a new query is required. Continuation recomputes from the replaceable index
without storing durable query results or rereading unchanged block bodies.

## Optional feedback

`knowledge_feedback(ref, rating, reason?)` records one current `up`/`down`
vote per opaque root and entry (a new vote replaces the old, including its
revision). Supply the exact `feedback_ref` returned with the observation:
`mindie://DOMAIN/<task-id>@<observed-revision>`. Task and block read references are rejected,
so a delayed vote cannot silently attach to a newer task revision. A minimal
identity receipt retains only task/revision hashes after old packages are
pruned; voting does not read or retain historical bodies. The reason is optional,
at most 1000 characters. Raw native
session IDs never leave the store — public exports carry only the random
opaque root ID. Votes recorded while sharing is off stay local
(`publishable=0`) and are never backfilled; a vote while off also never wakes
capture or the outbox.

## Automatic contribution delivery

One coalescing outbox per domain: the idle timer (default 300 s from the
settings file; task deactivation flushes early) packs every changed draft
revision and unbatched publishable vote into a single `mindie-contribution/1`
batch — complete canonical task packages under `tasks/` plus
`feedback/*.json` — staged as already-scanned bytes in a private staging
directory. A batch is an internal delivery record, not a user-managed unit or
an extra approval step. One flush is one bounded in-memory operation (per-flush envelope);
material beyond it waits for the next automatic batch after this one
resolves, never dropped and never user-managed. A final per-entry outbound
scan quarantines exactly an unsafe entry instead of blocking the batch.
Local packaging failures are classified: content rejections stay quarantined
per entry; transient local failures (disk, interrupted process) persist a
backoff `next_check` and resume automatically once due — a crash can never
permanently consume unbatched material.

Core calls `mindie_knowledge.community.submit_batch` /
`reconcile_batch` and records the receipt; when the community package is not
installed while sharing is enabled, batches are marked `unavailable`
(a dependency failure, never a fake success) and resubmitted by the outbox
worker once the install recovers. Unknown outcomes get one bounded read-only
reconciliation per scheduler opportunity with a persisted backoff — no
permanent exhaustion latch; confirmation requires the exact expected head or
Git ancestry proof that the expected commit reached our PR (the head may
have advanced after a lost response). A proven closed-unmerged PR is
`rejected`: exactly that batch's entries are quarantined out of future
automatic batches (their local drafts stay readable), never resurrected by a
new PR, while unrelated material keeps flowing on a fresh branch. Failed
revisions are never automatically resent; `unavailable` (transient
environment) revisions are resubmitted with persisted backoff. An update's
expected base comes only from confirmed per-entry send receipts, and a
remote file that moved (bot/maintainer edits) is an explicit exact-base
conflict. Publication does not text-merge or overwrite that changed file. The model is never involved in batching,
commit messages or PR text.

## Knowledge sync

`sync --config` is standalone and model-free (30 s per attempt): it follows
the configured content repository branch as an immutable Git commit,
validates the candidate tree (canonical complete task packages under `tasks/`,
per-file platform envelope, UTF-8/LF, schema, revisions, domain) and switches
atomically. There is no whole-feed entry-count or total-byte cap: blobs are
read one at a time through a bounded persistent `git cat-file --batch`
process, and each verified blob is checkpointed against the exact candidate
commit, so an attempt that hits the deadline resumes after the last staged
blob — never restarting at item zero — and the visible feed switches in one
short local transaction only after the candidate completes. Transient
failures persist a backoff `next_check` and are retried automatically once
due; a structurally incompatible candidate stays quarantined against its
immutable commit; a bad candidate always keeps the old cache. A valid empty
tree empties ordinary search (withdrawal is upstream deletion); an
unsupported old layout (e.g. `corpus/`) fails loudly instead of looking like
an empty feed. Sync works with community contribution off and never starts
the maintenance service.

## MCP surface

Native MCP dispatch belongs to the harness adapters; the former core MCP host
shim (bound to Codex-only turn metadata) is retired. Core keeps the
authenticated loopback RPC the adapters forward to (`query`, `explain`,
`feedback`, `capture`), each still bound to a verified per-call identity and
re-checked against the admission store. `explain` takes only `ref` and reads
one existing material block per request. Adjacent block references allow explicit
traversal without assembling a whole task or discarding long-task content.
Read rejection codes and material corruption remain visible through the RPC
boundary; they do not become successful empty responses.

## Commands

```sh
mindie-knowledge serve --config domain.json      # foreground service
mindie-knowledge status --config domain.json     # live or local read-only status
mindie-knowledge sharing-status --config domain.json
mindie-knowledge sync --config domain.json       # one bounded knowledge sync
mindie-knowledge stop --config domain.json
mindie-knowledge hook --config domain.json       # Stop envelope on stdin
mindie-knowledge contribution-inspect --config domain.json --batch ID
mindie-knowledge contribution-reconcile --config domain.json --batch ID
mindie-knowledge contribution-retry --config domain.json --batch ID
mindie-knowledge contribution-compact --config domain.json --batch ID
```

The contribution operations are deterministic and model-free: inspect is
read-only (loop outbox + community ledger); reconcile runs the bounded
read-only remote inspection and updates both stores (always available;
automatic checks are spaced by persisted backoff, never exhausted); retry
resubmits exactly one confirmed failed or rejected stored payload with
`explicit_retry` (unknown outcomes are refused); compact removes the
resolved staging of a confirmed batch while keeping the candidate until
the same revision is present in the public feed. IDs, hashes and PR/head receipts
remain. None of these operations reruns a model, resets a capture cursor
or replays failed model attempts.

Shutdown cancels in-flight maintenance through the shared cancel event,
drains the queue as never-attempted, and joins workers with bounded waits.
Process bounding uses POSIX process groups and Windows suspended creation
followed by Job assignment and resume. The Windows mechanism has separate
component evidence; this change does not claim new native Windows acceptance.

### Deferred discovery and trusted publication validation

Each feed refresh has a 30-second execution budget. Git output and process
ownership are bounded. Discovery makes one bounded pass per sync — no
internal retry loop. Three consecutive transport failures before resolving
the remote commit defer ordinary discovery for one hour (the updater's
established post-failure cadence), so frequent callers do not hammer the
network; this is a deferral, never a permanent latch. Once the backoff is
due, an ordinary sync discovers again, and a successful discovery clears
the transient failure count. State persisted before this deferral existed
carries no due time and is retried immediately. An operator may run
`mindie-knowledge sync --config CONFIG --resume` to skip the deferral and
to recheck a backed-off candidate now. This
does not replay failed model work or revalidate an invalid candidate:
incompatible content stays quarantined against its immutable commit.

Content CI invokes the pinned installed package with
`python -I -m mindie_knowledge.publication_check --repo CHECKOUT --revision SHA`.
The validator reads immutable Git blobs through one bounded persistent
`git cat-file --batch` process, accepts an empty publication, and checks
canonical documents, feedback, file modes, the real per-file platform
envelope and private-data findings — with no whole-publication file-count or
total-byte cap and no fixed whole-tree metadata cap (the listing streams
through a scratch file). Verified results checkpoint per path, bound to the
exact commit and the exact validation context (domain, pinned validator
version, privacy-rule set, schemas); a normal call advances in bounded slices
and auto-continues within the invocation, never restarting a large fixed
commit at item zero and never asking the caller to rerun. `--state FILE`
overrides the default scratch checkpoint in the candidate repo's Git dir.
Candidate repository Python is never imported or executed.

### Pre-release format boundary

The current material format uses fresh `state-v4.sqlite3` metadata and Markdown
packages. Old private databases and organizer checkpoints are not imported or
used for recovery. The persisted capture floor combines sharing and activation
timestamps, so normal Stop capture does not backfill earlier material. Explicit
selected-history import is a separately authorized source operation. Existing
failed/unknown current-format publication receipts remain non-replayable.

## Confirmed payload cleanup and idle updates

Publication confirmation requires the expected remote head or verified ancestry.
Uncertain writes retain their exact frozen package and use read-only reconciliation;
they never authorize a blind repeated write. An open confirmed PR permits staging
cleanup but keeps the current local candidate readable. The draft pointer is
compacted only when its exact revision is present in the synchronized public feed.

Per-entry receipts retain the confirmed file identities, head, revision, PR and
contribution generation independently of the latest coalescing batch. Replacing a
lineage batch cannot erase another task's sending history. A later continuation
uses its current draft or retained current feed package as the base, preserving
all earlier blocks. There is no organizer-triggered remote restoration path.
Successful upstream withdrawal blocks resurrection, and exact-base publication
checks surface remote edits instead of silently merging them.

The authenticated local `stop_if_idle` RPC freezes admission and initiates
shutdown only when no actual call, worker or admitted capture work remains.
Idle authorization grants and pending/unknown durable PR receipts alone do
not block a version switch. Adapters protect each complete call with their
operation lock and use this RPC instead of status-then-stop inference.
