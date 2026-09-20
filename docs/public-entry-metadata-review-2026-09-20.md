# Public Markdown metadata review

Status: code-path audit and proposed simplification. The versions-only meaning
of `conditions` is implemented in the organizer instructions and documented
contract; the remaining serialization changes below are not deployed.

The public experience is detailed reference material. It should not expose the
local capture, authorization or maintenance ledger. A field being used by the
current implementation is not enough reason to publish it.

| Field | Observed use | Decision |
| --- | --- | --- |
| `title` | Search/display | Keep stable by default. Correct only a misleading title or a materially changed finding/scope; ordinary appended observations do not require automatic renaming. |
| `summary` | Short retrieval result | Keep, without generating a second summary artifact. |
| `conditions` | Returned context; optional conflicting-version filtering for reference knowledge | Only known software versions/source commits; omit when empty. Other details stay in the body. |
| `entry_id` | Stable lineage, pinned references, feedback and Skill source links | Keep as the fixed object for updates and feedback, including distinct experiences with the same title. Title edits are exceptional, not the reason to introduce a renaming workflow. |
| `revision` | Exact historical reads, feedback version, publication grants and content integrity | Keep the internal content fingerprint, compute it during parse/write, and remove the redundant public hash field. A Git commit alone does not identify a local unpublished revision. |
| `producers` | Organizer's local draft ownership checks and task-owned draft selection | Remove from public Markdown; store ownership privately. It is not used for retrieval ranking or vote counting, and an opaque public producer does not authenticate an independent human/agent. |
| `kind` | Distinguishes sourced reference knowledge from observed experience; affects source validation and condition filtering | Keep one explicit line. The directory is consistent with it, but a standalone entry retains its meaning. |
| `domain` | Rejects wrong-domain feed content and forms references | Keep one explicit line. It is inexpensive and preserves standalone/cross-feed validation. |
| `schema` | Rejects unsupported document formats | Keep one explicit format marker; avoid a second repository manifest solely to remove this line. |
| `sources` | Currently a mandatory public reference list for reference knowledge, optional for experience | Remove the separate public header. Keep generation-session provenance privately. Preserve relevant public documentation/code/issue links in the body; a task being the production source does not make external evidence unnecessary. Update the old structured-source publication requirement when changing the format. |
| `status` | Currently two withdrawal mechanisms: explicit retired state and absence from the authoritative feed tree | Remove from public Markdown. Withdraw by deleting the file from the public main tree; successful sync removes it from ordinary retrieval. Preserve enough local membership state to prevent stale drafts from restoring it. |
| `retirement_reason` | Currently explains an explicit retired entry | Remove from public Markdown. Record the withdrawal reason in its PR/commit, whose Git history retains the previous content. |

Typical header after the proposed serialization change:

```yaml
schema: mindie-entry/2
entry_id: <stable opaque identity>
domain: vllm-ascend
kind: experience
title: RMSNorm reference must use dtype-rounded inputs
summary: <short factual abstract and evidence boundary>
conditions:
  torch_version: 2.10.0+cpu
  torch_npu_version: 2.10.0.post2
```

## Dependencies that must change together

1. Move task ownership to a local entry-owner relation, used by
   `Store.append_observation` and `Store.draft_headers`. Never infer permission
   from a downloaded document's claimed producers. Preserve the existing
   sharing-generation grants and consumed attempt receipts.
2. Split public serialization from the normalized internal document. Expand
   omitted optional values deterministically and compute the content revision
   over the canonical public semantic fields, excluding local ownership.
   Keep exact historical references and feedback attached to their revisions.
3. Update contributor, feed parser, trusted publication validator and Bot
   contract together. Publish the new format only after the installed reader,
   CI validator and Bot validator support it. Do not silently serve an empty
   corpus when a client sees an unsupported format.
4. Verify a real create -> correction -> publication -> sync -> pinned read ->
   optional feedback -> upstream deletion -> sync withdrawal flow, including
   cross-task update refusal. Current `Store.install_feed` already excludes
   missing upstream entries and their stale drafts from normal search. An old
   cached pinned reference may remain readable for history, but its response
   must clearly identify it as withdrawn; this disclosure still needs work.
   Do not add an independent local authority to delete public entries or imply
   that an offline client can observe an upstream deletion before syncing.
   An existing model-extraction success does not prove this format transition.

This review adds no new scoring, promotion threshold, source service or model
invocation. It preserves detailed bodies and the user's loose optional votes.
