# Service and attempt lifetime

The knowledge core owns admitted work, result persistence and final scratch
cleanup. Native adapters validate the existing shared scratch declaration once;
they do not add another definition of whether a managed core is alive.
An adapter's narrower native subprocess group or Job still owns its terminal
protocol, pipe draining and local cancellation.

## Platform mechanisms

Windows keeps the existing Task Scheduler service host and per-attempt
kill-on-close Job. The named Job membership proof includes the virtual
environment redirector. Killing the real core closes the retained Job handle
and stops descendants even after their intermediate worker exits. No additional
adapter HANDLE or PPID state machine is needed.

Linux and WSL use an on-demand systemd user service with `Type=exec`,
`ExitType=main`, `KillMode=control-group` and `Restart=no`. The supported manager
is systemd 254 or newer with a unified cgroup hierarchy. Version 254 provides
`systemd-run --expand-environment=no`, needed to preserve literal invocation
arguments. An unavailable user manager, unsupported version or unverified
containment is an explicit error. A failed manager launch never falls back to
an unprotected core.

The service main verifies its exact unit command, properties, MainPID and
`/proc/self/cgroup` before marking itself contained. This private process-local
mark is bound to the PID and cannot be inherited by a fork or set through
caller configuration. Normal summary cancellation retains per-attempt process
groups; the service cgroup supplies the core-crash boundary.

macOS launchd only reaps its job's process group. Moving the core under launchd
does not cover summary processes in a separate session; an independent
per-attempt launchd job also survives unrelated core death. Both limitations
were reproduced with synthetic processes, including an exited intermediate
worker. Core therefore uses one small owner-pipe watcher per attempt. The
launcher forks the watcher and then execs the adapter in the same PID, retaining
the existing PPID/PGID contract. Only core retains the pipe's write end. The
watcher closes protocol stdio and kills its own retained group when core closes
the pipe or dies. It has no model, retry, result, scratch or service state.

Direct Linux APIs and history imports outside the verified service reuse this
same watcher. This is an explicit execution boundary chosen before launch,
not a fallback after systemd fails. Fork, descriptor and exec failures cannot
continue without protection. Proven pre-exec failure is distinguished from a
lost or uncertain launch acknowledgement.

## One-use context and manager transactions

The immutable engine configuration declares permitted path bindings and
optional environment names. Windows continues using current-user DPAPI.
Linux uses a private current-UID regular file with mode 0600; symlinks, FIFOs,
foreign owners and group/world-readable transfers are rejected. Neither Task
Scheduler metadata nor systemd unit properties contain credential values.
The transfer is bound to the exact config, attempt, owner and cwd, validated
and deleted before serving. Declared absent environment values are removed;
POSIX names remain case-sensitive and Windows names case-insensitive.

The existing start lock serializes launch and retirement. A durable intent
precedes the systemd start request. The exact unit command and manager properties
are verified through structured properties, not a formatted command string.
An inactive or absent unit does not prove an uncertain Start was unsent or
finished. Only an exact terminal result or terminal manager failure permits a
later new attempt. Accepted Start with a failed local acknowledgement retains
its context and reports uncertainty. Result receipts validate their attempt,
status and integer terminal exit code before controlling another launch.

Manager IPC and cleanup observations have finite bounds. They do not impose a
deadline on indexing or model execution. Short POSIX Stop handoff only starts
the existing detached wake helper; that helper performs the manager operation
and authenticated readiness. Status observations in the short Hook use a
0.25-second control budget. A silent listener leaves the capture durable and
reports unknown runtime state instead of blocking the Hook or claiming delivery.

## Compatible clients and exact retirement

An adapter can declare a client protocol and semantic business identity.
Runtime code locations are excluded only where the declaration supplies an
explicit compatible protocol; provider, data, authorization and publication
bindings remain part of that identity. An authenticated compatible live owner
can serve an older client without making it another service owner.

Completed retirement may name one verified successor config and fingerprint
in the existing retirement receipt. When no compatible active service exists,
an old client follows only that exact chain. Changed targets, incompatible
business settings, cycles and uncertain retirement are errors. It never scans
versions, selects a newest release, restores a retired owner, or introduces a
global current pointer. Retirement without a successor does not authorize a
client to attach to another compatible owner.

## Results and validation

The durable terminal-result callback still precedes tree cleanup, pipe drain
and scratch deletion. Known usage survives cleanup failure. Unknown paid work
is preserved and never replayed by these mechanisms. Abrupt core death can
leave private scratch or an uncertain attempt receipt; crash-safe process
termination does not claim crash-safe directory deletion.

Model-free tests cover owner death before and after worker exit, launch
failure, invalid result receipts, uncertain manager effects, exact ownership,
context privacy and absence, silent status listeners, and old/new compatible
clients. Platform evidence remains separate from native adapter acceptance.
The existing Windows Task Scheduler contract and descriptor layout are retained.
The common `service_host` module only shares receipt IO, static errors and the
one-use runner; platform manager transactions remain explicit.
