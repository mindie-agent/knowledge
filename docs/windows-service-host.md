# Windows knowledge service ownership

The knowledge consumer must outlive the native application that accepted a
Stop event. A detached child is insufficient when the native application owns
an ancestor Windows Job with kill-on-close and no breakaway permission. A
real Codex MCP fixture reproduced that boundary.

Windows now starts one on-demand Task Scheduler action per user SID and
physical domain root. It uses the current virtual environment's `pythonw.exe`,
an interactive login token, and least privilege. There are no periodic or
logon triggers. An unavailable interactive token or scheduler operation is a
visible startup failure; there is no alternate launch backend.

The existing authenticated connection, worker status and consumer lock remain
the service authority. A registered or running task is not readiness. The
task descriptor records launch identity and cleanup ownership, not another
sharing choice, provider configuration, or knowledge state.

## Launch context

Adapters may declare `service_launch_context` in their immutable engine config:

```json
{
  "service_launch_context": {
    "paths": {"SELECTED_ADAPTER_CONFIG": "/absolute/selected/config.json"},
    "environment": ["SELECTED_PROVIDER_KEY", "HTTPS_PROXY"]
  }
}
```

`service_context.validate(config)` returns the validated `paths` mapping and
optional environment name list. Adapters select only their profile paths,
selected provider authentication names and a finite transport-variable list.
Loader, process/session identity and general environment overrides are rejected.
Names do not make missing optional values mandatory.

Only declared values present in the invoking process cross the Windows
boundary. Current-user DPAPI protects a private, one-use transfer bound to the
SID, config fingerprint, launch nonce and cwd. Task XML contains its location
and invocation, never credentials. After successful validation and deletion of
the transfer, the host clears declared ambient variables before applying the
caller projection. This preserves an explicitly absent caller credential or
proxy even if the Scheduler host has an older ambient value. Unrelated host
variables are not cleared. Persisted path bindings also apply before engine
loading on POSIX.

The task XML omits `WorkingDirectory`; the host changes to its bound original
cwd before serving. This preserves relative component behavior without the
Scheduler schema's 260-character working-directory field. Data, manifest,
locks and diagnostic paths retain Windows extended paths for I/O. The
interpreter itself must fit the Scheduler command-path field; an oversized
interpreter path fails explicitly.

## Start, uncertainty and retirement

`ensure_service` owns the existing domain start lock until authenticated
readiness or a real failure. `prepare_service` and Windows Stop wake use that
same lock and may return `starting` without claiming readiness or indexing.
The service owns and cancels its summary children as before.

Registration and Start intent are written before each external operation.
Exact task XML, SID, one Exec action and one principal are checked before
reuse or removal. A nonce can enter the host once. A manager timeout, broken
reply, or idle observation after Start is not proof that Start was unsent.
Those attempts retain their identity and are reconciled without repeating
Start. A Start accepted by the manager followed by local receipt failure is
reported as `start_receipt_write_failed` with uncertainty; its transfer remains
available for the already accepted action.

Transfers are removed after consumption and after proven pre-start failure.
An accepted or uncertain pending Start retains its encrypted transfer until
the action consumes it or exact terminal reconciliation/retirement cleans it.
No timer converts uncertainty into permission to start again.

Exact-config retirement bars new admission and waits for the existing
consumer's safe exit. It then removes only the verified task and confirms
absence. Manager cleanup has finite observation: incomplete cleanup reports
`task_cleanup_pending` separately from completed business retirement. It
does not kill useful work or erase queued material.

Safe caller receipts and startup diagnostics retain static host stage/code
and uncertainty. They do not include raw provider messages, environment
values, transcript content or private paths.

Task queries use the Scheduler COM interface to look up the exact task name.
They do not enumerate all registered tasks. Only `ERROR_FILE_NOT_FOUND` from
that lookup means absence; other manager errors remain failures. The owner SID
returned by the directory ACL check is reused within the same operation.
Registration, Start, deletion, their separate durable intents and readbacks
remain unchanged.

COM omits a default RunLevel that CIM exports explicitly. The query projection
fills a missing XML node only from the observed effective COM RunLevel enum.
An explicit XML/COM disagreement or unknown task state is a visible error.
The existing strict XML ownership validator remains unchanged. Same-task
Windows comparisons verify identical SID, state, LastRunTime and LastTaskResult
before first execution, while running and after exit, including the never-run
timestamp. A representation change must not become evidence to replay Start.

A same-script, new-profile Windows Stop comparison reduced foreground time
from 6.97 to 3.12 seconds; the two queries fell from 5.16 to 1.57 seconds.
This is one controlled regression comparison, not a cross-machine latency
guarantee. The first exact query still took 1.16 seconds, so a fixed three-second
test guard cannot substitute for observing the required foreground result.
No product execution deadline was changed.

## Windowless standard streams and startup errors

Unlike a detached console process with redirected handles, `pythonw` may set
`sys.stdin`, `sys.stdout` and `sys.stderr` to `None`. A real Windows retrieval
startup failed when ReMe configured its logger with that absent output stream.
The service host supplies a real UTF-8 null-device file only for each missing
stream before loading components. Existing streams are preserved, and the host
closes only the handles it created. This matches the existing detached
`DEVNULL` contract without changing third-party logging or storing raw output.

When a component fails during startup, its static stage, exception class and
cause travel in the current attempt's result. The polling caller retains those
details alongside the host code instead of replacing the original failure with
a generic host exception. Both boundaries validate the static allowlist;
exception messages and source content do not cross it.

## Verification boundaries

`test_windows_service_host.py` covers context projection and absence,
one-use binding, real Windows DPAPI, task ownership, startup serialization,
interrupted external operations, post-Start receipt failure, finite cleanup
and safe error delivery. The Windows startup/retirement suites explicitly
select a direct-process test backend to exercise real RPC and lock behavior
in CI without an interactive login. This is a named test fixture, not a
production fallback or Scheduler acceptance claim.

Separate Lenovo evidence uses the real native Codex MCP host, product
bounded helper, Scheduler action and knowledge service. It checks
authenticated readiness before native exit, worker and consumer ownership
after exit, then exact retirement and task absence. The fixture uses no
model, captures no private transcript and publishes nothing. Natural Stop
capture and summary/index/publication acceptance remain separate evidence.

The windowless-stream regression additionally runs the real CC helper,
Scheduler host and ReMe retrieval twice against one synthetic material. Both
queries return the same nonempty reference, reuse the service and index, and
perform one checkpoint load and one material rebuild. Exact retirement
releases the consumer and removes the task. No model is called. Unit fault
injection verifies that a transcript-import error survives the actual host
result and polling path. Unchanged POSIX launch behavior reuses prior native
evidence; these checks do not certify natural Stop or publication.

## Design principles review

Reviewed against design-principles blob
`62cdd5ae40553df2f0f7f7d022141123792d2944`:

1. Total cost: fixes an observed native-process lifetime failure without
   extra model calls, periodic jobs, or manual service orchestration.
2. Closed problem: the host implements one bounded launch/ownership contract;
   it makes no task or content decisions.
3. Understanding burden: existing query and Stop entry points manage startup;
   failures expose static stages instead of requiring users to inspect jobs.
4. Single responsibility: shared core owns service lifetime, while adapters
   declare selected paths and environment names. Existing sharing, admission,
   consumer and provider authorities remain authoritative.
5. Informational skills: no skill behavior or mandatory agent workflow changes.
6. Reference knowledge: no authority, certification, mandatory retrieval or
   publication behavior is added.
7. On demand: tasks start only through an already requested service operation;
   no logon/periodic activation or extra summary request is introduced.
8. Reuse: existing process guards, SID-private directory creation, locks,
   authenticated RPC and retirement are reused. Prior paid/unknown captures
   and unrelated native evidence are preserved.
9. Visible errors: actual readiness remains required; manager uncertainty,
   caller absence, receipt failure and cleanup failure remain distinct.
   Fault injection caught and fixed ambient credential inheritance, split
   startup locks and deletion of a transfer after an accepted Start.

The windowless-stream follow-up was reviewed against the same nine principles.
It removes a reproduced import failure and its generic-error misdiagnosis (1),
implements only the missing-stream and safe startup-result contracts (2, 4),
and requires no new user configuration or orchestration (3). It changes no
skill, knowledge authority or activation behavior (5, 6, 7). It reuses the
existing detached null-output semantics and unchanged POSIX evidence without
model calls (8). Original startup errors remain visible and allowlisted across
the Scheduler boundary, while business exceptions still propagate (9).

The task-query follow-up was reviewed against the same nine principles.
A real synthetic Stop spent 5.16 of its 6.97 seconds in two task queries (1).
The change replaces only the bounded lookup and reuses the already verified
owner identity; it adds no manager process or ownership authority (2, 4).
There are no new user parameters, skills, knowledge rules or activation
requirements (3, 5, 6, 7). Existing write paths, transaction journals and prior
native evidence are reused (8). Exact absence, effective privilege, unknown
state, timestamp compatibility and uncertain Start outcomes remain explicit;
the strict ownership validator and no-replay fault tests stay in place (9).
