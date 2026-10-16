# Service & Attempt Process Lifetime

[中文](service-process-lifetime.zh.md) | **English** · [Back to Documentation Center](README.en.md)

During normal MindIE Agent operation, three execution scopes with very different lifetimes interact:
1. **Short-lived foreground calls and Hooks** (such as a `Stop Hook` that exits in milliseconds);
2. **The on-demand background knowledge service** (`mindie-knowledge serve`, which must survive after the short Hook exits to finish redaction, chunking, and indexing);
3. **Per-attempt worker subprocess trees** (such as the small-model summary worker or Gitleaks scanner, whose entire descendant tree must be terminated cleanly if the operation is cancelled or the parent service dies).

This guide explains how `mindie-knowledge` uses native OS containment mechanisms across Windows, Linux/WSL, and macOS to guarantee both service survival and zero orphan processes.

---

## 1. Platform Containment Matrix

| Platform | Background Service Host (Survives Short Hook Exit) | Per-Attempt Subprocess Tree Containment (Prevents Orphans) |
| --- | --- | --- |
| **Native Windows** | **On-Demand Task Scheduler Action** (registered per user SID via native `pywin32` COM APIs running `pythonw.exe`) | **Named Job Object** (`CREATE_SUSPENDED` + `KILL_ON_JOB_CLOSE`; kills all descendants even if an intermediate venv launcher exits early) |
| **Linux / WSL** | **On-Demand `systemd` User Service** (requires `systemd 254+` unified cgroup hierarchy with `Type=exec`, `ExitType=main`, `KillMode=control-group`) | **Per-Attempt Process Group + Service cgroup Boundary** (normal cancellation reaps the process group; abrupt service crash is cleaned up by the unit's control group) |
| **macOS** *(and direct non-systemd calls)* | **Detached Service Process Group** coordinated via startup lock & authenticated local loopback RPC | **`owner-pipe` Watcher Process** (core holds the sole write end of a pipe; if core closes the pipe or crashes, EOF triggers the watcher to kill the entire attempt process group) |

> [!NOTE]
> **Why an `owner-pipe` watcher on macOS instead of `launchd`?**
> On macOS, `launchd` only reaps its direct job process group; descendants that enter a separate session or outlive an exited intermediate launcher can survive if the parent core crashes. With the lightweight `owner-pipe` watcher, only `mindie-knowledge` holds the pipe's write descriptor: if the core process dies for any reason (even `SIGKILL`), the kernel closes the pipe immediately and the watcher reaps the entire attempt process group.

---

## 2. One-Use Encrypted/Private Launch Context

When a short-lived Hook starts the background service, it must pass the selected configuration path bindings and optional environment variables (such as `HTTPS_PROXY` or provider credential env names) **without ever writing secret values into Task Scheduler XML or `systemd` unit properties**:

- **Windows**: Protects the one-use transfer file with current-user **DPAPI**, bound to the user SID, configuration fingerprint, launch nonce, and working directory;
- **Linux / POSIX**: Writes a private `0600` regular file owned by the current UID (rejecting symlinks, FIFOs, and group/world-readable files);
- **Consume Once & Delete**: The service host validates and deletes the transfer file before serving, clearing declared absent ambient variables so stale host environment values never leak into the service.

---

## 3. Serialized Startup, Honest Uncertainty & Exact Retirement

- **Serialized Launch & No Blind Replay**:
  The domain startup lock serializes all launch and retirement operations. A durable launch intent is recorded before calling Task Scheduler or `systemd`. If a manager call times out or local receipt persistence fails, the runtime reports the uncertain state and reconciles against actual service readiness rather than blindly spawning a duplicate service.
- **No Arbitrary Deadlines on Healthy Work**:
  Short Hook status probes use a tight foreground observation budget (0.25s) so the Hook returns promptly, **without imposing any execution deadline on ongoing background redaction, indexing, or model calls**.
- **Exact Configuration Fingerprints & Safe Retirement (`retire_service`)**:
  Every service instance is bound to its exact configuration fingerprint. During an idle shutdown or version switch, `retire_service` checks whether any RPC call, worker, or admitted capture is active:
  - If busy, retirement rolls back cleanly so active work is never killed;
  - If idle, it records a durable retirement receipt for that exact configuration fingerprint so late wake helpers cannot accidentally restart the retired service.
