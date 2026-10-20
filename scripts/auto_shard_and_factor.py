#!/usr/bin/env python3
"""[Role] Thin executable wrapper for shard/factor dry-run planning.

[Invariants] Planning never splits a leaf, rewrites a rule or writes another
repository. Only an explicit reviewed --apply-plan may move whole files.
[Timeouts & Locks] No waits or locks; analysis is in memory and moves use atomic replacement.
[Side-Effect Policy] Read-only unless --apply-plan is supplied; that path is
delegated to the prevalidated whole-file move implementation.
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mindie_knowledge.graded.maintenance import auto_shard_main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(auto_shard_main())
