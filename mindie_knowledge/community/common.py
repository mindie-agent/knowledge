"""Shared helpers for the community package: digests, bounds, subprocesses.

Every external effect in this package is a bounded argv subprocess (git, gh or
the maintainer-configured review CLI). Contribution text is never interpolated
into a shell command: payloads travel via stdin temp files or ``--input`` body
files, and every call carries a deadline plus an output cap.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from mindie_knowledge.loop.process import terminate_tree

SCHEMA_BATCH = "mindie-contribution/1"
SCHEMA_FEEDBACK = "mindie-feedback/1"
SCHEMA_CONFIG = "mindie-community-config/1"
SCHEMA_ENTRY = "mindie-entry/1"
SCHEMA_REVIEW = "mindie-review/1"

MAX_DETAIL = 800
MAX_BATCH_FILES = 200
MAX_FILE_BYTES = 128 * 1024
MAX_BATCH_BYTES = 2 * 1024 * 1024
MAX_TITLE = 240
MAX_VOTE_REASON = 1000

DEFAULT_TRANSACTION_SECONDS = 120
DEFAULT_OPERATION_LIMIT = 60
DEFAULT_GIT_OP_SECONDS = 60
DEFAULT_API_OP_SECONDS = 30


class CommunityError(Exception):
    """A deterministic, user-visible failure. Detail must stay secret-free."""

    def __init__(self, detail: str, *, status: str = "failed"):
        super().__init__(detail[:MAX_DETAIL])
        self.status = status


class UnknownOutcome(CommunityError):
    """A remote write was attempted but its result cannot be confirmed."""

    def __init__(self, detail: str):
        super().__init__(detail, status="unknown")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def lf_bytes(text: str) -> bytes:
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def bounded_text(value: Any, name: str, limit: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise CommunityError(f"{name} must be a string")
    text = value.strip()
    if required and not text:
        raise CommunityError(f"{name} must be nonempty")
    if len(text.encode("utf-8")) > limit:
        raise CommunityError(f"{name} exceeds the {limit}-byte limit")
    return text


_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_ACCOUNT_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,120}")
_SHA_RE = re.compile(r"[0-9a-f]{40}")


def check_repository(value: Any, name: str = "repository") -> str:
    if not isinstance(value, str) or not _REPO_RE.fullmatch(value):
        raise CommunityError(f"{name} must look like owner/repo")
    return value


def check_account(value: Any) -> str:
    if not isinstance(value, str) or not _ACCOUNT_RE.fullmatch(value):
        raise CommunityError("account must be a plausible GitHub login")
    return value


def check_branch(value: Any) -> str:
    if not isinstance(value, str) or not _BRANCH_RE.fullmatch(value) or ".." in value:
        raise CommunityError("branch name is not usable")
    return value


def check_sha(value: Any, name: str = "commit") -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise CommunityError(f"{name} must be a full lowercase Git SHA")
    return value


class Deadline:
    """Total transaction deadline plus an operation count, shared by one run.

    The deadline is wall-clock for the whole transaction, not per request;
    ``step()`` enforces both limits and the cooperative cancel flag before
    every single external effect.
    """

    def __init__(
        self,
        seconds: float = DEFAULT_TRANSACTION_SECONDS,
        operations: int = DEFAULT_OPERATION_LIMIT,
        *,
        cancel: Any = None,
    ):
        if not (1 <= seconds <= 3600):
            raise CommunityError("transaction deadline must be 1..3600 seconds")
        if not (1 <= operations <= 500):
            raise CommunityError("operation limit must be 1..500")
        self.started = time.monotonic()
        self.limit = self.started + seconds
        self.seconds = seconds
        self.remaining_ops = operations
        self.operations_used = 0
        self.cancel = cancel

    def step(self, name: str = "operation") -> float:
        if self.cancel is not None:
            is_set = getattr(self.cancel, "is_set", None)
            if callable(is_set) and is_set():
                raise CommunityError("cancelled by owner", status="failed")
        self.remaining_ops -= 1
        self.operations_used += 1
        if self.remaining_ops < 0:
            raise CommunityError(f"operation limit exhausted at {name}", status="failed")
        remaining = self.limit - time.monotonic()
        if remaining <= 0:
            raise CommunityError(f"transaction deadline exceeded at {name}", status="unknown")
        return remaining

    def remaining(self) -> float:
        return max(0.0, self.limit - time.monotonic())


class ProcessResult:
    def __init__(self, code: int, out: bytes, err: bytes, timed_out: bool):
        self.code = code
        self.out = out
        self.err = err
        self.timed_out = timed_out

    @property
    def out_text(self) -> str:
        return self.out.decode("utf-8", "replace")

    @property
    def err_text(self) -> str:
        return self.err.decode("utf-8", "replace")


def _reader(stream, tag: str, chunks: "queue.Queue", stop: threading.Event) -> None:
    try:
        while not stop.is_set():
            chunk = os.read(stream.fileno(), 4096)
            if not chunk:
                break
            while not stop.is_set():
                try:
                    chunks.put((tag, chunk), timeout=0.05)
                    break
                except queue.Full:
                    continue
    except OSError:
        pass
    finally:
        try:
            chunks.put((tag, None), timeout=0.2)
        except queue.Full:
            pass


def run_argv(
    argv: Sequence[str],
    *,
    timeout: float,
    max_output: int = 256 * 1024,
    input_bytes: bytes | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    cancel: Any = None,
) -> ProcessResult:
    """Run ``argv`` with owned process-tree cleanup and bounded pipes.

    Returns the raw result; callers decide which exit codes mean what.
    A timeout kills the whole tree and reports ``timed_out`` so callers can
    distinguish "refused" from "unknown outcome". A cancel observed while the
    child is in flight also kills the tree and raises ``UnknownOutcome``: a
    mid-flight outbound operation must be reconciled read-only afterwards,
    never reported as a clean refusal.
    """
    argv = [str(a) for a in argv]
    if not argv:
        raise CommunityError("empty argv")
    import tempfile

    stdin_target = None
    input_file = None
    if input_bytes is not None:
        input_file = tempfile.TemporaryFile()
        input_file.write(input_bytes)
        input_file.seek(0)
        stdin_target = input_file
    merged_env = dict(os.environ)
    if env:
        merged_env.update({str(k): str(v) for k, v in env.items()})
    try:
        if os.name == "nt":
            process = subprocess.Popen(
                argv,
                stdin=stdin_target,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(cwd) if cwd else None,
                env=merged_env,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            process = subprocess.Popen(
                argv,
                stdin=stdin_target,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(cwd) if cwd else None,
                env=merged_env,
                start_new_session=True,
            )
    except FileNotFoundError:
        if input_file:
            input_file.close()
        raise CommunityError(f"executable not found: {argv[0]}")
    except OSError as exc:
        if input_file:
            input_file.close()
        raise CommunityError(f"cannot start {argv[0]}: {exc.strerror or exc}")

    # Bounded in-flight buffer: producers block instead of outgrowing max_output.
    chunks: "queue.Queue" = queue.Queue(maxsize=max(8, max_output // 4096 + 8))
    stop = threading.Event()
    readers = [
        threading.Thread(target=_reader, args=(stream, tag, chunks, stop), daemon=True)
        for stream, tag in ((process.stdout, "out"), (process.stderr, "err"))
    ]
    for reader in readers:
        reader.start()
    out = bytearray()
    err = bytearray()
    open_streams = len(readers)
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        while open_streams:
            if cancel is not None:
                is_set = getattr(cancel, "is_set", None)
                if callable(is_set) and is_set():
                    raise UnknownOutcome(
                        "cancelled while a subprocess was in flight; reconcile read-only"
                    )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                tag, chunk = chunks.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if chunk is None:
                open_streams -= 1
                continue
            target = out if tag == "out" else err
            if len(out) + len(err) + len(chunk) > max_output:
                target.extend(chunk[: max(0, max_output - len(out) - len(err))])
                timed_out = False
                raise CommunityError("process output exceeds limit")
            target.extend(chunk)
        if timed_out:
            return ProcessResult(-1, bytes(out), bytes(err), True)
        code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        return ProcessResult(code, bytes(out), bytes(err), False)
    finally:
        stop.set()
        terminate_tree(process)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
        for reader in readers:
            reader.join(timeout=1)
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
        if input_file:
            input_file.close()
