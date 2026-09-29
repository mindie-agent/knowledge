# MindIE domain loop

`mindie-knowledge` owns deterministic capture, local storage and contribution.
Current adapters select `capture_mode: public-transcript`, their own
`transcript_adapter`, an installed `redactor_executable`, and an optional
`summary_command`. They also supply `root`, `domain`, `community_config` and
`admission_path`. Model choice is adapter-owned implementation, not a user
configuration step. Legacy `agent_command` is retained only for old installations
and stored recovery records; current setup and update never select that path.

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
the Hook short-circuits and creates no capture, cursor, draft or organizer
call. Read-only retrieval, plugin updates and knowledge sync keep working; an
existing service or local retrieval cache does not imply capture is enabled.
Disabling mid-task cancels queued and running maintenance, the idle batch
timer and unsent batches; it never deletes drafts or published data, and
re-enabling never backfills the disabled period. An *unknown* authority state
— the settings file or the named consent document missing, unreadable,
corrupt or malformed — is a fault, not a revocation: no new read, model call
or outbound write happens, but already-received captures, pending gap
recoveries and saved apply results are parked with a persisted bounded
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

An optional separate worker reads the complete redacted body and returns only
title and summary. It never holds the body worker, and an old result cannot
replace metadata after new body content arrives. One settled body version has
one attempt. Failure keeps a labeled source excerpt and the full body usable.
Disabling sharing cancels both workers and unsent publication; an unchanged
enable does nothing. Consent lasts until the user changes it.

Old model-authored checkpoints and failed regions are preserved as historical
recovery evidence. Switching to public-transcript mode never silently applies
their model-authored body as new deterministic material.

## Entries, drafts, publication

`loop/documents.py` owns the canonical `mindie-entry/2` format: YAML
frontmatter with `schema`, `entry_id`, `domain`, `kind`, `title`, `summary`
and optional `conditions`; the detailed body as Markdown. There is no public
`revision`, `producers`, `sources`, `status` or `retirement_reason` — the
internal content `revision` is computed at parse/write as the SHA256 of the
canonical JSON of the public semantic fields (body included), and draft
ownership lives in a private entry-owner relation. Text fields are canonical
(stripped) at admission — noncanonical documents are rejected, never silently
rewritten, so render/parse/revision always agree. Duplicate YAML keys and
unknown fields fail loudly.

`title` names the case and `summary` is its short retrieval abstract.
`conditions` contains only observed software versions or source commit IDs
(for example `torch_version` or `vllm_ascend_commit`); use an empty map when
unknown. Hardware, topology, configuration, shape, seed, epsilon, device
mapping, tolerances and applicability limits belong in the detailed body.
Observed versions do not establish universal compatibility. Experience
queries return this context without excluding a case
because a requested condition differs. Reference `knowledge` entries can be
filtered by conflicting caller-supplied conditions. Neither path replaces the
agent's assessment of the detailed evidence and limits.

Search folds draft and published lineage: the published revision wins and
the returned `origin` describes that visible revision (`feed`), including
the author's own contribution after synchronization. A local-only hit is
`draft`. A draft that advances beyond its published revision is labeled `supplemental`,
never a second hit. Withdrawal is deletion from the upstream main tree: after
a successful sync the entry leaves ordinary search and is never resurrected
by its local draft, while retained pinned reads return an explicit
`withdrawn` flag with a readable note. Query references pin short 16-hex
entry/revision prefixes (`mindie://<domain>/<entry>@<revision>`, full hashes
only on the rare collision). A retained reference reads its exact body;
superseded local draft references expire rather than reading a newer body.
Drafts keep only the latest content and sharing grant. The outbox owns the
payload of a pending send, so it does not require draft history. Existing
stores discard superseded draft rows once on opening; freed database pages
are reused without a full database rewrite on each append. Published cached
revisions remain readable. Ambiguous prefixes fail instead of guessing.

### Local retrieval index

The existing `store-v3.sqlite3` contains a derived FTS5 index. Entries and
their revisions remain authoritative; the index does not store a second
copy of the body. Tokenization retains qualified identifiers, their aliases
and Chinese bigrams. Search uses the index, filters current visibility and
knowledge conditions before limiting results, then reads the matching
entries. Scores rank retrieval usefulness and do not measure factual
confidence.

Content changes update the derived index. An older cache builds its index in
resumable slices through the existing background worker, including when
community contribution is off. This local work does not capture a task,
call a model or submit a contribution. While a complete index is unavailable,
queries return an explicit readiness rejection instead of an empty or
partially searched corpus. The background work continues without a user
command or repeated queries; optional retrieval context must not prevent
an otherwise valid capture from being processed. The existing RPC deadline
remains unchanged.

The runtime requires SQLite 3.43.0 or newer with FTS5 and
`contentless_delete` support. The SQLite library used by the selected Python
interpreter determines this capability; a Python version alone does not.

## Optional feedback

`knowledge_feedback(ref, rating, reason?)` records one current `up`/`down`
vote per opaque root and entry (a new vote replaces the old, including its
revision); the reason is optional, at most 1000 characters. Raw native
session IDs never leave the store — public exports carry only the random
opaque root ID. Votes recorded while sharing is off stay local
(`publishable=0`) and are never backfilled; a vote while off also never wakes
capture or the outbox.

## Automatic contribution delivery

One coalescing outbox per domain: the idle timer (default 300 s from the
settings file; task deactivation flushes early) packs every changed draft
revision and unbatched publishable vote into a single `mindie-contribution/1`
batch — canonical entry Markdown under `cases/`/`topics/`, one
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
remote body that moved (bot/maintainer edits) receives only the
not-yet-confirmed observation blocks — the current remote body, header
included, stays authoritative. The model is never involved in batching,
commit messages or PR text.

## Knowledge sync

`sync --config` is standalone and model-free (30 s per attempt): it follows
the configured content repository branch as an immutable Git commit,
validates the candidate tree (canonical layout under `cases/`+`topics/`,
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
re-checked against the admission store. Bodies are no longer size-capped by a
business limit, so `explain` paginates long content by default (8 Ki
characters per page, explicit `limit` up to 32 Ki characters) using the
existing `offset`/`limit` shape with `content_offset`, `content_length` and
`next_offset` continuations — a per-call wire budget, not a document cap.

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
sent private payload (draft bodies/history, raw capture summaries, staging)
of a confirmed batch while keeping IDs, hashes, the retrieval header and
PR/head receipts. None of them reruns the organizer, resets a capture cursor
or replays failed model attempts.

Shutdown cancels in-flight maintenance through the shared cancel event,
drains the queue as never-attempted, and joins workers with bounded waits.
Process bounding is portable on POSIX (process groups); on Windows the Job
Object assignment races the already-running child, so reliable tree ownership
there is NOT proven and awaits an atomic create/assign/resume sequence plus
real Windows acceptance.

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

The v2 public format uses a fresh `store-v3.sqlite3`; old private database and
Markdown files are not imported, opened as active records, or deleted. Its
persisted capture floor is combined with sharing and activation timestamps,
so a fresh store cannot backfill transcript material from the previous format.
Entry filenames use the stable entry ID, so correcting a misleading title
updates the same file. Pending contributions are rechecked for withdrawal,
and a remote deletion of an expected base is a conflict, never permission to
restore the removed body. Existing failed or unknown publication receipts stay
non-replayable within this format.


## Confirmed payload cleanup and idle updates

Confirmation requires a matching remote PR head or Git ancestry proof that
the expected commit reached our PR. Uncertain writes stay `unknown`,
preserving inspection material and preventing blind replay; persisted backoff
spaces the read-only checks without ever latching. Automatic cleanup removes
exactly the sent draft payload and staging. Capture summaries are cleared only
when all recorded entry-and-revision references are covered by that confirmed
batch; newer unsent and ambiguous observations remain available.

Tiny per-entry receipts are sending history: the content identity actually
committed (as reported by the publisher, which can differ from the candidate
payload after a bot/maintainer merge edit), the cumulative confirmed
observation-marker set, the confirmed head, path, revision, PR and
contribution generation — kept independently of the latest coalescing batch.
After A is sent and compacted, a later B-only batch does not erase A's
receipt. A future A update checks the linked PR's actual state and re-reads
the current upstream main body after a merge, or the verified upstream PR
head while it is open, and appends only there. Squash and rebase merges use
the same rule. Closed-unmerged PRs are not restored. A cached published body
or retained contribution branch cannot prove that a remote entry still
exists; an old confirmed head is never reseeded over a remote correction, and
confirmed marker identities prevent re-attaching a submitted observation the
remote removed. Normal organizer context includes the compacted entry's
title, summary and sent revision with an empty excerpt, restricted to the same
task and contribution generation. Reading this header neither fetches the
body nor makes it a pending draft; restoration happens only when the organizer
actually extends that entry. Withdrawn entries remain excluded.
This does not retain redundant local body history or authorize
publishing into a different contribution scope.

The authenticated local `stop_if_idle` RPC freezes admission and initiates
shutdown only when no actual call, worker or admitted capture work remains.
Idle authorization grants and pending/unknown durable PR receipts alone do
not block a version switch. Adapters protect each complete call with their
operation lock and use this RPC instead of status-then-stop inference.
