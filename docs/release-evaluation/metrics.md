# Quality and cost metrics

No improvement percentage is predefined. Unknown stays unknown.

## Quality (task, not votes)

| Metric | Meaning |
| --- | --- |
| `task_completed` | The requested work finished with original artifacts (patch, log, test output) |
| `original_artifact_ok` | Artifacts are the named public/historical files, not a judge summary |
| `repeat_investigations` | Extra source-reading / debugging loops after the first |
| `repeat_experiments` | Extra device or serving runs |
| `human_corrections` | User redirects |
| `wrong_method_triggered` | Domain method loaded that the reverse-trigger cases forbid |
| `knowledge_used` | `knowledge_use` (or equivalent) actually recorded |
| `knowledge_contributed` | Independent evidence that the used entry changed the work; not `helpful` |

Forbidden as success: product judge `helpful`, Store `weight` / BM25 score increase.

## Cost

Record foreground and background separately:

- input / output / cached-input tokens
- wall seconds
- model calls

Cached input is observed and is not added again as new billed input, and is not
omitted. Background organize/judge cost is part of total cost.

## Caps (runner defaults)

- `wall_seconds=900` per case (overridable)
- `max_model_calls=8`
- `auto_retry=false`, `retries=0`

Timeout, discard, and no-output remain in the denominator as failed samples.
