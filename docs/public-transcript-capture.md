# Deterministic public transcript capture

An adapter may explicitly select `capture_mode: public-transcript`, provide its
own `transcript_adapter`, and name an installed `redactor_executable`. Existing
adapters retain their current organizer path; the core never infers a harness
format or silently switches other clients.

The parser supplies authorized public messages and exact source-byte ranges.
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
authoritative; draft files and the search index are derived views.
On migration, legacy organizer gaps remain failed and uncommitted legacy model
results remain held for inspection. Neither recovery route can call a body
model or apply its result in public-transcript mode. Appends refresh the source
excerpt so retrieval does not keep showing an earlier superseded observation.

Without `summary_command`, a clearly labeled source excerpt supplies the title
and retrieval introduction. An optional summary argv receives redacted source
text and `partial` (true for bounded first/last excerpts). It must return exactly
`title` and `summary`. There is no body field. A compare-and-apply check retains
the exact saved body and ignores a result for an obsolete body version. Attempts
coalesce after body writes and are reserved before spawning, once per version;
failure or restart retains the body and excerpt. Unavailable authority parks
the pending metadata task; revoked tasks cannot starve later active tasks.
The service reports summary states separately. Partial-source summaries receive
an enforced excerpt label even if the model omits that qualification.

The scanner detects rule-defined secrets and identifiers, not proprietary
meaning. Existing project authorization is required. A downstream review bot
is not a pre-upload secret boundary. Optional summary availability is reported
separately from body capture and publication readiness.
