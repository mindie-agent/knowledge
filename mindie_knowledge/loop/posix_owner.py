"""Bind an attempt group to its core when the OS host cannot retain that group.

The launcher becomes the adapter with exec, preserving PID, PGID and parent.
A small watcher holds only the read end of the core's lifetime pipe. It has no
result, retry, scratch or service responsibilities.
"""

import errno
import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import time

_START_WAIT = 5.0
_ERROR_LIMIT = 256


class LaunchError(OSError):
    def __init__(self, stage, *, not_started=False):
        super().__init__("owned attempt launch failed at " + stage)
        self.stage = stage
        self.mindie_execution = "not_started" if not_started else "unknown"
        self.mindie_category = "configuration" if not_started else "unknown"
        self.cleanup_failed = False


def _watch(owner_fd, ready_fd, status_fd, ready_read, pgid):
    for descriptor in (0, 1, 2, status_fd, ready_read):
        os.close(descriptor)
    try:
        os.write(ready_fd, b"R")
        os.close(ready_fd)
        while os.read(owner_fd, 1):
            pass
    finally:
        # The watcher itself retains this PGID, so a reused PID cannot redirect
        # the signal to an unrelated process group.
        os.killpg(pgid, signal.SIGKILL)


def _exec(owner, owner_fd, status_fd, command):
    stage = "descriptors"
    try:
        if (os.getppid() != owner or os.getpgrp() != os.getpid()
                or not stat.S_ISFIFO(os.fstat(owner_fd).st_mode)):
            raise OSError(errno.EINVAL, "owned attempt declaration is invalid")
        os.set_inheritable(status_fd, False)
        ready_read, ready_write = os.pipe()
        stage = "fork"
        watcher = os.fork()
        if watcher == 0:
            try:
                _watch(owner_fd, ready_write, status_fd, ready_read, os.getpgrp())
            finally:
                os._exit(78)
        os.close(ready_write)
        os.close(owner_fd)
        stage = "watcher"
        if os.read(ready_read, 1) != b"R":
            raise OSError(errno.EIO, "owned attempt watcher did not start")
        os.close(ready_read)
        if os.getppid() != owner:
            raise OSError(errno.ESRCH, "owned attempt owner exited before exec")
        stage = "exec"
        os.execvpe(command[0], command, os.environ)
    except OSError as exc:
        # This message can only be written before the target exec succeeds.
        os.write(status_fd, json.dumps(
            {"stage": stage, "errno": exc.errno}, separators=(",", ":")
        ).encode())
        return 78


def _exec_ack(descriptor):
    deadline = time.monotonic() + _START_WAIT
    data = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
            raise LaunchError("exec_ack")
        value = os.read(descriptor, _ERROR_LIMIT + 1 - len(data))
        if not value:
            break
        data.extend(value)
        if len(data) > _ERROR_LIMIT:
            raise LaunchError("exec_ack")
    if not data:
        return
    try:
        value = json.loads(data)
        if (set(value) != {"stage", "errno"}
                or value["stage"] not in {"descriptors", "fork", "watcher", "exec"}
                or type(value["errno"]) is not int):
            raise ValueError
    except (ValueError, TypeError):
        raise LaunchError("exec_ack") from None
    raise LaunchError(value["stage"], not_started=True)


def spawn(command, *, stdin, env):
    owner_read, owner_write = os.pipe()
    try:
        status_read, status_write = os.pipe()
    except BaseException:
        os.close(owner_read)
        os.close(owner_write)
        raise
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", str(Path(__file__).resolve()),
             str(os.getpid()), str(owner_read), str(status_write), *command],
            stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, pass_fds=(owner_read, status_write), env=env,
        )
    finally:
        os.close(owner_read)
        os.close(status_write)
        if process is None:
            os.close(owner_write)
            os.close(status_read)
    try:
        try:
            _exec_ack(status_read)
        except LaunchError:
            raise
        except OSError as exc:
            raise LaunchError("exec_ack") from exc
        process._mindie_owner_fd = owner_write
        return process
    except BaseException as exc:
        from .process import terminate_tree
        os.close(owner_write)
        try:
            terminate_tree(process, wait=True)
        except Exception as cleanup_error:
            if isinstance(exc, LaunchError):
                exc.cleanup_failed = True
            exc.add_note("owned attempt startup cleanup failed: " + type(cleanup_error).__name__)
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError as cleanup_error:
                if isinstance(exc, LaunchError):
                    exc.cleanup_failed = True
                exc.add_note("owned attempt stream cleanup failed: " + type(cleanup_error).__name__)
        raise
    finally:
        os.close(status_read)


def release(process):
    descriptor = getattr(process, "_mindie_owner_fd", None)
    if descriptor is not None:
        process._mindie_owner_fd = None
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(_exec(int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]),
                           sys.argv[4:]))
