"""
[Role] Module entry point for the community contributor CLI.
[Invariants] Exit status is propagated unchanged; no alternate configuration or publication path exists.
[Timeouts & Locks] Delegates timeout and lock behavior to `community.cli.main`.
[Side-Effect Policy] Performs only the CLI dispatch selected by the caller; no background work.
"""

from .cli import main
from mindie_knowledge._common import configure_utf8_stdio

if __name__ == "__main__":
    configure_utf8_stdio()
    raise SystemExit(main())
