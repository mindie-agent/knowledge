# Public transcript material and incremental indexing

`public-transcript` is the only capture pipeline. The adapter provides its own
`transcript_adapter`, an installed absolute `redactor_executable`, and the
`summary_command` used for required retrieval metadata. The removed
`agent_command` and `organize` configuration values fail explicitly. The core
never guesses a harness format.

The handoff queue stores only the native transcript reference. The parser selects
authorized visible user and assistant messages with exact source-byte ranges;
hidden reasoning, system/developer messages, tool traffic and foreign tasks are
excluded. Gitleaks 8.30.1 plus the deterministic privacy rules run before storage.
The installer verifies the pinned binary and license; capture never downloads a
scanner. Private-key redaction state crosses page boundaries. Scanning has no
default total deadline or report truncation. Shutdown cancellation stops the owned
scanner process tree, preserves pending work and leaves the source cursor unchanged.
Explicit authority revocation remains cancelled rather than pending.
Scanner failure also leaves the cursor unchanged and schedules only local deterministic work.
The scanner detects rule-defined secrets and identifiers, not proprietary meaning.

One authorized task owns stable material blocks and a current navigation file.
Bodies live in ordinary Markdown files under the local `materials/` directory.
The existing SQLite `state-v4.sqlite3` stores pointers, source cursors, authority,
queue state and outcome receipts; it does not keep another transcript body.
A complete new public message is divided into lossless UTF-8 blocks of at most
16 KiB. Candidate files are prepared before the pointer/cursor transaction;
current-file promotion and cleanup follow that committed transaction. A cleanup
failure remains a separate visible fault and cannot claim that the body write
never happened. Superseded manifests and unreferenced blocks are retired while
current draft/feed versions remain readable.

Stop capture and explicit selected-history import use this same material writer
and summary queue. An import validates the consumed prefix before extending its
cursor and quarantines its package until the selected snapshot is fully admitted.
Repeating an unchanged import makes no new model call. The fresh format does not
migrate old organizer bodies, checkpoints or paid recovery work.

LangMem 0.0.30 indexes complete new blocks together with short prior navigation.
It returns exactly one title/summary pair per admitted block and a current task
navigation summary. It cannot rewrite source material. A batch contains at most
eight blocks and at most 64 KiB of the rendered user prompt; partitioning retains
every block, without head/tail selection or a final whole-history model merge.
Native Harness system tokens are additional and are included in measured usage.
The Codex worker explicitly uses `gpt-5.6-luna` with `low` effort; the selected
native CLI route was verified during this implementation's acceptance work.
There is no runtime model fallback or separate provider credential.

A durable `SummaryLedger` reserves each invocation before launch. The worker
returns a bound envelope identifying the task, batch, policy and outcome.
Returned output and known token usage are committed **before** metadata scanning
or application. A later scanner, authority or local-apply fault reuses that saved
result and cannot trigger another paid call. A failed or interrupted invocation
is respectively failed or outcome-unknown; it needs explicit repair/retry.
Retry keeps the previous attempt and its usage. Missing usage stays unknown,
never zero. `model_calls` counts native invocation attempts, not independently
observed provider-internal requests. Token counters are reported; no monetary
price or dollar cost is inferred from them.

The 90-second invocation deadline, bounded process output and strict schema guard
one invocation. Rolling call/input-token admission limits constrain new work;
local recovery of returned output needs no fresh budget. Missing worker
configuration, incomplete indexing and failed/unknown batches block publication.
They do not turn a saved body or an excerpt into an indexed contribution.

ReMe's embedded Markdown chunker, file graph and BM25 index search all current
material blocks, including middle-of-task evidence absent from the short
navigation. The derived index can be rebuilt from those files. Retrieval and
consumer sync make no model calls and start no ReMe service or background agent.

Publication sends the entire canonical package as `tasks/<task-id>/index.md`
and its exactly referenced `blocks/<block-id>.md` files. A confirmed open PR
retires staging but keeps the local candidate. Once the same revision is present
in the public feed, its duplicate draft pointer may be compacted. A later task
continuation appends to the retained current feed package. Upstream withdrawal
prevents continuation from resurrecting removed content. Navigation and retrieved
experience remain fallible reference material, including incomplete work and
later corrections; neither publication nor retrieval certifies an outcome.
