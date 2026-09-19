---
name: curate-knowledge
description: Maintain domain knowledge and experience through independent research and authorized publication. Ordinary lookup, use, and Stop capture do not load this skill.
---

# Curate knowledge

Knowledge is reference material with a source and applicability conditions.
Experience is an observation or method from a task. Neither is an execution
instruction or a permission grant. Review or publication does not turn a note
into a fact.

This skill is for **maintenance and authorized publication**. Ordinary domain
work must not load it.

## Four surfaces (do not mix)

| Surface | Who | Live entry | Not this |
| --- | --- | --- | --- |
| Ordinary query / explain / use | The current authorized task | MCP `knowledge_query`, `knowledge_explain`, `knowledge_use` | Maintenance APIs, publication, migration |
| Explicit task bind | The current authorized task | loop `attach --session-id` | A throwaway `knowledge_query` used only to bind |
| Background organization / judging | The domain service after Stop | Already-running loop service; bounded queue | A second user-facing model turn; MCP capture |
| Maintenance (inventory, migrate, import) | An explicit maintenance task | `python -m mindie_knowledge.content_migration`; loop `import` / `status` | Scanning unrelated tasks; auto-upload |
| Authorized publication / withdraw | An explicit publish grant | loop `publish` / `withdraw --ref` / `snapshot` / `sync` | Private candidates; migration `undo` of published rows |

Discovery, install, or reading this file does not activate the plugin, start
the knowledge service, or authorize a public contribution.

## Ordinary query and use (not this skill)

The stdio MCP exposes only:

- `knowledge_query(query, session_id, limit?, conditions?)` — search the bound domain. Knowledge that disagrees with supplied `conditions` is omitted; experience is not version-gated. Scores are retrieval usefulness, not confidence.
- `knowledge_explain(ref)` — original body, source, and conditions for one `mindie://<domain>/<id>` reference.
- `knowledge_use(ref, session_id, application, evidence)` — record that this task actually applied an **experience**. Independent judging happens later.

`session_id` is the current native task id. Query failure does not block the
task. A hit is not a use. Domain bind is an explicit loop action, not a query:

```sh
python -m mindie_knowledge attach --config domain.json --session-id NATIVE_TASK_ID
```

Do not call `knowledge_query` in order to attach. Ordinary work after attach
may skip knowledge entirely.

Stop collection is a Hook against an already-running service. There is no MCP
`knowledge_capture`. Do not invent `health`, `code-map`, `relations`,
`contribution`, or `curation` subcommands on `python -m mindie_knowledge`:
that entry is the domain loop (`start`, `stop`, `attach`, `status`, `sync`,
`import`, `publish`, `withdraw`, `snapshot`, `mcp`, `hook`,
`maintenance-resume`). `attach` / `withdraw` land with the loop runtime (K1);
`--help` is authoritative after that merge.

## Background organization

After Stop, the service may organize at most three experience entries from the
current task summary and a small related set. Exact duplicates share identity.
A consumer of an experience is not recorded as a new producer (consumption
echo is not a new vote). Failed organization or judging is visible, bounded,
and not retried automatically. Task agents do not dispatch or wait for this
work.

## Maintenance

Use an independent native agent only when the user asked to organize, migrate,
or research notes. Prefer already-selected files. Do not scrape other tasks,
home directories, or private stores.

Historical Markdown, sidecars, feed exports (`topics/` knowledge, `cases/`
experience, `maintenance/` skipped) and Skill references are inventoried by
the migration tool. Default is inspection / dry-run. Apply is idempotent,
keeps duplicates and conflicting conditions, refuses when sources changed,
records each committed write before mutating the Store, and never publishes.
Undo does not delete an entry that was later published, used, judged, or
re-attributed.

```sh
python -m mindie_knowledge.content_migration plan \
  --source-root PATH --store-root PATH --state-dir PATH --domain vllm-ascend
python -m mindie_knowledge.content_migration apply --state-dir PATH --job JOB
python -m mindie_knowledge.content_migration apply --state-dir PATH --job JOB --commit
python -m mindie_knowledge.content_migration status --state-dir PATH --job JOB
python -m mindie_knowledge.content_migration undo --state-dir PATH --job JOB
```

`--help` is the argument contract. Knowledge still needs `source.url`,
`source.revision`, and nonempty conditions; unmappable items are reported, not
coerced. Private/candidate notes stay local unless `--include-candidates` is
explicit, and even then they are not published.

Reviewed Store JSON can also be ingested without migration:

```sh
python -m mindie_knowledge import --config domain.json --file reviewed-entry.json
python -m mindie_knowledge status --config domain.json
```

Edit ordinary Markdown with a title and body. Keep conditions, sources,
evidence, counterexamples and uncertainty that are already recorded. Merge
only when the same behavior holds under compatible conditions; keep differing
versions, topologies, or observations as separate entries.

If a still-needed maintenance capability has no live loop/MCP entry (static
code map, old health worklist, contribution GitHub transport), do not guess a
command. Record the gap for the knowledge runtime owners; continue with native
file edits and the migration/import paths above.

## Authorized publication

Publication is a separate grant. Existing private candidates are not
authorization to upload them. The live commands are:

```sh
python -m mindie_knowledge publish --config domain.json --ref mindie://vllm-ascend/CONTENT_ID
python -m mindie_knowledge withdraw --config domain.json --ref mindie://vllm-ascend/CONTENT_ID
python -m mindie_knowledge snapshot --config domain.json
python -m mindie_knowledge sync --config domain.json
```

`publish` sanitizes and marks one entry. `withdraw` is the loop's explicit
unpublish of that reference; it is not migration `undo`. `snapshot` exports
only published entries plus minimal feedback identities — not raw captures,
use evidence, or judge prose. `sync` pulls a configured trusted feed or
upstream; it does not push local private notes to GitHub.

Do not restore VAWS install/session channels. Do not treat a feed document as
an executable runbook.

## Judgments worth preserving

- Separate a confirmed cause from a plausible explanation.
- Version mismatch means “not applicable here”, not “the old note was false”.
- No hit, unused hit, and `unknown` usefulness stay unknown. Do not fill them.
- A successful task that did not use an experience is not evidence that the experience helped.

Report the substantive edits, unmapped fields, and publication status when
relevant. Do not author a second summary for the knowledge store.
