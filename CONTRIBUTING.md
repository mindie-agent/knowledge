# Contributing

Follow the [domain loop contract](docs/mindie-loop.md).
Keep knowledge, experience, actual use and independent feedback distinct.
Changes to public distribution must preserve the redaction boundary and exclude raw captures/use evidence.

From the repository root, run `python -m pytest -q tests` and `python -m mindie_knowledge.corpus_check --repo .`. Unset `MINDIE_FRAMEWORK_SOURCE` uses `tests/fixtures/production-parsers`. A wrong explicit path is a setup error.
Changes to the independent intake package also run its format/transport tests.
A fixture judge checks protocol behavior; model and NPU claims need actual recorded acceptance.

The current CLI is `mindie-knowledge`. Do not restore the former workspace installation or package aliases.

## Required design-principles review

Before creating or updating a development PR, review the final diff against all nine [MindIE Agent design principles](https://github.com/mindie-agent/mindie-agent/blob/main/docs/design-principles.md), including the explicit error-reporting contract. Fix violations before submission. Record the reviewed commit and principles revision, concrete conclusions and applicability, findings/fixes, and validation gaps in the PR description. Passing tests or checking boxes does not replace the review. This applies to draft PRs too; pure knowledge/feedback contributions continue to use their content rules.
