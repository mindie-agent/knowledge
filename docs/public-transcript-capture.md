# Deterministic public transcript capture

An adapter may explicitly select `capture_mode: public-transcript`, provide its
own `transcript_adapter`, and name an installed `redactor_executable`. Existing
adapters retain their current organizer path; the core never infers a harness
format or silently switches other clients.

The handoff queue retains only the native transcript reference, not an extra
raw copy of the final answer. The parser supplies authorized public messages and exact source-byte ranges.
The core uses a checksum-pinned Gitleaks 8.30.1 release plus its existing privacy
rules before saving the messages. Setup calls
`python -m mindie_knowledge.loop.transcript_redaction` to install the supported
platform binary and MIT license; runtime capture never downloads a component.
A scanner failure leaves the source cursor unchanged and an explicit failure.
Known task/home prefixes and Windows, WSL and file-URI profile paths are masked
as well as rule-defined credentials. Installer output is UTF-8 on every OS.

One native session within one sharing generation owns one appendable entry.
Body, range, cursor and continuation commit together using the existing Store
transaction. Body creation consumes no maintenance model budget. Existing
authorization checks, revocation, fork filtering, receipt-based restoration,
outbox, publication and query interfaces continue to apply. The database is
authoritative; the search index is a derived view. Capture does not also rewrite
a full Markdown mirror on every turn. Publication renders the selected database
revision into its staging file when needed. Old draft-file mirrors are inert;
confirmed-publication cleanup still removes them.
On migration, legacy organizer gaps remain failed and uncommitted legacy model
results remain held for inspection. Neither recovery route can call a body
model or apply its result in public-transcript mode. Appends refresh the source
excerpt and excerpt title so retrieval does not keep showing an earlier superseded observation.

Title/summary organization is required for a transcript contribution. A missing
`summary_command` is a configuration failure. The local body can still be saved,
but pending, failed, missing or stale summary work cannot enter a new export batch.
An excerpt is only a local unprocessed preview, never a replacement for completion.
The worker receives redacted source text and must return exactly `title` and
`summary`; it cannot rewrite the body. Compare-and-apply preserves the exact saved
body. Attempts are coalesced and reserved before spawning. Failure is visible in
summary status, including safe process categories; source-bearing errors are not
persisted. Unavailable authority parks work and revocation prevents application.

The current implementation sends the complete body. It has no token-aware long
input organization path. First/last excerpts are not a valid substitute for
complete organization. Incremental semantic body organization remains unimplemented.

The scanner detects rule-defined secrets and identifiers, not proprietary
meaning. Existing project authorization is required. A downstream review bot
is not a pre-upload secret boundary. Body capture, required metadata organization and publication readiness are
reported separately.
