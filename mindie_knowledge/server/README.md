# Knowledge retrieval service

Status: current

The service reads ordinary Markdown and saves local notes. It provides optional
reference material; it does not decide whether a claim applies to the current
environment. Known conditions and evidence stay visible for the Agent to judge.
See [the package README](../../README.md) for the common usage contract.

## Agent tools

| Tool | Inputs | Result |
|---|---|---|
| `knowledge_query` | `text`; optional `limit` (default 8) | Relevant excerpts and document references |
| `knowledge_explain` | `ref` from a query | Original Markdown and recorded context |
| `knowledge_capture` | `title`, `content` | Local saved note and indexing state |

A title and non-empty body are enough. No author schema, runtime coordinate,
status choice, fixed sections or extra summary is required. Old v2 arguments
such as `entry`, `reader_coordinate`, `statuses` and `include_unverified`
are not MCP inputs. Retrieval returns local and shared notes by relevance,
without trust ranking, status filtering or applicability verdicts.

Responses identify unavailable storage or indexes separately from an empty
result. Neither is evidence that a claim is absent, supported or safe to ignore.
The Agent can continue independent work. MCP capture saves the Markdown without
waiting for an index or retrieval startup; the MCP worker reconciles changes later.
Queries read the ready index and report pending maintenance when necessary.
The first valid MCP query or successful MCP capture activates background
maintenance. Initialize, tools-list, ping, invalid calls, explain and unused EOF
do not create a worker or prepare an index. Native summary capture remains local
and does not activate maintenance. The saved check/audit deadlines survive
reconnection; expired audits and explicit prepare still repair lost vectors.
Configured public sharing uses a separate redacted copy and follows the existing
authorization. Summary hooks can save locally while public sharing is disabled.

## Storage and setup

These locations are package configuration, not an author-managed lifecycle:

| Location | Content | Write path |
|---|---|---|
| `shared` | Packaged or downloaded public notes | Read-only; shared Release updates |
| `project` | A project's Markdown files | Ordinary project file editing |
| `candidate` | Local captured notes | Capture or ordinary local file editing |

Shared updates preserve the project and candidate locations. There is no
promotion requirement from one location to another. Missing project configuration
does not prevent using local notes.
Preparation indexes bundled or mounted shared Markdown; no release build is
needed first. Once a prebuilt shared pack is active, its stored vectors are reused
and `knowledge_explain(ref)` reads its original Markdown. Both paths are internal
to the package. Bundled notes and the current pack use separate namespaces and
remain available together; obsolete pack versions are excluded from retrieval.

For a standalone configured project:

```sh
mindie-knowledge prepare --project /path/to/project
```

This creates `.mindie-local/knowledge/service.json`, downloads the CPU model as
needed, prepares the available sources, and verifies index readiness. It returns
JSON with `ready` and status, with exit code 0 for ready or 1 for pending. Offline
or partial preparation remains explicit; the MCP worker retries quietly.
Existing mounts and public contribution settings are preserved. The workspace's
dependency installation invokes this entry internally.

Storage can also be configured directly:

```json
{
  "layers": {
    "project": { "roots": ["../example-project/.agents/knowledge"] },
    "candidate": { "root": "~/.local/state/mindie-knowledge/candidate" }
  }
}
```

Relative paths resolve against the configuration file's directory.
`MINDIE_KNOWLEDGE_CONFIG` selects that file; `MINDIE_KNOWLEDGE_STATE` selects
runtime state. Existing workspace setup can supply both without per-task inputs.
Public contribution and Release setup are described in
[publishing](../../docs/publishing.md).

```sh
mindie-knowledge server --config path/to/mindie-knowledge.json --describe
mindie-knowledge server --config path/to/mindie-knowledge.json
```

The installed package owns the local OpenViking instance and CPU embedding.
This service performs no NPU execution. Index reconciliation and configured
submission/sync run inside the package; callers do not sequence these operations
after each capture.
Every ten seconds the worker checks local changes; hourly integrity passes
compare stored content and actual index records. Shared sync verifies the active
pack even when its release version has not changed. Missing derived content is
restored from Markdown or a verified pack. One process lock shares maintenance
across MCP connections; failed work retries without changing source notes.

## Implementation reference

`layers.py` resolves configured storage; `query.py` retrieves Markdown;
`capture.py` saves local notes; `mcp_server.py` exposes the three tools.
The package version identifies the interface.

MCP uses newline-delimited UTF-8 JSON-RPC on stdio. Only protocol messages go to
stdout. Tool errors and unavailable sources remain visible to the caller without
turning knowledge into an execution gate.
