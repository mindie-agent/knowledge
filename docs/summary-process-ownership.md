# Summary process and temporary profile ownership

[中文](summary-process-ownership.zh.md) | English · [Documentation Index](README.en.md)

The shared [service and attempt lifetime](service-process-lifetime.md) contract
now also covers abrupt owner death on macOS, Windows and Linux/WSL. The historical
verification below describes the earlier scratch change; it does not stand in
for the newer platform and native acceptance.

Status: source-level native-process regressions passed; final installation
acceptance pending, 2026-10-15.

A native terminal result does not prove that its process or descendants have
stopped using the temporary profile. Codex intentionally delivers a result
before the shared owner closes its process tree. Claude Code also leaves
inherited-group descendants to that owner after reaping its direct child.
Deleting either profile inside the adapter therefore races with live writers.

## One cleanup owner

Only `bounded_run` calls with a durable `on_result` callback create a fresh
private scratch root. The existing diagnostics directory primitive creates
it with private POSIX permissions or the Windows user's SID. The adapter
creates its temporary profile inside this root and leaves deletion to core.
Core retains the root locally; it never accepts a deletion path from a worker
result.

The order is:

1. Validate and persist the terminal result and known usage through the
   existing callback and ledger.
2. Close the existing owned process tree. On Windows, retain the Job handle
   until `ActiveProcesses` reaches zero. On POSIX, reap the leader and observe
   that its process group has exited.
3. Drain and close owned protocol pipes.
4. Remove only the private scratch root created for this invocation.

Cancellation and failure use the same cleanup order. An uncertain tree exit
retains the private root and reports cleanup failure. A cleanup error cannot
erase a saved result, its usage, or the primary exception, and does not
authorize another model call. The small cleanup waits apply after completion
or cancellation, not to the business operation's execution time.

## Internal owner contract

`MINDIE_SUMMARY_SCRATCH` is an internal parent/worker declaration, not product
configuration. It carries a schema, owner PID, private root, and Windows Job
identity. Every spawn clears inherited declarations before creating its own.
Service launch configuration cannot inject this name, and adapters remove it
before launching native model processes.

On POSIX the adapter verifies its direct parent and owned process group. On
Windows a virtual-environment launcher may start the actual interpreter as
its child, so direct-parent equality is not a valid ownership test. Core gives
the existing Job a random name, rejects an existing object with that name
before modifying it, and assigns the suspended worker before resuming it.
The adapter briefly opens that exact Job for query, verifies membership, then
closes the query handle before invoking the model. This creates no additional
Job or resident owner and preserves kill-on-close behavior.

A direct adapter worker without a maintenance owner retains its existing
self-cleanup behavior. A managed worker with an absent or invalid declaration
fails before invoking a model. The adapters and core must be installed at
compatible pins; an old incomplete owner declaration is not permission to
defer directory cleanup. Installation probes check the new API.

This is not a durable scratch journal. An abrupt core death can leave a
private directory, and the change does not claim crash-proof deletion or scan
old directories automatically.

## Verification and review

Synthetic process tests exercise a descendant that still writes its profile
when the result callback runs, then require that both the tree and scratch
have gone when core returns. Separate tests cover direct execution, invalid
ownership before native start, cancellation, callback failure, spawn failure,
and cleanup failure with preserved usage. These tests call no model. Native
Windows must additionally exercise the actual venv launcher and Job boundary;
POSIX success alone does not establish that behavior.

The targeted macOS core selection passed 48 tests, with 9 Windows-only skips.
Lenovo passed the 20 scratch/lifetime cases, then both additional foreign-Job
and Job-name-collision cases. The actual venv redirector and descendant writer
are included. Codex's affected macOS adapter selection passed 52 tests and
31 subtests; Claude Code passed 33 tests and 6 subtests on macOS, and 31 tests
and 6 subtests on Windows with 2 POSIX-only skips. These are model-free source
tests, not a new installed candidate or natural Stop acceptance.

All nine design principles were reviewed against official design-principles
blob `62cdd5ae40553df2f0f7f7d022141123792d2944`:

1. The change removes a reproduced cleanup race and prevents paid-call replay;
   it adds one temporary root only to an admitted summary invocation.
2. It handles the closed problem of process-owned temporary resources, not
   generic task orchestration.
3. It adds no user parameter, activation step, or required cleanup procedure.
4. The existing process owner and ledger remain authoritative. Adapters do not
   acquire another cleanup owner or return arbitrary deletion targets.
5. No Skill requirement changes.
6. No knowledge entry gains authority, and no historical material is replayed.
7. No extra model call, polling service, or background activation is introduced.
8. Existing process groups, Jobs, private-directory creation, result callbacks,
   and unchanged native evidence are reused.
9. Invalid ownership fails before a model call; tree, pipe, and scratch failure
   preserve the actual business result. Failure branches receive explicit
   regression coverage. Native validation remains separate from source tests.
