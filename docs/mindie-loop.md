# MindIE domain loop

`mindie-knowledge` is the single-domain runtime. Configuration requires `root`
and `domain`; optional keys are `agent_command` (argv for the maintenance
runner), `community_config` (shared `mindie-community-config/1` settings file),
`session_activation` (adapter config for lease checks) and `feeds`.

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
transcript (`loop/transcript.py`): structural-signature whitelist of Codex
JSONL — user messages, public assistant commentary/final/final_answer messages,
function and custom-tool input/output with call identities. Harness catalogue
wrappers and duplicate native event wrappers are excluded. A native fork's
creation time excludes inherited parent material. Hidden reasoning, analysis channels, system/developer content,
credential fields and other tasks' history are never extracted. File
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
draft. Organizer output is at most three entries: `entry_id: null` creates a
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

`title` names the finding and `summary` is its short retrieval abstract.
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

`knowledge_query(query, limit?, conditions?)`, `knowledge_explain(ref,
offset?, limit?)` (both truthfully annotated read-only) and
`knowledge_feedback(ref, rating, reason?)` (a write). Every delivered call is
bound to the host's per-call metadata (`_meta['x-codex-turn-metadata']` with
matching `threadId`); missing or contradictory metadata fails closed — there
is no latest-lease guess. Discovery (`initialize`/`tools/list`) is static and
starts nothing.

## Commands

```sh
mindie-knowledge serve --config domain.json      # foreground service
mindie-knowledge status --config domain.json     # live or local read-only status
mindie-knowledge sharing-status --config domain.json
mindie-knowledge sync --config domain.json       # one bounded knowledge sync
mindie-knowledge maintenance-resume --config domain.json
mindie-knowledge stop --config domain.json
mindie-knowledge hook --config domain.json       # Stop envelope on stdin
mindie-knowledge mcp --config domain.json
```

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
