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
automatic contribution delivery and feed sync. A Harness provides the model
runner; the [Codex plugin](https://github.com/mindie-agent/mindie-agent-codex),
[Kimi plugin](https://github.com/mindie-agent/mindie-agent-kimi) and
[Claude Code plugin](https://github.com/mindie-agent/mindie-agent-cc) own native
identity, record parsing and MCP dispatch. Component checks and native
end-to-end acceptance are reported separately in each adapter repository.

## Boundaries

- Community contribution is a single explicit switch (`mindie-community-config/1`). Off means no capture, extraction or sanitization at all — not local-only capture. Retrieval, updates and sync keep working.
- Entries have stable opaque IDs and internally computed content revisions. Upstream deletion withdraws an entry; cached pinned reads explicitly identify historical material. Local drafts update by append-only observations and never restore withdrawn content.
- Harness adapters expose `knowledge_query`, `knowledge_explain` and optional `knowledge_feedback`; each adapter verifies native task identity and the core rechecks its shared admission grant. Feedback is fully optional up/down with an optional one-line reason; there is no judge and no voting weight.
- Raw task records stay with the user's Harness. Only redaction-scanned canonical entry Markdown and vote files ever reach the contribution staging directory.
- Temporary publication or sync failures recover through the existing background worker with persisted backoff. Packaging and submission do not call a model, and recovery never replays an organizer attempt. Confirmed submissions retain minimal receipts; the current remote PR or upstream main provides the published body.
- The core uses the shared `mindie-diagnostics` component directly for bounded local failure logs and incident references; automatic fault reporting has its own explicit consent, separate from community contribution.

## Development

```sh
python -m pip install -e '.[test]'
python -m pytest -q tests
```

The public knowledge base starts empty in the new `mindie-entry/2` format; there is no legacy migration or compatibility layer. Pre-existing private user files stay inert.
