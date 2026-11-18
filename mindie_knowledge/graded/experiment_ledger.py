"""Thin compatibility re-export of `mindie_harness.rsi.experiment_ledger` (canonical RSI ledger owner).

[Role]
Re-exports `mindie_harness.rsi.experiment_ledger` (source fingerprinting, paired-trial
validation, 4-gate RSI judgment, and `PROGRESS.md` rendering) for `mindie_knowledge.graded` callers.

[Invariants]
- `mindie_harness.rsi.experiment_ledger` is the sole implementation (`USING_HARNESS_RSI = True`).

[Timeouts & Locks]
- Owned by `mindie_harness.rsi.experiment_ledger`.

[Side-Effect Policy]
- Pure module delegation; CLI execution forwards directly to `_canonical.main()`.
"""
from __future__ import annotations

from importlib import import_module as _import_module
import sys

CANONICAL_OWNER = "mindie_harness.rsi.experiment_ledger"
USING_HARNESS_RSI = True
_canonical = _import_module(CANONICAL_OWNER)
setattr(_canonical, "CANONICAL_OWNER", CANONICAL_OWNER)
setattr(_canonical, "USING_HARNESS_RSI", True)

if __name__ == "__main__":
    raise SystemExit(_canonical.main())

globals().update(
    {
        k: v
        for k, v in _canonical.__dict__.items()
        if k not in ("__name__", "__file__", "__spec__", "__loader__", "__package__")
    }
)
sys.modules[__name__] = _canonical
