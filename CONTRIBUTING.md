# Contributing

Follow the [domain loop contract](docs/mindie-loop.md).
Keep knowledge, experience, actual use and independent feedback distinct.
Changes to public distribution must preserve the redaction boundary and exclude raw captures/use evidence.

From the repository root, run `python -m pytest -q tests` and `python -m mindie_knowledge.corpus_check --repo .`. Unset `MINDIE_FRAMEWORK_SOURCE` uses `tests/fixtures/production-parsers`. A wrong explicit path is a setup error.
Changes to the independent intake package also run its format/transport tests.
A fixture judge checks protocol behavior; model and NPU claims need actual recorded acceptance.

The current CLI is `mindie-knowledge`. Do not restore the former workspace installation or package aliases.
