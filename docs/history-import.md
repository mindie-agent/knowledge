# Explicit historical transcript import

`mindie_knowledge.loop.history_import.import_transcript` is the shared intake
behind a Harness's explicit history-import entry. The caller selects native
source files. Startup, Stop capture, synchronization and retrieval never discover
or import historical sessions automatically.

The requesting session must already have a current admission token and saved
contribution consent. Both the requesting scope and each source scope must be
allowed. The adapter checks this before reading source metadata; the core checks
again before every parser page and commit. Source sessions receive no new leases.
The selected file identity and byte length define a fixed snapshot; the activation
time filter is intentionally omitted for this explicitly selected history.

Each complete parser page goes through the same `prepare_increment` and
`Store.commit_material_increment` path as live capture: deterministic redaction,
lossless UTF-8 blocks, stable identities, source cursor, authorization and an index
queue. The scanner carries only an open private-key boundary across pages; no raw
secret buffer or transcript copy is retained. Native transcripts remain with the
Harness. Block bodies are Markdown files; SQLite stores metadata and receipts.

A durable intake marker prevents publication until every page of the selected
snapshot has been admitted. A malformed record, incomplete EOF, scanner failure,
source replacement or revoked authority fails visibly. Previously committed pages
remain local and resumable; they are not misreported as a complete import. A repeat
validates the consumed raw prefix and imports only its new suffix. An unchanged
repeat creates no new blocks or model calls. Prefix edits are rejected without
overwriting prior material. Version 3 databases are not read or migrated.

The shared summary worker receives complete new blocks plus short previous task
navigation. LangMem produces per-block indexes and updated navigation in one
bounded native invocation; it never rewrites source bodies. Publication requires
indexes for all blocks and an admitted complete snapshot. Import receipts report
local intake, summary status and publication status separately. The receipt's
`ref` selects current task navigation; `feedback_ref` identifies the observed
revision. Follow `first_block_ref` and adjacent block references to read material.
Missing worker
configuration, failed output validation and uncertain outcomes remain incomplete.

Returned outputs and usage are recorded before local scanning/application. A
local apply failure can resume from that saved result without another model call.
Failed or uncertain calls require an explicit `--retry-summary` request in the
Codex adapter; retry retains prior usage, and an uncertain call may already have
been billed. The normal scheduler does not automatically repeat it.

After a confirmed feed changes a task, continuation keeps its current remote
blocks and navigation plus provably unsent local blocks. This requires a local
append-only extension of the last confirmed sent package; a rewritten old body
cannot be guessed into independent additions. Prepared send receipts bind the
observed feed revision before publication, so a later feed update cannot change
the meaning of a delayed send confirmation. Pending or uncertain sends remain
frozen. Unfinished index attempts settle through their existing recovery path
before reorganization; completed per-block indexes are reused without another
model call. An edited open PR is checked at its actual current head and stays
`needs_review` without a write when it conflicts; main does not replace that PR's
base. Withdrawn public tasks cannot be resurrected by a stale draft. The normal
outbox handles frozen package submission and reconciliation; an import receipt
does not claim that GitHub publication or merge has happened. Consumers reuse the
producer's indexes and build their local ReMe index without a model call.
