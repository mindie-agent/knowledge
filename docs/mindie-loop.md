# MindIE domain loop

`mindie-knowledge` is the single-domain runtime. Configuration requires `root`
and `domain`; optional keys are `agent_command` (argv for the maintenance
runner), `community_config` (shared `mindie-community-config/1` settings file),
`admission_path` (the harness's explicit neutral admission SQLite file, owned
by core's `Admission` API), `transcript_adapter` (absolute local parser module
path exporting `FileIdentity`/`identify`/`read_material`; without it capture
is honest summary-only) and `feeds`. The legacy `session_activation` adapter
config indirection is rejected, not aliased.

## The gate

`capture_allowed = active adapter lease AND community enabled AND the lease's
canonical project_root inside the configured scope`. The shared settings file
is re-read before transcript reading, before every model spawn and before any
outbound write. When community contribution is off — the default — there is no
automatic capture, extraction or sanitization at all: the Hook short-circuits,
no capture row/cursor/draft/worker/model exists, and only read-only retrieval,
plugin updates and knowledge sync keep working. Disabling mid-task cancels
queued and running maintenance, the idle batch timer and unsent batches; it
never deletes drafts or published data, and re-enabling never backfills the
disabled period.

## Capture and bounded increments

The Stop Hook (`hook --config`) validates the bounded envelope, re-checks the
gate read-only and forwards whitelist fields to the already-running loopback
service; it never opens the transcript, starts a service, or retries. A valid
event carries `session_id` and `turn_id`; `transcript_path` and
`last_assistant_message` are both optional — a transcript without a final
summary is still accepted.

The worker reads only the new byte region of the admitted task's own
transcript through the configured `transcript_adapter` module (an absolute
local path loaded once at service start; core ships no native record parser —
the Codex parser is the adapter deliverable, the Kimi parser belongs to the
Kimi adapter). A missing parser means honest summary-only behavior, never a
format guess. The adapter parser applies its structural public-material
allowlist; hidden reasoning, system/developer content, credential fields and
other tasks' history are never extracted. File
replacement, truncation, unknown formats and partial trailing records are
handled explicitly (summary-only degradation within the same attempt, or a
visible coverage gap); a nonzero cursor resumes exactly where the last region
ended. Each region `(file identity, start, end, digest)` is durably reserved
BEFORE the model call, so failures, crashes and cancellation all consume it;
the attempted cursor and the last-successful cursor are separate, and failed
regions stay visible as coverage gaps (`status` shows them). Local scans are
bounded to 16 MiB / 2 seconds and a 48 KiB public text envelope. Noise advances
the durable cursor without calling a model. A public record that does not fit
stays at the next cursor; head/tail field clipping and oversized record skips
are explicit coverage gaps. Reads validate task identity, inode and prefix on
the same file handle, including nonzero offsets.

One organizer model call per accepted increment (input 64 KiB, structured
result 32 KiB, runner 120s/outer 125s, one concurrent call, 6 per task-hour, 20 per
domain-hour, pause after three consecutive failures; attempts are persisted
before spawn and never replayed). Quota-deferred material stays unattempted;
its persisted continuation survives restart. Only fresh/unattempted regions
resume automatically; interrupted or failed model regions never replay.
A deterministic redaction mask runs before
the model, and every candidate entry is scanned again before becoming a
draft. The organizer records actual actions and observations from the supplied
material. It preserves useful commands, numbers, errors and source-stated
uncertainty without extracting lessons, inventing causes or forcing a
failure-fix-success story. Unmentioned details are omitted, not listed as
unknown. Title and summary are neutral retrieval introductions.
Organizer output is at most three entries: `entry_id: null` creates a
draft owned by the producing task's opaque identity; a non-null id appends a
self-contained observation to that draft, deduplicated by the increment
marker. Corrections append to the body and update the current retrieval header;
previous pinned revisions remain unchanged. Context selection includes recent
correction tails and matches task-owned headers to the current material.
Bodies are capped at 64 KiB (`draft full` stops expansion — no
auto-condense or extra model).

For explicit local historical experiments, `python -m
mindie_knowledge.loop.history plan --source FILE --session-id ID --output DIR`
creates bounded public-material packets and a resumable source snapshot.
These are private local planning files, not experiences: this command never
starts a model/service, creates a publication grant or uploads anything.
Normal capture never invokes historical planning or backfills sharing-off time.

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

Search folds draft and published lineage: the published revision wins, and a
draft that advances beyond its published revision is labeled `supplemental`,
never a second hit. Withdrawal is deletion from the upstream main tree: after
a successful sync the entry leaves ordinary search and is never resurrected
by its local draft, while retained pinned reads return an explicit
`withdrawn` flag with a readable note. Query references pin short 16-hex
entry/revision prefixes (`mindie://<domain>/<entry>@<revision>`, full hashes
only on the rare collision) and always read the exact historical body;
ambiguous prefixes fail instead of guessing.

## Optional feedback

`knowledge_feedback(ref, rating, reason?)` records one current `up`/`down`
vote per opaque root and entry (a new vote replaces the old, including its
revision); the reason is optional, at most 1000 characters. Raw native
session IDs never leave the store — public exports carry only the random
opaque root ID. Votes recorded while sharing is off stay local
(`publishable=0`) and are never backfilled; a vote while off also never wakes
capture or the outbox.

## Contribution batches

One coalescing outbox per domain: the idle timer (default 300 s from the
settings file; task deactivation flushes early) packs every changed draft
revision and unbatched publishable vote into a single `mindie-contribution/1`
batch — canonical entry Markdown under `cases/`/`topics/`, one
`feedback/*.json` — staged as already-scanned bytes in a private staging
directory. Core calls `mindie_knowledge.community.submit_batch` /
`reconcile_batch` and records the receipt; when the community package is not
installed while sharing is enabled, batches fail loudly as `unavailable`
(a dependency failure, never a fake success). Failed/unknown batch revisions
are never automatically rewritten; unknown outcomes get bounded read-only
reconciliation before any new work. The model is never involved in batching,
commit messages or PR text.

## Knowledge sync

`sync --config` is standalone and model-free (30 s per attempt, 3 attempts
per candidate persisted across restarts): it follows the configured content
repository branch as an immutable Git commit, validates the complete
candidate tree (canonical layout under `cases/`+`topics/`, sizes, UTF-8/LF,
schema, revisions, domain) and switches
atomically. A valid empty tree empties ordinary search (withdrawal is
upstream deletion); an
unsupported old layout (e.g. `corpus/`) fails loudly instead of looking like
an empty feed; a bad candidate always keeps the old cache. Sync works with
community contribution off and never starts the maintenance service.

## MCP surface

Native MCP dispatch belongs to the harness adapters; the former core MCP host
shim (bound to Codex-only turn metadata) is retired. Core keeps the
authenticated loopback RPC the adapters forward to (`query`, `explain`,
`feedback`, `capture`), each still bound to a verified per-call identity and
re-checked against the admission store.

## Commands

```sh
mindie-knowledge serve --config domain.json      # foreground service
mindie-knowledge status --config domain.json     # live or local read-only status
mindie-knowledge sharing-status --config domain.json
mindie-knowledge sync --config domain.json       # one bounded knowledge sync
mindie-knowledge maintenance-resume --config domain.json
mindie-knowledge stop --config domain.json
mindie-knowledge hook --config domain.json       # Stop envelope on stdin
mindie-knowledge contribution-inspect --config domain.json --batch ID
mindie-knowledge contribution-reconcile --config domain.json --batch ID
mindie-knowledge contribution-retry --config domain.json --batch ID
mindie-knowledge contribution-compact --config domain.json --batch ID
```

The contribution operations are deterministic and model-free: inspect is
read-only (loop outbox + community ledger); reconcile runs the bounded
read-only remote inspection and updates both stores (available even after the
automatic read budget is exhausted); retry resubmits exactly one confirmed
failed stored payload with `explicit_retry` (unknown outcomes are refused); compact removes the
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
to grant a transiently exhausted candidate one fresh bounded round. This
does not replay failed model work or revalidate an invalid candidate:
incompatible content stays quarantined against its immutable commit.

Content CI invokes the pinned installed package with
`python -I -m mindie_knowledge.publication_check --repo CHECKOUT --revision SHA`.
The validator reads immutable Git blobs, accepts an empty publication, and
checks canonical documents, feedback, file modes, sizes and private-data
findings. Candidate repository Python is never imported or executed.

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

Confirmation requires a matching remote PR head. Exhausted read-only
reconciliation leaves an uncertain write `unknown`, preserving inspection
material and preventing blind replay. Automatic cleanup removes exactly the
sent draft payload and staging. Capture summaries are cleared only when all
recorded entry-and-revision references are covered by that confirmed batch;
newer unsent and ambiguous observations remain available.

Tiny per-entry receipts retain the confirmed head, path, hash, revision,
PR and contribution generation independently of the latest coalescing batch.
After A is sent and compacted, a later B-only batch does not erase A's
receipt. A future A update retrieves the exact prior remote body once before
appending. Normal organizer context includes the compacted entry's retained
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
