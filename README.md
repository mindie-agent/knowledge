# MindIE Knowledge

Local domain knowledge, experience collection, distribution and independent usefulness feedback for MindIE Agent.

Use `mindie-knowledge` from the `mindie-knowledge` Python package (Python 3.11+).
The module namespace is `mindie_knowledge`. There are no VAWS package or command aliases.

```sh
python -m pip install -e . -e tools/knowledge-intake
mindie-knowledge start --config domain.json
mindie-knowledge status --config domain.json
mindie-knowledge mcp --config domain.json
```

See [the runtime contract](docs/mindie-loop.md) for configuration, import, explicit publication,
feed synchronization, independent judging and ranking. A Harness provides the model runner;
the [Codex plugin](https://github.com/mindie-agent/mindie-agent-codex) supplies the first adapter.

## Boundaries

- Each domain owns a separate store and service. Knowledge, experience, use and feedback are distinct records.
- The local MCP exposes only attach, query, explain and use. Discovery never starts the service; Stop collection and background organization/judging are separate.
- A Stop summarizes the current task. Public distribution includes only explicitly published sanitized material.
- Feedback measures usefulness in an actual use, not factual truth or confidence.
- Codex, the knowledge service and judges run locally. Remote NPU execution belongs to remote-dev.

The official vLLM-Ascend feed currently lives in this repository's `knowledge/vllm-ascend` branch.
Content consolidation into `mindie-agent/knowledge-vllm-ascend` is a separate remaining task.

## Development

```sh
python -m pip install -e '.[test]' -e tools/knowledge-intake
python -m pytest -q tests
python -m mindie_knowledge.corpus_check --repo .
```

Internal Markdown, redaction, retrieval and distribution libraries are reused.
Older backend design documents record their own scope; the supported product service and CLI are the domain loop.
Historical corpus provenance is preserved and does not define supported installation paths.
