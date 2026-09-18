# Public contribution and shared updates

Status: current

Local installation and retrieval preparation do not enable public uploads.
`shared_sync.enabled` defaults to true and is independent of `publishing.enabled`.
For explicitly requested contribution setup, `vaws-knowledge publishing configure --config PATH --consent-file COMMUNITY_JSON` enables
sharing according to the user's authorization and configuration. It uses
`GH_TOKEN` / `GITHUB_TOKEN`, or reuses GitHub CLI authentication, creates
or reuses the user's corpus fork, and prepares a dedicated contribution clone.
Existing remotes in business repositories are not changed. Use `--read-only`
for a client that only consumes releases; downloads of the public corpus do not
require a login.

Workspace onboarding binds the service to its one local community decision:

```console
vaws-knowledge publishing configure --config SERVICE_JSON --consent-file COMMUNITY_JSON --github-user PERSONAL_USER
vaws-knowledge publishing configure --config SERVICE_JSON --consent-file COMMUNITY_JSON --read-only
```

`COMMUNITY_JSON` is owned by the workspace and uses schema `vaws.community.v1`,
`workspace_id` and `revision` as 32 lowercase hexadecimal characters, and
`decision: "enabled"` or `"disabled"`. New captures bind to the enabled revision.
The running service rereads the service configuration and community decision
before queuing, processing each submission, Git push and PR creation. A disabled,
missing or invalid decision stops subsequent automatic contributions immediately;
an already dispatched network request cannot be recalled. Local captures,
pending records and shared downloads remain available. Re-enabling participation
does not authorize pending records from an earlier revision, and never sweeps
private captures into the upload queue. The operator can explicitly submit a
retained public copy with the contribution CLI after reviewing it.

Automatic publishing always requires `consent_file`, including standalone
installations. A `publishing.enabled` flag by itself does not authorize uploads.
Historical pending records without a current consent revision remain local.
`publishing status` exposes effective upload eligibility for each retained record.

Token-only setup does not require installing `gh`. The confirmed personal user
must match the authenticated GitHub identity before creating a fork. HTTPS clone,
fetch and push receive credentials only through a scoped child-process Git
environment; credentials are never placed in remote URLs, command arguments or
Git config files. Existing global Git settings are not changed. API and Git
redirects cannot forward the token to another host. Token permissions and access
to the selected repositories still determine which GitHub operations succeed.

The default corpus is `mindie-agent/knowledge-vllm-ascend`. The service
config contains `state_root`, the three layer mounts, `shared_sync`, and `publishing` settings.
Set `VAWS_KNOWLEDGE_CONFIG` to that config for MCP and CLI consumers. The workspace
provides `.agents/scripts/knowledge_setup.py` to set this up with its own paths.

With public sharing enabled, a new capture saves a private Markdown candidate and
prepares a redacted public copy in local state. The MCP service retries pending
submissions in the background, pushes the content branch to the fork, then opens
or reuses a PR. Offline or authentication failure keeps the pending record.
Re-delivery of the same content reuses the record. Closed/merged PRs are recorded
and are not reopened automatically. Existing private candidates are not bulk
submitted when configuration is enabled.

PR checks validate Markdown and redaction. **Human reviewers merge knowledge
PRs.** This path needs no automatic reviewer or model credential. PR preparation,
checks and publication do not prove hardware claims. The package handles this
workflow; ordinary tasks do not need a fork, publication commands or review waits.

After a corpus merge, CI builds the exact Git commit into a dense OVPack and
manifest, uploads both to a draft Release, then publishes it. The release tag
identifies the source commit. A published release is never overwritten.

After a valid query or successful MCP capture activates maintenance, its worker
checks releases when the saved deadline is due and every 30 minutes;
failed checks retry after one minute. Submission polling is every 30 seconds.
Multiple clients share the same OS lock and state. Closing MCP ends its worker;
the next actual knowledge use resumes from durable state. An unused MCP
connection does not create the worker or start release checks. No OS timer or additional daemon is
installed. New packs are verified and imported before the shared pointer moves.
Failure preserves the previous shared version and all project/candidate content.
While maintenance is active and the backend is available, hourly integrity checks export and compare the active content and vectors against
the retained verified pack. A missing or damaged import is restored into a new
staging namespace and activated after verification, including when the release
version is unchanged. Public contribution errors do not make local retrieval
unready. Bundled Markdown remains available alongside the current public pack.

The package's local OpenViking instance uses `api_key` auth. Root and tenant keys
are private local files with restrictive permissions; only the tenant key is
used for content operations. Capture, retrieval, build and import share this
tenant contract. Status output carries no keys.

`vaws-knowledge publishing status --config PATH` reports the last check and PRs.
`vaws-knowledge publishing once --config PATH` performs one explicit recovery
or verification pass; normal capture does not need this command.

## Native-client summaries

All five workspace clients use the same knowledge MCP tools. Automatic capture
uses only a native event that supplies final response text. The observed support
as of 2026-09-12 is:

| Client | Event and final-text field | Native source |
|---|---|---|
| Codex | `Stop` → `last_assistant_message` | [OpenAI hooks](https://learn.chatgpt.com/docs/hooks) |
| Claude Code | `Stop` → `last_assistant_message` | [Claude hooks](https://code.claude.com/docs/en/hooks) |
| Cursor | `afterAgentResponse` → `text` | [Cursor hooks](https://cursor.com/docs/hooks) |
| Grok | `hookEventName: "stop"` → `lastAssistantMessage` | [Grok hooks](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/10-hooks.md) |
| Kimi Code | MCP and session support; no automatic summary capture | [Kimi hooks](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/hooks.html), [Stop implementation](https://github.com/MoonshotAI/kimi-code/blob/main/packages/agent-core-v2/src/features/externalHooks/agent/agentExternalHooksService.ts) |

Kimi Code 0.42.0 and the inspected upstream `Stop` implementation supply the
stop-hook flag without final response text. No summary hook is installed for
that event. A useful existing finding may still be captured through MCP; this
does not require an extra summary or a transcript scan.

Configured adapters accept only responses from their selected project. They
reuse final text without reading transcripts or thinking events; repeated
delivery of the same text reuses its local note. Grok's native marker prevents
its imported hooks from capturing the same event again. Empty or absent summaries
are a no-op, and optional capture errors do not interrupt the client. Lookup and
capture remain optional for every client.
Hook capture saves locally even when public publishing is disabled. Public
queuing follows the publishing setting; local persistence does not enable sharing.
Native hook trust remains managed by the client; configuration does not bypass it.

Validation depends on the tested revision and environment. Historical tests do
not establish results for another platform or a larger corpus.
