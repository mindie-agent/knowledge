# community-bot example: self-hosted review automation

This is a *recipe*, not a deployed bot. The maintainer owns every credential
and the Grok CLI installation; nothing here writes to the user's global Grok
config, and shipping these files does not establish live event delivery.

## Layout

- `community-bot.yml` — GitHub Actions workflow (trigger shape only)
- `bot-settings.example.json` — community config for the bot runner

The model bridge is the packaged adapter `mindie_knowledge.community.grok_adapter`
(see `docs/community-sharing.md`); the example shell wrapper is gone because
the real Grok CLI speaks `--prompt-file`/`--output-format json`/`--json-schema`,
not invented flags.

## Credential and environment ownership

| Secret | Held by | Reaches the code via |
| --- | --- | --- |
| Content-repo token (PR read/merge, checks read) | GitHub Actions secret `MINDIE_BOT_TOKEN` | env `GH_TOKEN` (never in files) |
| Plugin-repo token (Skill PRs) | Actions secret `MINDIE_PLUGIN_TOKEN` | env `MINDIE_PLUGIN_TOKEN` |
| Grok CLI + its own credentials | self-hosted runner image | `bot.grok_argv` / `bot.skill_grok_argv` pointing at the packaged adapter |

Least scope: contents:write (merge), pull-requests:write, checks:read on the
content repo; a *separate* token for the plugin repo. The bot account's own
login goes in `bot.account` so its own events are ignored (no self-recursion).

Exact adapter argv (verified against the installed Grok CLI 1.0.30 `--help`):

```json
"grok_argv": ["python", "-m", "mindie_knowledge.community.grok_adapter", "--kind", "review"],
"skill_grok_argv": ["python", "-m", "mindie_knowledge.community.grok_adapter", "--kind", "skill"]
```

The adapter runs one bounded single turn:
`grok --prompt-file <tmp> --output-format json --json-schema <tmp>
--max-turns 1 --no-subagents --disable-web-search --permission-mode plan
--deny Bash --deny Write --deny Edit --deny NotebookEdit`, extracts only the
structured result from the JSON envelope (hidden reasoning is never parsed),
and deletes its temporary prompt/schema files.

## Concurrency and triggers

- Triggers: `pull_request` (opened/reopened/synchronize) → `event`;
  a 5-minute `schedule` → `poll-once` as the safety net.
- `concurrency: mindie-community-bot` with `cancel-in-progress: false`:
  one runner at a time; event floods collapse into ledger dedup by
  `(repo, pr, head_sha)`.
- Model calls: exactly one per unique PR head, `review_timeout_seconds: 300`,
  output ≤ 128 KiB; the same failed head is never retried. Skill generation:
  one call per material digest.

## Local / self-hosted CLI operation

```bash
pip install -e .                      # package with mindie_knowledge.community
export GH_TOKEN=...                   # bot token for the content repo
python -m mindie_knowledge.community poll-once \
    --settings bot-settings.json --state-dir /var/lib/mindie-bot/state
python -m mindie_knowledge.community event \
    --settings bot-settings.json --state-dir /var/lib/mindie-bot/state \
    --file "$GITHUB_EVENT_PATH"       # one webhook delivery
python -m mindie_knowledge.community review \
    --settings bot-settings.json --state-dir /var/lib/mindie-bot/state --pr 123
python -m mindie_knowledge.community skill-scan \
    --settings bot-settings.json --state-dir /var/lib/mindie-bot/state --retirements
```

Each command prints one JSON receipt (`status`, `detail`, and for reviews
`repo`/`pr`/`head_sha`/`verdict`). `pending` always means a human or a later
legitimate trigger must act — it is never dressed up as success.

## Contributor-side commands (plugin host)

```bash
python -m mindie_knowledge.community submit \
    --settings community.json --state-dir ~/.mindie/community --batch batch.json
python -m mindie_knowledge.community reconcile \
    --settings community.json --state-dir ~/.mindie/community --batch-id <id>
```

The contributor config lives next to the plugin's own config; `config_path`
in the in-memory settings points back at it so every outbound write re-reads
the live enabled/generation/repository values.
