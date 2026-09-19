"""Bound owned process groups and pipe memory without retaining retry work."""

import os
import selectors
import signal
import subprocess
import tempfile
import time


def bounded_run(command, payload, *, timeout, max_output):
    if os.name != "posix":
        raise RuntimeError("bounded maintenance process groups require POSIX")
    with tempfile.TemporaryFile() as input_file:
        input_file.write(payload.encode())
        input_file.seek(0)
        process = subprocess.Popen(
            command,
            stdin=input_file,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env={**os.environ, "MINDIE_MAINTENANCE_GROUP": "1"},
        )
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
            # Also terminate descendants that outlived the direct child.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=1)
            selector.close()
            process.stdout.close()
            process.stderr.close()
