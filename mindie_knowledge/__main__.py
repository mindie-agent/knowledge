"""``python -m mindie_knowledge`` is the same as the ``mindie-knowledge`` console script.

[Role] Module entry point equivalent to the `mindie-knowledge` console script.
[Invariants] Exit status is propagated unchanged from the CLI; no alternate command path is introduced.
[Timeouts & Locks] Delegates all timeout and lock behavior to `loop.cli.main`.
[Side-Effect Policy] Performs only the CLI dispatch selected by the caller; no background work.
"""

from mindie_knowledge.loop.cli import main

raise SystemExit(main())
