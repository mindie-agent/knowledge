"""[Role] Expose the condition-bound graded unit type.
[Invariants] Importing a unit never activates capture or publication.
[Timeouts & Locks] No waits or locks.
[Side-Effect Policy] Imports only; no persisted state changes.
"""

from .units import GradedUnit

__all__ = ["GradedUnit"]
