# Independent knowledge acceptance (G1)

This directory is the evaluation plan. The executable runner is
`python -m tests.acceptance_release`. **G1 does not invoke Codex, Grok, Kimi, or
any model.** Codex runs `gpt-5.6-luna` / `max` and feeds records back.

## Phases (do not collapse)

1. `plan --run-dir DIR` — inventory cases, pin model/reasoning/caps, zero retry.
2. `start --run-dir DIR --case ID` — launch spec only; `invoked_model=false`.
3. Executor (Codex) runs the case if `requires_model`, or G1 local checks if not.
4. `complete --run-dir DIR --case ID --record RECORD.json` — **manifest** check
   only: schema, declared `model`/`reasoning` strings, caps, `retries=0`.
   `required_model_verified` stays false. Native Codex `turn_context` (model,
   effort max, thread id, immutable hashes, usage) is verified by the root
   task. `timeout` / `discarded` / `no_output` stay in the denominator.
5. `evidence --run-dir DIR --case ID --artifacts ARTIFACTS.json` — original-artifact
   audit, separate from the product judge.
6. `status --run-dir DIR` — planned / started / completed / valid.

## Record schema (`mindie-acceptance-record/1`)

```json
{
  "schema": "mindie-acceptance-record/1",
  "case": "abc-effect",
  "model": "gpt-5.6-luna",
  "reasoning": "max",
  "auto_retry": false,
  "retries": 0,
  "calls": 1,
  "outcome": "completed",
  "foreground": {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "wall_seconds": 0, "calls": 0},
  "background": {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "wall_seconds": 0, "calls": 0},
  "product_judge": {"verdict": "unknown", "reason": "..."},
  "quality": {}
}
```

Foreground vs background tokens and wall time are required fields. Cached input
is recorded and is not treated as new billed tokens, and is not dropped.

## Local checks G1 already runs

`pytest tests/acceptance_release` covers migration plan/apply/undo, Store
no-hit / version filter / consumption echo, runner schema, evidence rules, and
the rewritten `curate-knowledge` surface. Those are not model or NPU acceptance.

## Not verified here

- Windows real machine
- NPU jobs
- `gpt-5.6-luna` organizer/judge/effect runs (Codex)
- Official `knowledge/vllm-ascend` feed objects (listed remotely, not in this clone)
