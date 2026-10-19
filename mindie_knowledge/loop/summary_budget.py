"""[Role] Share a summary worker's remaining time with its native model call.

[Invariants] A native call ends before its enclosing worker deadline so its
terminal receipt can be emitted without the parent interrupting cleanup.
[Timeouts & Locks] 315s worker default, 300s native maximum, 5s receipt/cleanup
reserve. The absolute monotonic deadline includes worker startup and setup.
[Side-Effect Policy] Reads process-local timing configuration; no I/O or retries.
"""
from __future__ import annotations

import math
import os
import time

DEADLINE_ENV = "MINDIE_SUMMARY_WORKER_DEADLINE"
WORKER_SECONDS = 315.0
CLEANUP_SECONDS = 5.0
IDENTITY_SECONDS = 10.0


def worker_timeout():
    seconds = float(os.environ.get("MINDIE_SUMMARY_TIMEOUT_SECONDS", WORKER_SECONDS))
    if not math.isfinite(seconds) or seconds <= CLEANUP_SECONDS:
        raise ValueError("summary worker timeout must exceed its 5s cleanup reserve")
    return seconds


def native_timeout(max_seconds=300.0):
    deadline = os.environ.get(DEADLINE_ENV)
    remaining = (float(deadline) - time.monotonic()
                 if deadline is not None else worker_timeout())
    budget = min(max_seconds, remaining - CLEANUP_SECONDS)
    if not math.isfinite(budget) or budget <= 0:
        raise TimeoutError("summary worker budget expired before native invocation")
    return budget
