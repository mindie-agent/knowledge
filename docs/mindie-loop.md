# MindIE domain loop, first implementation

`mindie-knowledge` is the new single-domain runtime entrypoint. Its configuration
requires `root`, `domain`, and an external `agent_command` argv list. The command
receives JSON on stdin and returns schema-constrained JSON on stdout. The runtime
does not depend on a particular Harness; the Codex adapter supplies the runner.

## Boundaries

- Separate directory, SQLite ledger, Markdown content, process and reference
  namespace for each domain. A snapshot/ref for another domain is rejected.
- SQLite is the serving catalogue and event ledger. Markdown and metadata are
  readable exports, not independently editable authoritative inputs. Use `import`
  to ingest reviewed entries. Content identity derives from normalized content and
  source/applicability; provenance and feedback do not change it.
- Knowledge requires a source URL, revision and applicability conditions.
  Experience is advisory material. A query can exclude incompatible knowledge;
  it does not gate experiences on versions or assign factual confidence.
- Captures, uses and feedback are separate records. One use per entry and consumer
  task; producer self-use cannot count. The judge uses a fresh invocation and a
  distinct identity. This is logical task isolation, not adversarial identity proof.

## Core protocol

The stdio MCP exposes only `knowledge_query`, `knowledge_explain`, and
`knowledge_use`. The query supplies a native task ID and selects the configured
domain; unrelated tasks are not collected. The Stop bridge calls a bounded private
RPC against an already-running local service. It does not start a server, parse
transcripts, access reasoning, or persist failed/offline captures for later retry.

An accepted capture binds the first following final reply to pending use records
in that task. A bounded in-memory queue triggers organization and independent
judging. Previously queued captures are discarded after a service restart.
Failed evaluations are recorded, contribute no vote, and are not automatically
retried. Human corrections to completed use records are not implemented yet.

Organizer output is zero to three experience entries. It receives the current
summary and a small set of related entries to avoid redundant material. Exact
content duplicates share identity. The judge receives the experience, application,
observed evidence and final outcome. It returns `helpful`, `unhelpful`, or `unknown`
plus a reason. These are usefulness signals, not fact checking or reproduction.

## Distribution

`publish --ref` explicitly marks a reviewed sanitized entry for distribution.
`auto_publish: true` is an explicit operator option for organized entries. Existing
public-copy redaction rejects material needing further sanitization. Snapshots
contain only published entries and minimal feedback identities/verdicts; they omit
raw captures, use evidence and judge prose. Their digest covers the entire payload.

`upstream` explicitly connects a replica to a trusted domain authority. Sync pulls
the snapshot, then submits completed local uses to the authority's independent
judge. Explicitly published replica entries are offered to the authority first,
rechecked by the existing publication redactor, and then distributed. Unpublished
replica content remains local. Subsequent syncs bring updated feedback to replicas.
The authority's distributed verdict supersedes a prior local verdict for the same
use; the use still counts once. HTTP is permitted only
on loopback; remote services require HTTPS. Redirects are rejected. Service credentials
belong in private configuration; production multi-user identity/authorization,
durable publisher deployment and public release channels are outside this first slice.

## Official domain feed

Install the independent, model-free reader with `pip install ./tools/knowledge-intake`.
An explicit `feeds` configuration reads a trusted publisher's committed exports:

```json
{"feeds": [{"repository": "vllm-ascend-workspace/vaws-knowledge",
  "ref": "knowledge/vllm-ascend", "domain": "vllm-ascend",
  "interval_seconds": 300}]}
```

The GitHub reader uses existing `gh` authentication when available, with public
HTTPS reads otherwise. It resolves one immutable commit and verifies the export
manifest, every body and sidecar before changing search. `topics/` become knowledge
and must have explicit applicability; `cases/` become advisory experiences;
maintenance diaries are excluded. Feed content is reference data, never executable
instructions. Source references retain the publisher commit and original content hash.

Background checks have a bounded 120-second read budget and run at the configured
interval (minimum 60 seconds). They are polling, not GitHub event subscriptions.
An unchanged commit downloads no content; unchanged blobs are hash-checked and
reused. Changed or removed documents disappear from search in one SQLite transaction.
Old content remains available to existing references and uses. Unchanged experiences
keep their identities and feedback. A failed import keeps the prior searchable
generation and reports the failure in `status`. This feed does not send local
captures or votes to GitHub. Experience feedback distribution uses `upstream`.

## Initial ranking policy

Existing BM25 lexical retrieval supplies relevance. Experience relevance is
multiplied by `exp(0.35 * (helpful - unhelpful))`, bounded to `[0.1, 3]`.
Unknown is neutral. At least three negative uses and a negative surplus of three
withdraw the experience from search; content remains inspectable. This is an
explicit initial heuristic, not an effectiveness claim. Domain size is capped at
10,000 entries until a measured larger index or a useful domain split is selected.

## Commands

```sh
mindie-knowledge start --config domain.json
mindie-knowledge status --config domain.json
mindie-knowledge import --config domain.json --file reviewed-entry.json
mindie-knowledge publish --config domain.json --ref mindie://vllm-ascend/CONTENT_ID
mindie-knowledge sync --config domain.json
mindie-knowledge mcp --config domain.json
```

`serve` runs in foreground for diagnostics. Configuration and service versions
must be kept together; restart the owned service after updating runtime configuration.
The adapter repository includes opt-in live Codex acceptance; deterministic unit
tests here use a fixture runner and do not establish model judgment quality.
