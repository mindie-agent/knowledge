#!/usr/bin/env python3
"""[Role] Apply a complete authored and reviewed C5 change through the runtime.
[Invariants] No automatic semantic acceptance; exact base generation is required.
[Timeouts & Locks] Local file operations only; the reviewer owns the checkout.
[Side-Effect Policy] Changes the selected units and index only when --apply is set.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mindie_knowledge.graded.reviewed_change import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
