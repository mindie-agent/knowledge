# MindIE Knowledge

Part of [MindIE Agent](https://github.com/mindie-agent/mindie-agent).

Design inherits all nine [VAWS / MindIE Agent principles](https://github.com/mindie-agent/mindie-agent/blob/main/docs/design-principles.md). Retiring the old runtime does not retire those principles.

Local domain knowledge and experience loop for MindIE Agent: bounded capture from admitted tasks, optional community sharing through Git, and read-only retrieval from the canonical Git publication.

Use `mindie-knowledge` from the `mindie-knowledge` Python package (Python 3.11+).
The module namespace is `mindie_knowledge`. The runtime database holds queue,
receipt and header metadata; authoritative current material bodies are ordinary
Markdown files. A replaceable retrieval checkpoint stores current block chunks
and term counts.
Pinned ReMe components provide local file chunking, the file graph and BM25
retrieval. No ReMe service, daemon, watcher, model, embedding or external
account configuration is started or required for retrieval.

```sh
python -m pip install -e .
mindie-knowledge serve --config domain.json
mindie-knowledge status --config domain.json
mindie-knowledge sync --config domain.json
```

See [the runtime contract](docs/mindie-loop.md) for configuration, the capture
gate, bounded transcript increments, drafts and revisions, optional feedback,
automatic contribution delivery and feed sync. Capture requires an explicit
transcript parser and absolute Gitleaks executable path. A Harness supplies the required `summary_command` for incremental block
indexes; missing configuration preserves local material and blocks publication.
The [Codex plugin](https://github.com/mindie-agent/mindie-agent-codex) provides
the current summary-worker protocol. Kimi and Claude Code parsers have
compatibility tests; their summary workers have not been migrated to this
protocol. Parser compatibility does not establish complete Harness support.
Component checks and native end-to-end acceptance remain separate evidence.

Every public feed declares `publication-contract.json`: domain, exact task/block
schemas, immutable validator and Bot review contract. Publication reads it at
the upstream base before recording a new send intent; feed sync checks it before
promoting data. Product-managed feeds and community settings also bind its exact
SHA256. A mismatch reports `contract_mismatch`, preserves current data and frozen
work, and recovers when a matching deployment is available. Code is never loaded
from a content PR. The [Bot contract](docs/repository-bot-contract.md) separates
ordinary task/feedback contributions from contract, workflow and policy changes.

## Boundaries

- The adapters ask for a contribution choice only once after installation and persist it in the shared profile. New tasks and forks bind internally without asking again; ordinary failures do not revoke the choice.
- Community contribution is a single explicit switch (`mindie-community-config/1`). Off means no capture, extraction or sanitization at all — not local-only capture. Retrieval, updates and sync keep working.
- Entries have stable opaque IDs and internally computed content revisions. Drafts and the subscribed feed retain current packages only. A block reference remains readable across appends or navigation changes while its exact bytes remain a current member; replaced blocks and withdrawn tasks are unavailable. Pending contributions freeze their send files separately; the outbox holds only descriptors. Upstream deletion removes the published body and leaves a withdrawal marker. Local drafts update by append-only observations and never restore withdrawn content. The private feed Git cache retains one shallow tip, without old revision objects.
- Harness adapters expose `knowledge_query`, `knowledge_explain` and optional `knowledge_feedback`; each adapter verifies native task identity. Public reads require that identity but no capture grant. Capture and feedback publication still check their applicable shared authorization. Feedback is fully optional up/down with an optional one-line reason; there is no judge and no voting weight.
- Raw task records stay with the user's Harness. Complete mechanically redacted material, fallible block titles/summaries, current navigation and optional vote files reach contribution staging.
- Temporary publication or sync failures recover through the existing background worker with persisted backoff. Packaging, submission and consumer retrieval make no model calls. The index worker summarizes only new blocks plus prior navigation, retains returned output through local apply failures, and records failed or uncertain invocations without automatically repeating them. Confirmed submissions retain minimal receipts; the current remote PR or upstream main provides the published body.
- The core uses the shared `mindie-diagnostics` component directly for bounded local failure logs and incident references; automatic fault reporting has its own explicit consent, separate from community contribution.

See [framework stability and verification](docs/framework-stability.md) for the
current behavior, reproducible checks and acceptance boundaries.

## Development

```sh
python -m pip install -e '.[test]'
python -m pytest -q tests
```

Run that from the repository root. If `MINDIE_FRAMEWORK_SOURCE` is unset, the test `conftest` uses the committed `tests/fixtures/production-parsers` and checks the three parser files exist. An explicit path that is missing or incomplete exits pytest with a setup error and does not search a production install. `MINDIE_PARSER_KIMI`, `MINDIE_PARSER_CC`, and `MINDIE_PARSER_CODEX` still override one file.

Public material uses `tasks/<task_id>/` packages: a
`mindie-material-task/1` navigation manifest embeds a `mindie-entry/3` header
and binds ordered `mindie-material-block/1` files. All blocks must have indexes
before publication. Task status tracks indexing progress, not the underlying
business outcome. Failed and uncertain evidence remains reference material;
summaries and retrieval scores do not certify it. Explicitly selected historical
imports and live capture share the same material pipeline. The v4 runtime does
not automatically open prior development state formats. Pre-existing private
files stay inert until explicitly selected through the supported import path.

## Development and released state

Until the repositories meet the product release criteria and publish normal
release versions, breaking state changes are allowed repeatedly. There is no
one-reset limit. Internal package numbers and Git pins used for development
installation do not declare that coordinated release.

`mindie_knowledge.state_layout` declares the persistent `FORMAT` and the product
`RELEASE_VERSION`. It remains `None` during development; the release commit sets
a normal `major.minor.patch` version. State lives in `<root>/<domain>/state-v<FORMAT>`.
A development format change selects a fresh directory and leaves earlier data
inert. Configuration and old directories are retained; old receipts are not
imported into the new development format.

Once a release has successfully opened the state, compatible upgrades reuse
the same material, cursors, model attempts and publication receipts. Returning
to a development build does not remove that release boundary. A different
format must ship an explicit migration before it can consume released state;
the current implementation refuses it and preserves the old state. Missing or
damaged layout metadata is an error, never permission to reset. The Harness
updater uses the read-only compatibility check before retiring its old runtime.
Future migration code and native release acceptance are still separate work;
this boundary does not certify an unknown future representation.
