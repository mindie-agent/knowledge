<p align="center">
  <img src="https://raw.githubusercontent.com/mindie-agent/mindie-agent/main/assets/brand/mindie-agent-logo.png" alt="MindIE Agent logo" width="128" height="128">
</p>

# MindIE Knowledge

Design inherits all nine [VAWS / MindIE Agent principles](https://github.com/mindie-agent/mindie-agent/blob/main/docs/design-principles.md). Retiring the old runtime does not retire those principles.

Local domain knowledge and experience loop for MindIE Agent: bounded capture from admitted tasks, optional community sharing through Git, and read-only retrieval from the canonical Git publication.

Use `mindie-knowledge` from the `mindie-knowledge` Python package (Python 3.11+).
The interpreter's SQLite must be 3.43.0 or newer with FTS5 and
`contentless_delete` support. The module namespace is `mindie_knowledge`.

```sh
python -m pip install -e .
mindie-knowledge serve --config domain.json
mindie-knowledge status --config domain.json
mindie-knowledge sync --config domain.json
```

See [the runtime contract](docs/mindie-loop.md) for configuration, the capture
gate, bounded transcript increments, drafts and revisions, optional feedback,
automatic contribution delivery and feed sync. A Harness provides the model
runner; the [Codex plugin](https://github.com/mindie-agent/mindie-agent-codex),
[Kimi plugin](https://github.com/mindie-agent/mindie-agent-kimi) and
[Claude Code plugin](https://github.com/mindie-agent/mindie-agent-cc) own native
identity, record parsing and MCP dispatch. Component checks and native
end-to-end acceptance are reported separately in each adapter repository.

## Boundaries

- The adapters ask for a contribution choice only once after installation and persist it in the shared profile. New tasks and forks bind internally without asking again; ordinary failures do not revoke the choice.
- Community contribution is a single explicit switch (`mindie-community-config/1`). Off means no capture, extraction or sanitization at all — not local-only capture. Retrieval, updates and sync keep working.
- Entries have stable opaque IDs and internally computed content revisions. Upstream deletion withdraws an entry; cached pinned reads explicitly identify historical material. Local drafts update by append-only observations and never restore withdrawn content.
- Harness adapters expose `knowledge_query`, `knowledge_explain` and optional `knowledge_feedback`; each adapter verifies native task identity and the core rechecks its shared admission grant. Feedback is fully optional up/down with an optional one-line reason; there is no judge and no voting weight.
- Raw task records stay with the user's Harness. Only redaction-scanned canonical entry Markdown and vote files ever reach the contribution staging directory.
- Temporary publication or sync failures recover through the existing background worker with persisted backoff. Packaging and submission do not call a model or rerun organization. Deadline-interrupted organization has its own bounded recovery of the exact recorded region. Confirmed submissions retain minimal receipts; the current remote PR or upstream main provides the published body.
- The core uses the shared `mindie-diagnostics` component directly for bounded local failure logs and incident references; automatic fault reporting has its own explicit consent, separate from community contribution.

See [framework stability and verification](docs/framework-stability.md) for the
current behavior, reproducible checks and acceptance boundaries.

## Development

```sh
python -m pip install -e '.[test]'
python -m pytest -q tests
```

Run that from the repository root. If `MINDIE_FRAMEWORK_SOURCE` is unset, the test `conftest` uses the committed `tests/fixtures/production-parsers` and checks the three parser files exist. An explicit path that is missing or incomplete exits pytest with a setup error and does not search a production install. `MINDIE_PARSER_KIMI`, `MINDIE_PARSER_CC`, and `MINDIE_PARSER_CODEX` still override one file.

The public knowledge base starts empty in the new `mindie-entry/2` format; there is no legacy migration or compatibility layer. Pre-existing private user files stay inert.
