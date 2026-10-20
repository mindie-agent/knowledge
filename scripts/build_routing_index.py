#!/usr/bin/env python3
"""[Role] Thin executable wrapper for the graded routing-index build/check.

[Invariants] All validation and atomic writes live in the shared maintenance
module; this file only resolves the repository import root and exits with its code.
[Timeouts & Locks] No waits or locks; maintenance uses only in-memory validation and atomic replacement.
[Side-Effect Policy] Writes only when maintenance.build_routing_index is not in
--check mode and the whole corpus is valid.
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mindie_knowledge.graded.maintenance import build_routing_index_main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(build_routing_index_main())
