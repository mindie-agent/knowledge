# Public Markdown metadata review

Status: code-path audit and proposed simplification. The versions-only meaning
of `conditions` is implemented in the organizer instructions and documented
contract; the remaining serialization changes below are not deployed.

The public experience is detailed reference material. It should not expose the
local capture, authorization or maintenance ledger. A field being used by the
current implementation is not enough reason to publish it.

| Field | Observed use | Decision |
| --- | --- | --- |
| `title` | Search/display | Keep. |
| `summary` | Short retrieval result | Keep, without generating a second summary artifact. |
| `conditions` | Returned context; optional conflicting-version filtering for reference knowledge | Only known software versions/source commits; omit when empty. Other details stay in the body. |
| `entry_id` | Stable lineage, pinned references, feedback and Skill source links | Keep. Titles/paths/content change; they cannot replace identity. |
| `revision` | Exact historical reads, feedback version, publication grants and content integrity | Keep the internal content fingerprint, compute it during parse/write, and remove the redundant public hash field. A Git commit alone does not identify a local unpublished revision. |
| `producers` | Organizer's local draft ownership checks and task-owned draft selection | Remove from public Markdown; store ownership privately. It is not used for retrieval ranking or vote counting, and an opaque public producer does not authenticate an independent human/agent. |
| `kind` | Distinguishes sourced reference knowledge from observed experience; affects source validation and condition filtering | Keep one explicit line. The directory is consistent with it, but a standalone entry retains its meaning. |
| `domain` | Rejects wrong-domain feed content and forms references | Keep one explicit line. It is inexpensive and preserves standalone/cross-feed validation. |
| `schema` | Rejects unsupported document formats | Keep one explicit format marker; avoid a second repository manifest solely to remove this line. |
| `sources` | Public source references; required for reference knowledge | Include only when nonempty. Never publish private transcript paths/URLs to fill it. An original experience can omit it. |
| `status` | Retired entries leave normal retrieval but remain explainable | Default to active when absent; serialize only retired. |
| `retirement_reason` | Explains why a retired entry should no longer be used | Require only for retired entries; omit for active entries. |

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
   optional feedback -> retirement flow, including cross-task update refusal.
   An existing model-extraction success does not prove this format transition.

This review adds no new scoring, promotion threshold, source service or model
invocation. It preserves detailed bodies and the user's loose optional votes.
