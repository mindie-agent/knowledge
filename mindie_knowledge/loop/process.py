"""Bound owned process trees and pipe memory without retaining retry work.

Pipe draining uses daemon reader threads and a queue, which works with
subprocess pipes on both POSIX and Windows (``selectors`` cannot select
Windows pipes). Process-tree cleanup: POSIX uses a new session and
``killpg``; Windows uses ``CREATE_NEW_PROCESS_GROUP`` plus ``taskkill /T``.
The Windows path implements the same contract but has no real-machine
evidence yet.
"""

import os
import queue
import signal
import subprocess
import tempfile
import threading
import time


class MaintenanceCancelled(RuntimeError):
    """The owning service is stopping; interrupted work is not replayed."""


def _spawn(command, stdin):
    if os.name == "nt":
        return subprocess.Popen(
            command,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            env={**os.environ, "MINDIE_MAINTENANCE_GROUP": "1"},
        )
    return subprocess.Popen(
        command,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env={**os.environ, "MINDIE_MAINTENANCE_GROUP": "1"},
    )


def terminate_tree(process):
    """Terminate the whole owned tree rooted at ``process``; never raises."""
    if os.name == "nt":
        # taskkill /T walks the descendant tree; /F forces termination.
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
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


def _reader(stream, tag, chunks):
    try:
        while True:
            chunk = os.read(stream.fileno(), 4096)
            if not chunk:
                break
            chunks.put((tag, chunk))
    except OSError:
        pass
    finally:
        chunks.put((tag, None))


def bounded_run(command, payload, *, timeout, max_output, cancel=None):
    with tempfile.TemporaryFile() as input_file:
        input_file.write(payload.encode())
        input_file.seek(0)
        process = _spawn(command, input_file)
        chunks = queue.Queue()
        readers = [
            threading.Thread(
                target=_reader, args=(stream, tag, chunks), daemon=True
            )
            for stream, tag in ((process.stdout, "out"), (process.stderr, "err"))
        ]
        for reader in readers:
            reader.start()
        output = bytearray()
        total = 0
        open_streams = len(readers)
        deadline = time.monotonic() + timeout
        try:
            while open_streams:
                if cancel is not None and cancel.is_set():
                    raise MaintenanceCancelled("maintenance cancelled by shutdown")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("maintenance deadline exceeded")
                try:
                    tag, chunk = chunks.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    continue
                if chunk is None:
                    open_streams -= 1
                    continue
                total += len(chunk)
                if total > max_output:
                    raise ValueError("maintenance output exceeds limit")
                if tag == "out":
                    output.extend(chunk)
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if code:
                raise RuntimeError(f"maintenance agent exited {code}")
            return output.decode()
        finally:
            terminate_tree(process)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
            for reader in readers:
                reader.join(timeout=1)
            process.stdout.close()
            process.stderr.close()
