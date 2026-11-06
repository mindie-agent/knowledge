# mindie-knowledge 0.13.14: Validate the Windows background service GUI entry

[中文](knowledge-0.13.14.md)

Fixes [#115](https://github.com/mindie-agent/knowledge/issues/115). The service previously checked only that `pythonw.exe` existed, allowing Task Scheduler to execute a console launcher bearing that name. Before creating a task and before actually starting a recovered registration, the host now reads the PE32/PE32+ headers and requires GUI subsystem 2. Invalid entries report `interpreter:windowless_interpreter_invalid`; missing files report `interpreter:windowless_interpreter_missing`. Rejection never registers or starts a new task.

- Uncached, read-only validation covers old environments and in-place updates. Repairing the environment permits a normal retry while preserving venv identity, arguments, stop behavior and the no-replay protocol for uncertain starts.
- Experiment-ledger Git subprocesses now use Windows `CREATE_NO_WINDOW`, including fingerprint calculation from a consoleless service.
- Regressions cover a real console `python.exe` named `pythonw.exe`, rejection before registration, old-registration recovery and retry after repair. Documented guardrails cover both entry and subprocess boundaries.
- Includes main's merged #117 test fix: cancelled zombie process groups are no longer mistaken for live process trees.

Validation: the Windows startup, retirement, ownership, Git, network and RSI scope passed 204 tests with 3 skips. Real interactive-desktop Task Scheduler acceptance passed 1 test: service readiness, preserved venv and arguments, no visible window or descendant `conhost.exe`, followed by normal stop and deletion of the temporary task.

Measured on Windows x86_64, uv 0.13.3 and CPython 3.13.16. The incident's original uv 0.11.11 / CPython 3.13.13 combination was not reinstalled; the origin of the invalid binary is not inferred. Hosted CI lacks an interactive login token; run real scheduled acceptance explicitly on an interactive desktop as documented in `docs/windows-service-host.md`. The formal release workflow runs the full component suite and benchmarks the exact tagged source, delivering those reports as release assets.
