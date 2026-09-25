"""Bounded blob reads through one persistent ``git cat-file --batch``.

Spawning a fresh Git process per blob makes whole-tree validation hit the
per-attempt deadline as a knowledge base grows; one long-lived batch process
per validation attempt keeps each read a bounded single-record operation
(at most one blob body in memory) without per-item spawn overhead. The batch
protocol is parsed strictly: a malformed header, a short read or a protocol
desync aborts the attempt as a transient local failure (the caller's
checkpointed progress survives and resumes), never a guessed byte stream.

Whole-tree metadata (``ls-tree``) likewise carries no fixed output cap: it is
written to a scratch file and consumed as bounded chunks, so a growing
catalogue never hits an output-buffer wall.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
import time

from .loop.process import terminate_tree

_HEADER_RE = re.compile(rb"^([0-9a-f]{40,64}) (blob|commit|tree|tag) ([0-9]+)$")
_MAX_HEADER = 4096
_READ_CHUNK = 1024 * 1024


def with_windows_longpaths(env):
    """Copy ``env`` and, on Windows, add command-local ``core.longpaths``.

    ``GIT_CONFIG_*`` is process configuration, so it applies to ``clone``
    before the target repository exists. Existing slots, including
    hooksPath and credential helpers, stay. Other platforms are unchanged.
    No git config file is written.
    """
    out = {str(key): str(value) for key, value in env.items()}
    if os.name != "nt":
        return out
    try:
        count = int(out.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    if count < 0:
        count = 0
    for index in range(count):
        if out.get(f"GIT_CONFIG_KEY_{index}") == "core.longpaths":
            return out
    out["GIT_CONFIG_COUNT"] = str(count + 1)
    out[f"GIT_CONFIG_KEY_{count}"] = "core.longpaths"
    out[f"GIT_CONFIG_VALUE_{count}"] = "true"
    return out


def _git_command(argv) -> bool:
    if not argv:
        return False
    return os.path.basename(str(argv[0])).lower() in {"git", "git.exe"}


def run_stdout_to_file(argv, target, *, timeout, env=None):
    """Run ``argv`` with stdout redirected to ``target`` (no memory cap).

    Metadata output can grow with the catalogue; disk absorbs it. stderr goes
    to a managed scratch file so a chatty child can never block the pipe and
    fake a timeout; only a bounded tail is read afterwards for diagnostics.
    Raises OSError on a nonzero exit and TimeoutError when the bounded wait
    expires; the process tree is always cleaned up.
    """
    merged_env = dict(os.environ)
    if env:
        merged_env.update({str(k): str(v) for k, v in env.items()})
    argv = [str(a) for a in argv]
    if _git_command(argv):
        merged_env = with_windows_longpaths(merged_env)
    spawn = {}
    if os.name == "nt":
        spawn["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        spawn["start_new_session"] = True
    out_handle = open(target, "wb")
    err_fd, err_name = tempfile.mkstemp(prefix="mindie-stderr-", suffix=".log")
    try:
        err_handle = os.fdopen(err_fd, "wb")
        try:
            try:
                process = subprocess.Popen(
                    argv, stdin=subprocess.DEVNULL, stdout=out_handle,
                    stderr=err_handle, env=merged_env, **spawn,
                )
            except (FileNotFoundError, OSError) as exc:
                raise OSError(f"cannot start {argv[0]}: {exc}") from exc
            try:
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    terminate_tree(process)
                    raise TimeoutError(f"{argv[0]} exceeded the bounded wait") from None
                if process.returncode != 0:
                    raise OSError(f"{argv[0]} failed: {_bounded_tail(err_name)}")
            finally:
                if process.poll() is None:
                    terminate_tree(process)
        finally:
            err_handle.close()
    finally:
        out_handle.close()
        try:
            os.unlink(err_name)
        except OSError:
            pass


def _bounded_tail(path, limit=64 * 1024):
    """The last ``limit`` bytes of a scratch stderr file, never the whole log."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            return handle.read().decode("utf-8", "replace").strip()[:300]
    except OSError:
        return ""


def iter_file_records(path, *, separator=b"\0", chunk_size=1024 * 1024):
    """Yield separator-delimited records from a file in bounded chunks."""
    with open(path, "rb") as handle:
        buffer = b""
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            buffer += chunk
            parts = buffer.split(separator)
            buffer = parts.pop()
            yield from parts
        if buffer:
            yield buffer


class CatFileBatch:
    """One ``git cat-file --batch`` reader process with a bounded buffer.

    ``read`` returns the exact blob bytes, or None for a missing object.
    ``max_bytes`` is the real per-file platform envelope: an oversized object
    aborts the attempt (the process is then desynced and must be closed).
    """

    def __init__(self, repo, *, env=None):
        merged_env = dict(os.environ)
        if env:
            merged_env.update({str(k): str(v) for k, v in env.items()})
        merged_env = with_windows_longpaths(merged_env)
        spawn = {}
        if os.name == "nt":
            spawn["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            spawn["start_new_session"] = True
        try:
            self._process = subprocess.Popen(
                ["git", "-C", str(repo), "cat-file", "--batch"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=merged_env,
                **spawn,
            )
        except (FileNotFoundError, OSError) as exc:
            raise OSError(f"cannot start git cat-file --batch: {exc}") from exc
        self._buffer = bytearray()
        self._eof = False
        self._cond = threading.Condition()
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self):
        try:
            while True:
                chunk = os.read(self._process.stdout.fileno(), _READ_CHUNK)
                if not chunk:
                    break
                with self._cond:
                    self._buffer.extend(chunk)
                    self._cond.notify_all()
        except OSError:
            pass
        finally:
            with self._cond:
                self._eof = True
                self._cond.notify_all()

    def _await(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("git blob read exceeded the validation deadline")
        self._cond.wait(timeout=min(remaining, 1.0))

    def _take(self, count, deadline):
        with self._cond:
            while len(self._buffer) < count:
                if self._eof:
                    raise OSError("git cat-file --batch closed its stream")
                self._await(deadline)
            out = bytes(self._buffer[:count])
            del self._buffer[:count]
            return out

    def _readline(self, deadline):
        with self._cond:
            while True:
                index = self._buffer.find(b"\n")
                if index >= 0:
                    line = bytes(self._buffer[:index])
                    del self._buffer[: index + 1]
                    return line
                if len(self._buffer) > _MAX_HEADER:
                    raise OSError("git cat-file --batch returned a malformed header")
                if self._eof:
                    raise OSError("git cat-file --batch closed its stream")
                self._await(deadline)

    def read(self, rev, *, deadline, max_bytes):
        """The exact bytes of one blob revision (``commit:path`` or SHA)."""
        try:
            self._process.stdin.write(rev.encode("utf-8") + b"\n")
            self._process.stdin.flush()
        except OSError as exc:
            raise OSError(f"git cat-file --batch write failed: {exc}") from exc
        line = self._readline(deadline)
        if line == rev.encode("utf-8") + b" missing":
            return None
        header = _HEADER_RE.fullmatch(line)
        if header is None:
            raise OSError(f"git cat-file --batch returned a malformed header: {line[:80]!r}")
        _sha, kind, size = header.groups()
        size = int(size)
        if kind != b"blob":
            raise OSError(f"{rev} is a {kind.decode()}, not a blob")
        if size > max_bytes:
            # The real per-file platform envelope: reject the candidate
            # without reading the oversized body into memory. The caller
            # aborts the attempt, so the desynced process is simply closed.
            raise ValueError(f"{rev} exceeds the per-file byte limit")
        data = self._take(size, deadline)
        trailer = self._take(1, deadline)
        if trailer != b"\n":
            raise OSError("git cat-file --batch protocol desync")
        return data

    def close(self):
        try:
            self._process.stdin.close()
        except OSError:
            pass
        terminate_tree(self._process)
        try:
            self._process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self._process.kill()
            try:
                self._process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        self._reader.join(timeout=1)
