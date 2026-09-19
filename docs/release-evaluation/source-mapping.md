# Historical content → current Store

Inventory is limited to an explicit `--source-root` plus packaged public corpus
copied into isolated test data. User task directories, `~`, `.mindie-local`, and
official live feeds are not scanned. Private summaries are not published.

## Current Store contract (this worktree)

`Store.add(kind, title, content, source, conditions, producers)`

- Identity: SHA-256 of `[kind, whitespace-normalized title, whitespace-normalized content, source, conditions]`.
- Knowledge requires `source.url`, `source.revision`, nonempty `conditions`.
- Experience does not.
- Stable reference after import: `mindie://<domain>/<content_id>`.
- `publish` is a separate operator action; migration never calls it.

Live CLI: `python -m mindie_knowledge import|publish|snapshot|sync|status`.
Live MCP: `knowledge_query` / `knowledge_explain` / `knowledge_use`.

## What is preserved

| Source shape | Maps to | Preserved | Notes |
| --- | --- | --- | --- |
| Markdown + `.meta.json` sidecar | knowledge or experience per `kind` / path | title, body, source.url/revision/sha256, conditions | Sidecar conditions win on key clash; body kept verbatim |
| Packaged `corpus/references/*.md` | knowledge when URL/revision/conditions exist or can be constructed | body, `## Conditions`, `## Source` | Theoretical peaks stay theoretical |
| Feed export `topics/*.md` + conditions sidecar | knowledge | path, conditions, constructed blob URL if `--origin-repository` | Same rule as `loop.feed` |
| Feed export `cases/*.md` | experience | title, body | Advisory |
| Reviewed `*.entry.json` | as declared | Store fields | Must still satisfy knowledge constraints |
| Public GitHub blob links in body | source.url/revision | revision from the blob path | |

`Source.version` is recorded as `source.revision` when revision is absent.
When a Conditions section is missing, provider/kind/version/date from Source and
a `Topics:` line may fill applicability so a specification note is not silently dropped.

## Reported unmapped (not invented)

- Retrieval aliases / `retrieval.aliases`
- Old `viking://` URIs (kept as `legacy_ref` in the plan)
- layer / promotion (`shared` / `project` / `candidate`)
- sidecar `status`, `captured_at`, free-form `evidence` objects
- Operational Skill files (`SKILL.md`, `openai.yaml`, `independent-maintenance.md`)
- Maintenance diaries (`maintenance/`)
- Credential-like keys; `private: true`; candidate layer (unless `--include-candidates`, still unpublished)
- Knowledge lacking url+revision+conditions, unless `--experience-fallback`

## Conflicts, duplicates, failures

- Exact Store identity: `duplicate_retain` (no second write).
- Same title, different conditions or bodies: two identities, both kept.
- Sidecar vs body condition values: `conflict_retain`; sidecar keys stored, body unchanged.
- Source/sidecar bytes changed since plan: apply refused; originals untouched.
- Mid-apply interruption: per-item `progress.json` reservations; job is `apply_partial` and can resume. Concurrent Store writes are not deleted.
- Undo: removes only entries whose apply-time ownership fingerprint still matches (document/producers, publication, uses, feedback, feed membership). Never deletes source files.

## Official feed

`origin/knowledge/vllm-ascend` (`4a79da03…` on the remote) was **not** present as
local git objects. G1 did not fetch or modify it. Local feed-shaped fixtures
cover the `topics/` / `cases/` / `maintenance/` rules.

## VAWS / private

Migration does not restore VAWS install or session channels, does not upload
private candidates, and does not treat feed/Skill text as executable authority.
