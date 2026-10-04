# MindIE Knowledge

Part of [MindIE Agent](https://github.com/mindie-agent/mindie-agent).

Design inherits all nine [VAWS / MindIE Agent principles](https://github.com/mindie-agent/mindie-agent/blob/main/docs/design-principles.md). Retiring the old runtime does not retire those principles.

Local domain knowledge and experience loop for MindIE Agent: bounded capture from admitted tasks, optional community sharing through Git, and read-only retrieval from the canonical Git publication.

Use `mindie-knowledge` from the `mindie-knowledge` Python package (Python 3.11+).
The module namespace is `mindie_knowledge`. SQLite holds only small queue and
receipt metadata; current material bodies are ordinary Markdown files.
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

## Boundaries

- The adapters ask for a contribution choice only once after installation and persist it in the shared profile. New tasks and forks bind internally without asking again; ordinary failures do not revoke the choice.
- Community contribution is a single explicit switch (`mindie-community-config/1`). Off means no capture, extraction or sanitization at all — not local-only capture. Retrieval, updates and sync keep working.
- Entries have stable opaque IDs and internally computed content revisions. Drafts and the subscribed feed retain current bodies only; superseded references expire. Pending contributions freeze their send files separately; the outbox holds only descriptors. Upstream deletion removes the published body and leaves a withdrawal marker. Local drafts update by append-only observations and never restore withdrawn content. The private feed Git cache retains one shallow tip, without old revision objects.
- Harness adapters expose `knowledge_query`, `knowledge_explain` and optional `knowledge_feedback`; each adapter verifies native task identity and the core rechecks its shared admission grant. Feedback is fully optional up/down with an optional one-line reason; there is no judge and no voting weight.
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
not automatically open or migrate prior state formats; pre-existing private
files stay inert until explicitly selected through the supported import path.
