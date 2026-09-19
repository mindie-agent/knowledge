# Evidence versus product judge

Two verdicts are stored on every model case:

1. **Product judge** — the knowledge component's independent judge
   (`helpful` / `unhelpful` / `unknown`) for one use. It is a usefulness signal
   inside the product. It is not the release effect decision.
2. **Evidence audit** — `tests.acceptance_release.evidence.audit`, which reads
   original artifacts (files, logs, patches, dated acceptance notes) named by
   the case. Missing artifacts → `unknown`.

Effect success requires original artifacts that show reduced investigation,
reduced experiment, an avoided error, success-without-contribution handled
correctly, or a failure that still excluded a wrong hypothesis.

Explicitly insufficient:

- judge `helpful`
- Store feedback counts or BM25 score going up
- task success alone
- this runner's JSON existing on disk

A→B→judge→C: A produces, B uses, judge records, C retrieves. The protocol can
be checked locally without a model (`abc-protocol`). Whether C's work improved
is `abc-effect` and stays unknown until Codex supplies artifacts.
