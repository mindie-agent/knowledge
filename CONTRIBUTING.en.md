# Contributing to `mindie-knowledge`

[中文](CONTRIBUTING.md) | **English** · [Back to Homepage](README.en.md)

Thank you for contributing to **MindIE Knowledge** (`mindie-agent/knowledge`)! This repository powers domain knowledge synchronization, local BM25 retrieval, double fail-closed privacy redaction, and community experience sharing for MindIE Agent.

To keep the codebase maintainable, leak-free, and honest under failure conditions, please review the guidelines below before opening a Pull Request.

---

## 1. Core Engineering Guidelines

1. **Knowledge Is Reference Material, Not Authority**:
   Search ranking, generated navigation summaries, usefulness votes, and PR merges never certify that a technical claim is universally true. Keep **public knowledge packages**, **local task drafts**, **actual task use**, and **independent feedback** cleanly separated.
2. **Preserve Fail-Closed Privacy Redaction**:
   Any change touching capture, indexing, outbound staging, or feed sync must guarantee that unredacted transcripts, tool traffic, hidden reasoning traces, internal IPs, `<user>@<host>` login targets, personal home paths, and credentials never enter public distribution paths.
3. **Never Hide Errors or Degrade Silently**:
   When a read fails, a config file is damaged, a scanner crashes, or a subprocess times out, report the failure stage and cause explicitly to the caller. Never turn a read failure into an empty search result, never treat corrupt consent/settings as first-time onboarding, and never blindly replay an `unknown` external write or paid model invocation.
4. **Keep Package & CLI Naming Clean**:
   The CLI entry point is `mindie-knowledge` (package `mindie-knowledge`, module `mindie_knowledge`). Do not reintroduce retired legacy aliases or duplicate state stores.
5. **Separate Fixture Tests from Live Acceptance Evidence**:
   Repository unit tests use deterministic fixtures to verify protocols and failure branches; claims about real models, native agent hosts, or Ascend NPU hardware must cite traceable acceptance records rather than extrapolating from synthetic fixtures.

---

## 2. Local Development & Required Checks

Run both verification commands from the repository root before pushing:

```bash
# 1. Install editable package and test dependencies
python3 -m pip install -e '.[test]'

# 2. Run the unit and integration test suite
MINDIE_FRAMEWORK_SOURCE=tests/fixtures/production-parsers python3 -m pytest -q tests

# 3. Run the deterministic privacy scanner across all public docs and packaged skills
python3 -m mindie_knowledge.redact --check README.md README.en.md CONTRIBUTING.md CONTRIBUTING.en.md docs mindie_knowledge/skills
```

> [!TIP]
> **Avoiding False Positives in Documentation & Fixtures**:
> - `mindie_knowledge.redact` scans public documentation with the same strict ruleset (`r3`) used for corpus exports. When writing examples, always use angle-bracket placeholders (such as `/home/<user>/workspace`, `C:\Users\<user>\...`, `<user>@<host>`) or RFC 5737 documentation IPs (`192.0.2.x`).
> - When `MINDIE_FRAMEWORK_SOURCE` is unset, `tests/conftest.py` uses the committed `tests/fixtures/production-parsers` directory. Passing an invalid explicit path fails immediately as a setup error rather than silently falling back to a global install.

---

## 3. Design Principles Check Before PR Submission

Before opening or updating any development PR (covering code, configuration, dependencies, CI, or documentation), read the [MindIE Agent Engineering Design Principles](https://github.com/mindie-agent/mindie-agent/blob/main/docs/design-principles.en.md) and include a concise `## 设计原则 Review` section in your PR description (see [`.github/pull_request_template.md`](.github/pull_request_template.md)):

- **What real task cost does this change reduce?** Does it avoid unnecessary background work, duplicate state, or cognitive overhead?
- **Are component boundaries clean?** Does it reuse mature OS/host mechanisms and delete superseded paths once replaced?
- **Are all failure branches visible to the caller?** Does it distinguish completed work, known failure, and uncertain (`unknown`) external effects?
- **What verification commands were run, and what external boundaries remain unverified?**
