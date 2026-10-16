# Contributing

[中文](CONTRIBUTING.md) | English

Thank you for contributing to **MindIE Knowledge** (`mindie-agent/knowledge`), the shared domain knowledge and task-experience loop runtime for MindIE Agent.

Before submitting code, configuration, build/CI, or documentation changes, please follow the engineering boundaries and verification requirements below.

---

## Core Engineering Contracts

1. **Follow the Domain Loop Contract**: All changes must adhere to the [MindIE domain loop contract](docs/mindie-loop.md) ([中文](docs/mindie-loop.zh.md)).
2. **Keep Domain Concepts Distinct**: Keep **knowledge**, **task experience**, **actual business use**, and **independent feedback** clearly separated. Search ranking, summary generation, votes, and PR merges never grant decision authority to an entry.
3. **Preserve the Redaction Boundary**: Any change affecting public distribution, feed sync, or community sharing must preserve the fail-closed redaction boundary and exclude raw transcripts, unredacted material, private host paths, credentials, and raw usage evidence.
4. **Keep Current Package & CLI Naming**: The current CLI is `mindie-knowledge` (package `mindie-knowledge`, module `mindie_knowledge`). Do not restore retired workspace installation paths or legacy package aliases.
5. **Keep Implementation Status Distinct from Acceptance Evidence**: Fixture tests verify protocol and component behavior; claims involving real models, native agent hosts, or Ascend NPU execution require recorded acceptance evidence.

---

## Local Development & Required Checks

Install the package with test dependencies and run the verification suite from the repository root:

```sh
# 1. Install editable package and test dependencies
python -m pip install -e '.[test]'

# 2. Run the unit and integration test suite
MINDIE_FRAMEWORK_SOURCE=tests/fixtures/production-parsers python -m pytest -q tests

# 3. Run the deterministic privacy scanner across all public docs and skills
python -m mindie_knowledge.redact --check README.md README.en.md CONTRIBUTING.md CONTRIBUTING.en.md docs mindie_knowledge/skills
```

- When `MINDIE_FRAMEWORK_SOURCE` is unset, `tests/conftest.py` uses `tests/fixtures/production-parsers` and verifies the three parser files exist. An invalid explicit path is a setup error and never falls back to a production installation.
- Changes to the community intake/publication package (`mindie_knowledge/community`) or `mindie_knowledge/publication_check.py` must also pass their format, contract, and transport test suites.
- Never include strings resembling private IPs, SSH login targets, local user home paths, tokens, or internal email addresses in any Markdown file, test fixture, or PR description.

---

## Mandatory Design-Principles Review Before PR Submission

Before creating or updating any development PR (including draft PRs, covering code, configuration, dependencies, CI, and project documentation), you **must** read the current [MindIE Agent nine design principles](https://github.com/mindie-agent/mindie-agent/blob/main/docs/design-principles.md) and review the final diff and affected behavior against all nine principles:

1. **Measure Agent efficiency by the total cost of achieving the user's actual goal**
2. **Toolify only closed problems with well-defined boundaries**
3. **Do not increase the Agent's cognitive burden**
4. **Give every tool a single, well-defined boundary**
5. **Skills are primarily informational**
6. **Knowledge is for reference only**
7. **All tools and Skills intervene strictly on demand**
8. **Reuse mature mechanisms and existing validated results first**
9. **Tools must be observable and localizable; hiding errors is forbidden**

### Review & PR Description Requirements

- **Errors must remain visible to the caller**: Never turn a required-step failure into success, empty data, first-time onboarding, user disablement, or an undocumented fallback. Distinguish completed work, failure, and uncertain external effects; never replay an uncertain external write or hide the primary error behind a later cleanup failure.
- **Fix violations before submission**: Any principle violation found during review must be fixed and re-verified before opening or updating the PR.
- **Record the review in the PR description**: Complete the `## 设计原则 Review` section from [`.github/pull_request_template.md`](.github/pull_request_template.md), recording:
  - The reviewed commit SHA and design-principles revision;
  - Concrete conclusions and applicability rationale for all nine principles (explain why any non-applicable principle does not apply to this diff; checkboxes, blanket `N/A`, or "all principles followed" are not a review);
  - Findings and fixes applied;
  - Actual validation commands executed and any remaining unverified external boundaries.
