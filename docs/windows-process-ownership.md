# Windows process ownership

Both organizer execution and community Git/gh subprocesses use
`mindie_knowledge.windows_process.spawn_owned`. The child is created suspended,
assigned to a Job with `KILL_ON_JOB_CLOSE`, and resumed through the documented
Win32 Toolhelp and `ResumeThread` APIs. Cleanup terminates the owned Job even
when its original process has exited.

Two previous paths violated the same contract. The organizer attached its Job
after an ordinary spawn, allowing fast descendants to escape before assignment.
Community subprocesses did not attach a Job at all. A PID-based `taskkill /T`
cannot recover ownership once the leader has exited. A surviving descendant can
also keep an inherited pipe open, blocking reader cleanup beyond the deadline.

The focused Windows regression command is:

```sh
python -m pytest -q tests/test_windows_process_ownership.py
```

Its matrix has two callers (organizer and community) and two distinct terminal
states (normal leader exit with detached output, and a descendant holding the
output pipes past the deadline). Each case starts real processes, verifies the
descendant exits, and uses an external watchdog plus an exact opened process
handle to clean up a failing implementation. It does not mock process launch,
process liveness or Job assignment.

On the Windows validation host, the four tests failed against main `873136e`
in 12.45 seconds. They all pass with the candidate, and the ownership plus
diagnostic failure group has 23 passing tests in 10.33 seconds. These tests
exercise the Win32 mechanism; POSIX process-group tests remain separate.

References: [Toolhelp thread snapshots](https://learn.microsoft.com/en-us/windows/win32/api/tlhelp32/nf-tlhelp32-createtoolhelp32snapshot),
[Thread32First](https://learn.microsoft.com/en-us/windows/win32/api/tlhelp32/nf-tlhelp32-thread32first).
