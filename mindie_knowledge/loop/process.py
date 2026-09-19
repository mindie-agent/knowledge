"""Bound owned process trees and pipe memory without retaining retry work.

Process-tree cleanup is portable: POSIX uses a new session and ``killpg``;
Windows uses ``CREATE_NEW_PROCESS_GROUP`` plus ``taskkill /T`` on the tree.
Both paths terminate descendants that outlived the direct child. The Windows
path is implemented to the same contract but is verified on macOS/Linux CI
only until a Windows machine runs it.
"""

import os
import selectors
import signal
import subprocess
import tempfile
import time


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
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
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


def bounded_run(command, payload, *, timeout, max_output):
    with tempfile.TemporaryFile() as input_file:
        input_file.write(payload.encode())
        input_file.seek(0)
        process = _spawn(command, input_file)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "out")
        selector.register(process.stderr, selectors.EVENT_READ, "err")
        output = bytearray()
        total = 0
        deadline = time.monotonic() + timeout
        try:
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise TimeoutError("maintenance deadline exceeded")
                for key, _ in selector.select(
                    min(0.1, max(0, deadline - time.monotonic()))
                ):
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > max_output:
                        raise ValueError("maintenance output exceeds limit")
                    if key.data == "out":
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
            selector.close()
            process.stdout.close()
            process.stderr.close()
