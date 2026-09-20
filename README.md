# MindIE Knowledge

Design inherits all nine [VAWS / MindIE Agent principles](https://github.com/mindie-agent/mindie-agent/blob/main/docs/design-principles.md). Retiring the old runtime does not retire those principles.

Local domain knowledge and experience loop for MindIE Agent: bounded capture from admitted tasks, optional community sharing through Git, and read-only retrieval from the canonical Git publication.

Use `mindie-knowledge` from the `mindie-knowledge` Python package (Python 3.11+). The module namespace is `mindie_knowledge`.

```sh
python -m pip install -e .
mindie-knowledge serve --config domain.json
mindie-knowledge status --config domain.json
mindie-knowledge sync --config domain.json
```

See [the runtime contract](docs/mindie-loop.md) for configuration, the capture
gate, bounded transcript increments, drafts and revisions, optional feedback,
contribution batches and feed sync. A Harness provides the model runner; the
[Codex plugin](https://github.com/mindie-agent/mindie-agent-codex) and independent [Kimi plugin](https://github.com/mindie-agent/mindie-agent-kimi) own native identity, record parsing and MCP dispatch. Kimi lifecycle acceptance is still in progress.

## Boundaries

- Community contribution is a single explicit switch (`mindie-community-config/1`). Off means no capture, extraction or sanitization at all — not local-only capture. Retrieval, updates and sync keep working.
- Entries have stable opaque IDs and internally computed content revisions. Upstream deletion withdraws an entry; cached pinned reads explicitly identify historical material. Local drafts update by append-only observations and never restore withdrawn content.
- Harness adapters expose `knowledge_query`, `knowledge_explain` and optional `knowledge_feedback`; each adapter verifies native task identity and the core rechecks its shared admission grant. Feedback is fully optional up/down with an optional one-line reason; there is no judge and no voting weight.
- Raw task records stay with the user's Harness. Only redaction-scanned canonical entry Markdown and vote files ever reach the contribution staging directory.

## Development

```sh
python -m pip install -e '.[test]'
python -m pytest -q tests
```

The public knowledge base starts empty in the new `mindie-entry/2` format; there is no legacy migration or compatibility layer. Pre-existing private user files stay inert.
