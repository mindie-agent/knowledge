# Explicit historical transcript import

`mindie_knowledge.loop.history_import.import_transcript` is called by an adapter's
explicit user-requested history entry. It is not used by the service scheduler,
Stop capture, startup, synchronization, or the existing read-only `history.plan`
helper. The adapter owns selection and native metadata; the core owns admission,
redaction, storage, duplicate detection and ordinary contribution grants.

The caller supplies an already active requesting session/token, an explicit file,
its native source session/project scope, and the parser's initial FileIdentity.
Source sessions do not receive leases. Current and source project scopes must be
covered by enabled, saved contribution settings. Revalidate the current lease,
sharing generation and both scopes before each parser page and the final write.
The adapter must also check current admission/consent before reading source
metadata. Historical reads deliberately omit the ordinary activation timestamp
filter, and freeze `scan_until` to the selected snapshot size.

Public messages are redacted together with the installed scanner and shared r3
privacy rules, including complete or unterminated private-key blocks. One file
becomes one experience entry with an initial excerpt header; no model writes its body.
The same transaction schedules one required
title/summary task for that body. It uses the importing session's saved authority
and reads only the saved redacted body, never reopening the source. Revocation
prevents the call or discards its result. Repeating identical content does not
schedule another attempt. Each import receipt reports `summary.status` separately
from its local-save result. Missing configuration, a missing job or a failed
summary is incomplete work; failure keeps the local body, but does not release it
for a new export batch. A source excerpt is not a completed contribution.
Pages neither clip messages nor become a second raw history store. Source format
failure, incomplete EOF, replacement, scanner failure or permission revocation
before commit leaves that file unimported. Counts expose corrupt skipped records.
The existing file envelope still applies, and memory is proportional to one public
projection; this is not a streaming constant-memory importer.

`history_imports` retains only the last content length/hash, entry id/revision and
generation per source key. A repeated public projection is unchanged; an explicit
later call can append its new suffix. Body, receipt and grant commit in one Store
transaction. Prefix edits or a changed sharing generation refuse an overwrite.
Compacted submitted entries use the existing authoritative restore path before
extension. Ordinary outbox submission owns all publication/reconciliation; import
does not claim that a local save means a PR has been submitted or merged.
