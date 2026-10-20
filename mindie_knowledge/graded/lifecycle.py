"""[Role] Author explicit graded-unit lifecycle transitions without rewriting evidence.

[Invariants] A unit moves active -> suspect -> superseded; active may also be
archived. A suspect or superseded unit is never ready to apply, but its body,
conditions, provenance and prior revision remain auditable. A status change is
semantic: it advances the integer revision and never silently deletes evidence.
[Timeouts & Locks] Pure in-memory metadata transformation; no I/O, waits, locks
or model calls.
[Side-Effect Policy] Returns a new immutable GradedUnit; the caller owns the
reviewed-change write and publication gates.
"""
from __future__ import annotations

from copy import deepcopy

from .units import STATUSES, GradedUnit

TRANSITIONS = {
    ("active", "suspect"),
    ("suspect", "active"),
    ("suspect", "superseded"),
    ("active", "archived"),
    ("suspect", "archived"),
    ("archived", "active"),
}


def transition_unit(unit: GradedUnit, target_status: str, *,
                    replacement: str | None = None, reason: str = "") -> GradedUnit:
    """Return the next revision after one reviewed lifecycle transition.

    Direct active -> superseded is rejected: a refutation first makes the unit
    suspect, preserving a visible review state before a replacement is named.
    """
    if not isinstance(unit, GradedUnit):
        raise ValueError("lifecycle transitions require a GradedUnit")
    if target_status not in STATUSES:
        raise ValueError(f"target_status must be one of {sorted(STATUSES)}")
    if (unit.status, target_status) not in TRANSITIONS:
        raise ValueError(f"illegal lifecycle transition: {unit.status} -> {target_status}")
    if target_status == "superseded":
        from .units import UNIT_ID
        if (not isinstance(replacement, str) or UNIT_ID.fullmatch(replacement) is None
                or replacement == unit.id):
            raise ValueError("superseded requires another stable unit ID as replacement")
    elif replacement is not None:
        raise ValueError("replacement is only valid when superseding a unit")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason must be text")
    reason = (reason or "").strip()
    metadata = deepcopy(unit.metadata)
    metadata["status"] = target_status
    metadata["revision"] = unit.revision + 1
    if target_status == "superseded":
        metadata["superseded_by"] = replacement
    else:
        metadata.pop("superseded_by", None)
    if reason:
        metadata["status_reason"] = reason
    else:
        metadata.pop("status_reason", None)
    return GradedUnit.from_parts(metadata, unit.body, unit.path)


def suspect_unit(unit: GradedUnit, *, reason: str) -> GradedUnit:
    """Demote an active unit after valid contrary evidence; keep it auditable."""
    return transition_unit(unit, "suspect", reason=reason)


def supersede_unit(unit: GradedUnit, replacement: str, *, reason: str) -> GradedUnit:
    """Replace a suspect unit after a reviewed successor exists."""
    return transition_unit(unit, "superseded", replacement=replacement, reason=reason)


def restore_unit(unit: GradedUnit, *, reason: str) -> GradedUnit:
    """Recover a suspect or archived unit when the doubt was itself resolved."""
    return transition_unit(unit, "active", reason=reason)


__all__ = ["TRANSITIONS", "restore_unit", "supersede_unit", "suspect_unit", "transition_unit"]
