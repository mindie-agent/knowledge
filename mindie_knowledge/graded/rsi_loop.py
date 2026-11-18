"""Thin compatibility re-export of `mindie_harness.rsi.rsi_loop` (canonical RSI loop owner).

[Role]
Re-exports `mindie_harness.rsi.rsi_loop` (`profile`, `consult`, `propose`, `measure`,
`learn`, `record`) for callers importing `mindie_knowledge.graded.rsi_loop`.

[Invariants]
- `mindie_harness.rsi.rsi_loop` is the sole implementation (`USING_HARNESS_RSI = True`).

[Timeouts & Locks]
- Owned by `mindie_harness.rsi.rsi_loop`.

[Side-Effect Policy]
- Pure module delegation; CLI execution forwards directly to `_canonical.main()`.
"""
from __future__ import annotations

from importlib import import_module as _import_module
import sys

CANONICAL_OWNER = "mindie_harness.rsi.rsi_loop"
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
