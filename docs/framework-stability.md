# Framework stability — 2026-09-28

Core recovery and shared configuration; reviewed production revision `0df968a`.

## Everyday use

Invoke the native MindIE entry and continue the existing task: `/mindie-agent`
in Kimi or Claude Code, and `$mindie-agent` in Codex. The first use after
installation asks for a choice once. That choice persists across tasks,
forks, restarts, updates and ordinary failures. Later entries require no
renewal or manual activation/recovery steps. An explicit choice change is
still available through the entry. Automatic diagnostic reporting has its
own independent saved choice and does not follow contribution consent.

With contribution off, the framework performs no Stop collection or organizer
call. Retrieval and remote development remain available. Temporary service
or network failures use the existing worker's persisted recovery state;
corrupt configuration is reported as a fault and never triggers onboarding.

## Implementation and verification

The core owns persisted consent, canonical write locking, bounded recovery and publication state. Native adapters supply identity and transcript parsers.

482 tests passed; one long-path case passed separately under a shorter temporary path. The integrated parser fixtures passed 15 focused checks. These are local component baseline results before the Windows read follow-up. The PR
checks are the source of online CI status; local counts are not CI claims.

From the repository root, use a Python interpreter satisfying the README's
SQLite requirement. Install `pip install -e '.[test]'` for core, or
`pip install -r runtime-requirements.txt` for an adapter, then run:

```sh
MINDIE_FRAMEWORK_SOURCE=tests/fixtures/production-parsers python -m pytest -q tests
```

Tests use committed real parser/peer fixtures or exact Git revisions declared
in the workflow. They do not discover a user's production installation.
Missing dependencies fail explicitly. Keep failure-path, concurrency and
recovery cases bounded and deterministic; model sessions are reserved for
checks that actually need a native host.

## Windows configuration reads and writes

The shared configuration implementation combines delete-sharing reads with
a native atomic rename on Windows. Existing readers retain the complete old
document, and readers opening the published path see the complete new one.
The core and all three bootstrap copies use the same implementation. This
requires both sides of the mechanism: Python's Windows `os.replace` alone
cannot replace an open destination. See the [Windows rename semantics](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information).

An external program holding a file without delete sharing can still block
an update; that error preserves the saved document and does not trigger
onboarding or a retry loop. Windows CI checks this filesystem contract;
it is separate from acceptance inside a native Windows agent host.

## Integration and acceptance boundaries

Publish the exact dependency commits before adapter CI. Process the reviewed
PRs in order: knowledge core, Claude Code, Kimi, Codex. Preserve published
commit identities used by runtime and test pins.

Isolated native read-only entry and persistent choice were exercised across
the three hosts. Claude's normal generated package and pinned runtime were
verified. Evidence for unchanged entry paths can be reused; it does not
prove native contribution Stop, publication, or Windows host acceptance.
Those remain separate integration checks. Historical dated reports retain
their original scope. Domain asset import and new business workflows remain
deferred while the framework stabilizes.
