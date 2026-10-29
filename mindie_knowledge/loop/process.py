"""[Role] Bound owned process trees and pipe memory without retaining retry work.

[Invariants] A validated result is persisted before cleanup; cleanup failure
does not turn completed external work into a retry.
[Timeouts & Locks] Caller timeout bounds execution, followed by bounded process
and pipe cleanup. Explicit child_env propagates a summary's enclosing deadline.
[Side-Effect Policy] Spawns and reaps only the owned tree and temporary scratch.

Pipe draining uses daemon reader threads and a bounded queue, which works
with subprocess pipes on both POSIX and Windows (``selectors`` cannot select
Windows pipes). The queue is deliberately bounded so a runaway child cannot
grow memory past the output cap before the consumer notices; readers block
briefly and drop nothing while the consumer is alive.

Process-tree cleanup: POSIX uses a new session and ``killpg``. Windows
assigns the spawned process to a Job Object so descendants stay owned, then
``TerminateJobObject``. A suspended start establishes Job ownership before
the child can create descendants; community Git/gh calls share this mechanism.
Real Windows tests verify descendant cleanup after leader exit for this runner
and after an explicit inherited-pipe deadline for the community caller.
"""

import ctypes
from functools import lru_cache
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from .dfx import failure
from . import posix_owner, summary_budget, summary_scratch


def spawn_service(command, *, from_detached_starter=False):
    """Transfer an explicit local service/starter beyond the short caller.

    Windows callers that own a Job must explicitly permit breakaway. Failure
    stays visible; never return a false successful start inside a dying Job.
    The service remains owned by its consumer lock and authenticated endpoint.
    A detached starter has already crossed the short caller's Job boundary.
    Its service inherits that surviving boundary: trying to break away again
    can be denied by an outer host Job even though the first transfer succeeded.
    """
    options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        options["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            | (0 if from_detached_starter else subprocess.CREATE_BREAKAWAY_FROM_JOB)
        )
    else:
        options["start_new_session"] = True
    return subprocess.Popen(command, **options)


class MaintenanceCancelled(RuntimeError):
    """The owning service is stopping; interrupted work is not replayed."""


# Trusted adapters report these categories through their exit status. Never
# parse provider stderr: it can contain secrets, task material or reasoning.
# An unclassified/older adapter exit remains unknown, not a guessed timeout.
AGENT_ERROR_EXIT_CODES = {
    "configuration": 78,
    "deadline": 124,
    "native": 70,
    "invalid_result": 65,
    "output_limit": 75,
}


def _failure_detail(category, started):
    return f"category={category}; elapsed={time.monotonic() - started:.3f}s"


def annotated_error(exc, category, started, exit_code=None, stage="run"):
    """Attach diagnostics and return the same exception object.

    The trusted category also travels on the exception object so the budget
    layer can tell a per-item content failure (invalid result, output limit)
    apart from a shared configuration/runtime failure without parsing text.
    """
    exc.mindie_category = category
    failure(
        "organizer.process",
        stage=stage,
        category=category,
        exception=exc,
        elapsed_ms=(time.monotonic() - started) * 1000,
        exit_code=exit_code,
        reportable=category in {"invalid_result", "output_limit", "cleanup"},
    )
    return exc


def _spawn(command, stdin, scratch=None, child_env=None):
    env = {**os.environ, "MINDIE_MAINTENANCE_GROUP": "1",
           "MINDIE_MAINTENANCE_OWNER": str(os.getpid())}
    env.pop(summary_scratch.ENVIRONMENT_KEY, None)
    env.pop(summary_budget.DEADLINE_ENV, None)
    if child_env is not None:
        env.update(child_env)
    if scratch is not None:
        env[summary_scratch.ENVIRONMENT_KEY] = summary_scratch.declaration(scratch)
    if os.name == "nt":
        from mindie_knowledge.windows_process import spawn_owned
        return spawn_owned(
            command,
            job_name=summary_scratch.job_name(scratch) if scratch is not None else None,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    from .service_host import process_is_contained
    if not process_is_contained():
        return posix_owner.spawn(command, stdin=stdin, env=env)
    return subprocess.Popen(
        command,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=env,
    )


@lru_cache(maxsize=1)
def _native_group_inspector():
    # libproc metadata remains usable inside sandboxes that deny setuid ps.
    # Probe exported capabilities once; observation failures never select ps.
    native = ctypes.CDLL(None)
    if all(hasattr(native, name) for name in ("proc_listpgrppids", "proc_pidinfo")):
        from .darwin_process import group_has_writers
        return group_has_writers
    return None


def _posix_group_has_writers(pgid, *, timeout):
    """Inspect only process metadata; a zombie cannot write to owned scratch."""
    inspector = _native_group_inspector()
    if inspector is not None:
        return inspector(pgid, timeout=timeout)
    return _ps_group_has_writers(pgid, timeout=timeout)


def _ps_group_has_writers(pgid, *, timeout):
    executable = shutil.which("ps")
    if executable is None:
        raise RuntimeError("owned process group inspection requires libproc or ps")
    # BSD -g selects a process group; Linux -g selects a numeric session.
    # Our group starts a new session, and exact PGID filtering excludes any
    # other group in that session. No command lines or environment are read.
    result = subprocess.run(
        [executable, "-o", "pid=,pgid=,stat=", "-g", str(pgid)],
        capture_output=True, text=True, timeout=timeout,
        # WSL reached through a Windows SSH console can inherit an invalid
        # terminal width. This metadata-only query has no terminal formatting.
        env={**os.environ, "LC_ALL": "C", "COLUMNS": "80", "LINES": "24"},
    )
    if result.stderr.strip() or result.returncode not in (0, 1):
        raise RuntimeError("owned process group inspection failed")
    if result.returncode == 1:
        if result.stdout.strip():
            raise RuntimeError("owned process group inspection was incomplete")
        return False
    has_writers = False
    for line in result.stdout.splitlines():
        parts = line.split()
        if (len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit()
                or int(parts[0]) <= 0 or int(parts[1]) <= 0):
            raise RuntimeError("owned process group metadata is invalid")
        if int(parts[1]) != pgid:
            continue
        # Unknown states remain potential writers. Only an explicit zombie
        # state proves an existing process can no longer use the profile.
        state = parts[2]
        if not (state.startswith("Z") and all(c in "<>NLSsl+EWX" for c in state[1:])):
            has_writers = True
    return has_writers


def terminate_tree(process, *, wait=False):
    """Terminate an owned tree; optionally prove it has no remaining writers."""
    if os.name == "nt":
        from mindie_knowledge.windows_process import terminate_owned
        terminate_owned(process, wait=wait)
        return
    try:
        _terminate_posix_tree(process, wait=wait)
    finally:
        original_error = sys.exc_info()[1]
        try:
            posix_owner.release(process)
        except TimeoutError:
            raise
        except OSError as cleanup_error:
            if original_error is None:
                raise
            original_error.add_note("owned attempt lifetime pipe did not close")


def _terminate_posix_tree(process, *, wait):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    if wait:
        process.wait(timeout=1)
        deadline = time.monotonic() + 2
        while True:
            signal_error = None
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            except PermissionError as exc:
                signal_error = exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if signal_error is not None:
                    raise signal_error
                raise RuntimeError("owned process group did not exit during cleanup")
            try:
                if not _posix_group_has_writers(process.pid, timeout=remaining):
                    break
            except Exception as inspection_error:
                if signal_error is not None:
                    raise signal_error from inspection_error
                raise
            time.sleep(0.01)


def _reader(stream, tag, chunks, cancel, errors):
    try:
        while True:
            chunk = os.read(stream.fileno(), 4096)
            if not chunk:
                break
            # Bounded hand-off: if the consumer stopped (cancellation), the
            # child is being killed; blocked readers exit instead of growing
            # memory without limit.
            while True:
                try:
                    chunks.put((tag, chunk), timeout=0.1)
                    break
                except queue.Full:
                    if cancel is not None and cancel.is_set():
                        return
    except TimeoutError:
        raise
    except OSError as exc:
        errors.append(exc)
    finally:
        try:
            chunks.put((tag, None), timeout=0.5)
        except queue.Full:
            pass


def bounded_run(command, payload, *, timeout=None, max_output, cancel=None,
                cleanup_receipt=None, on_result=None, child_env=None):
    started = time.monotonic()
    if cancel is not None and cancel.is_set():
        exc = MaintenanceCancelled('maintenance cancelled before start')
        exc.mindie_execution = 'not_started'
        raise exc
    with tempfile.TemporaryFile() as input_file:
        input_file.write(payload.encode())
        input_file.seek(0)
        scratch = None
        try:
            if on_result is not None:
                scratch = summary_scratch.create_root()
            process = _spawn(command, input_file, scratch, child_env)
        except TimeoutError:
            raise
        except (OSError, posix_owner.LaunchError) as exc:
            if not hasattr(exc, "mindie_execution"):
                exc.mindie_execution = 'not_started'
            if getattr(exc, "cleanup_failed", False):
                if cleanup_receipt is not None:
                    cleanup_receipt["cleanup_failed"] = True
            elif scratch is not None:
                try:
                    shutil.rmtree(scratch)
                except TimeoutError:
                    raise
                except OSError as cleanup_error:
                    failure("organizer.process", stage="scratch-cleanup", category="cleanup",
                            exception=cleanup_error)
                    if cleanup_receipt is not None:
                        cleanup_receipt['cleanup_failed'] = True
            raise annotated_error(exc, getattr(exc, "mindie_category", "configuration"),
                                  started, stage='start')
        chunks = queue.Queue(maxsize=max(2, max_output // 4096 + 1))
        readers_stop = threading.Event()
        read_errors = []
        readers = [
            threading.Thread(
                target=_reader, args=(stream, tag, chunks, readers_stop, read_errors), daemon=True
            )
            for stream, tag in ((process.stdout, "out"), (process.stderr, "err"))
        ]
        for reader in readers:
            reader.start()
        output = bytearray()
        total = 0
        open_streams = len(readers)
        result_saved = False
        deadline = None if timeout is None else started + timeout
        try:
            while open_streams or process.poll() is None:
                if read_errors:
                    raise annotated_error(RuntimeError("maintenance output read failed"),
                                          "invalid_result", started) from read_errors[0]
                if cancel is not None and cancel.is_set():
                    raise MaintenanceCancelled("maintenance cancelled by shutdown")
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise annotated_error(
                        TimeoutError(
                            "maintenance deadline exceeded; "
                            + _failure_detail("deadline", started)
                        ),
                        "deadline",
                        started,
                    )
                if not open_streams:
                    try:
                        process.wait(timeout=0.05 if remaining is None else min(0.05, remaining))
                    except subprocess.TimeoutExpired:
                        pass
                    continue
                try:
                    tag, chunk = chunks.get(timeout=0.1 if remaining is None else min(0.1, remaining))
                except queue.Empty:
                    if process.poll() is not None:
                        break  # Native exit, even when a descendant inherited its pipes.
                    continue
                if chunk is None:
                    open_streams -= 1
                    continue
                total += len(chunk)
                if total > max_output:
                    raise annotated_error(
                        ValueError(
                            "maintenance output exceeds limit; "
                            + _failure_detail("output_limit", started)
                        ),
                        "output_limit",
                        started,
                    )
                if tag == "out":
                    output.extend(chunk)
                    if on_result is not None and not result_saved and b'\n' in output:
                        on_result(bytes(output).decode())
                        result_saved = True
                        break  # A validated terminal envelope closes this operation.
            if read_errors:
                raise annotated_error(RuntimeError("maintenance output read failed"),
                                      "invalid_result", started) from read_errors[0]
            try:
                # Without a saved terminal result, the loop above observes
                # actual exit while keeping cancellation and any explicit
                # deadline live even after both output pipes have closed.
                code = process.wait(timeout=2 if result_saved else None)
            except subprocess.TimeoutExpired:
                if result_saved:
                    if cleanup_receipt is not None:
                        cleanup_receipt['cleanup_failed'] = True
                    code = 0
                else:
                    raise annotated_error(
                    TimeoutError(
                        "maintenance deadline exceeded; "
                        + _failure_detail("deadline", started)
                    ),
                    "deadline",
                    started,
                    ) from None
            if code and result_saved:
                if cleanup_receipt is not None:
                    cleanup_receipt['cleanup_failed'] = True
                code = 0
            if code:
                category = next(
                    (name for name, value in AGENT_ERROR_EXIT_CODES.items()
                     if value == code), "unknown"
                )
                raise annotated_error(
                    RuntimeError(
                        f"maintenance agent exited {code}; "
                        + _failure_detail(category, started)
                    ),
                    category,
                    started,
                    exit_code=code,
                )
            try:
                return output.decode()
            except UnicodeDecodeError as exc:
                raise annotated_error(exc, "invalid_result", started, stage="decode")
        finally:
            original_error = sys.exc_info()[1]
            cleanup_stage = "cleanup"
            try:
                readers_stop.set()
                terminate_tree(process, wait=scratch is not None)
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
                for reader in readers:
                    reader.join(timeout=1)
                for reader, stream in zip(readers, (process.stdout, process.stderr)):
                    if not reader.is_alive():
                        stream.close()
                if any(reader.is_alive() for reader in readers):
                    raise RuntimeError('owned protocol pipes did not close during cleanup')
                if scratch is not None:
                    cleanup_stage = "scratch-cleanup"
                    shutil.rmtree(scratch)
            except Exception as cleanup_error:
                failure(
                    "organizer.process",
                    stage=cleanup_stage,
                    category="cleanup",
                    exception=cleanup_error,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
                if cleanup_receipt is not None:
                    cleanup_receipt['cleanup_failed'] = True
                elif original_error is None:
                    raise
