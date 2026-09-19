# Independent maintenance

Use this reference only from an explicit maintenance or publication task.
Ordinary query, explain, use, and Stop capture do not load it.

## Live commands (verify with `--help`)

Domain loop (`python -m mindie_knowledge` / `mindie-knowledge`):

| Command | Role |
| --- | --- |
| `attach --config PATH --session-id ID` | Explicit domain bind for this native task; does not require a query |
| `status --config PATH` | Ledger, captures, uses, feedback, failed judges |
| `import --config PATH --file ENTRY.json` | Reviewed Store document (`kind`, `title`, `content`, `source`, `conditions`) |
| `publish --config PATH --ref mindie://DOMAIN/ID` | Explicit sanitized publication |
| `withdraw --config PATH --ref mindie://DOMAIN/ID` | Explicit unpublish; not migration undo |
| `snapshot --config PATH` | Published subset only |
| `sync --config PATH` | Trusted feed / upstream pull; not a private upload |
| `start` / `stop` / `serve` / `mcp` / `hook` | Service lifecycle; MCP is query/explain/use only |
| `maintenance-resume --config PATH` | Resume a paused background budget; does not replay failed work |

Content migration (`python -m mindie_knowledge.content_migration`):

| Command | Role |
| --- | --- |
| `plan --source-root --store-root --state-dir --domain` | Inventory and map; does not write the Store |
| `apply --state-dir --job` | Dry-run (default): re-check source hashes and Store baseline |
| `apply --state-dir --job --commit` | Write mappable entries; skip duplicates; keep conflicts |
| `status --state-dir --job` | Plan / apply / undo receipt |
| `undo --state-dir --job` | Reverse unchanged job writes; retains published/used/feedback/feed/attribution changes |

Optional mapping flags (see `--help`): `--origin-repository`, `--source-revision`,
`--repo-root`, `--include-candidates`, `--experience-fallback`. Candidates and
private sidecars are not published. `--experience-fallback` is opt-in because
knowledge missing `source.url` / `source.revision` / conditions is otherwise
reported unmappable rather than silently stored as experience.

There is no live `python -m mindie_knowledge health`, `code-map`, `relations`,
`curation`, `curation-export`, or `contribution` path: the package module entry
is the domain loop. Do not tell a task agent to call them.

## What maps, what does not

Preserved when present: title, body, `source.url`, `source.revision`,
`source.sha256`, applicability conditions, feed path, content hash, and a new
stable `mindie://<domain>/<content_id>` reference.

Reported unmapped (not invented as Store fields): retrieval aliases, old
`viking://` URIs, layer/promotion, sidecar `status` / `captured_at` / free-form
`evidence` objects, maintenance diaries, operational Skill files, credential-like
keys, and private/candidate notes (unless `--include-candidates`, still unpublished).

Conflicts (same title, different conditions or bodies) stay as separate identities.
Exact Store identity matches are retained, not overwritten. Source bytes that
change after `plan` block `apply`. `plan` and dry-run `apply` open an existing
ledger read-only (no mkdir, chmod, WAL, or schema upgrade). Each `--commit`
write is reserved on disk first; an interrupted job is `apply_partial` and can
resume without deleting concurrent Store writes. Undo compares the apply-time
ownership fingerprint and refuses publication, use, feedback, feed membership,
or producer/source changes.

## Independent research handoff

Select a bounded set of original notes the user named. Do not scrape other
tasks or transcripts. Fix revisions on public sources; a title match is not
execution evidence. Write ordinary Markdown (title + body) with conditions and
uncertainty. Then `plan` / dry-run `apply` before `--commit`.

Images: use the agent's native vision against the file the user provided. A
diagram or synthetic fixture is not measured NPU performance. The separately
installable intake tool is optional and is not part of MCP query.

Public contribution remains a human-reviewed grant. Configured `auto_publish`
on the service is an operator choice for organized experiences; it is not a
reason to upload existing private candidates.

## Background organization versus this skill

Stop may queue at most one organization attempt for the current task. The
organizer returns zero to three experiences. The judge returns
`helpful` / `unhelpful` / `unknown` from actual use evidence, not from task
success or a database score. Unknown is neutral. This skill does not start
those jobs and does not wait for them.
