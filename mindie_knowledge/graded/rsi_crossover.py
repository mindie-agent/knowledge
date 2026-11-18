"""Thin compatibility re-export of `mindie_harness.rsi.rsi_crossover` (canonical RSI lineage owner).

[Role]
Re-exports `mindie_harness.rsi.rsi_crossover` (crossover DAG lineage, orthogonal sibling
discovery, and Leave-One-Out ablation reuse) for `mindie_knowledge.graded.rsi_crossover` callers.

[Invariants]
- `mindie_harness.rsi.rsi_crossover` is the sole implementation (`USING_HARNESS_RSI = True`).

[Timeouts & Locks]
- Pure in-memory helpers owned by `mindie_harness.rsi.rsi_crossover`.

[Side-Effect Policy]
- Pure module delegation; zero disk or process side effects.
"""
from __future__ import annotations

from importlib import import_module as _import_module
import sys

CANONICAL_OWNER = "mindie_harness.rsi.rsi_crossover"
USING_HARNESS_RSI = True
_canonical = _import_module(CANONICAL_OWNER)
setattr(_canonical, "CANONICAL_OWNER", CANONICAL_OWNER)
setattr(_canonical, "USING_HARNESS_RSI", True)

globals().update(
    {
        k: v
        for k, v in _canonical.__dict__.items()
        if k not in ("__name__", "__file__", "__spec__", "__loader__", "__package__")
    }
)
sys.modules[__name__] = _canonical
