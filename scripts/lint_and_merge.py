#!/usr/bin/env python3
"""[Role] Thin executable wrapper for graded corpus lint and merge review.

[Invariants] C5 parsing and C8 closure stay in GradedUnit/RoutingIndex; the
shared maintenance module only reports duplicates and review proposals.
[Timeouts & Locks] No waits, locks, subprocesses or model calls; analysis is in memory.
[Side-Effect Policy] Read-only: never deletes, rewrites, moves or merges a unit.
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mindie_knowledge.graded.maintenance import lint_main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(lint_main())
