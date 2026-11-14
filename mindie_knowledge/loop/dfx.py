"""Record loop failures through diagnostics; imports perform no I/O.

[Role] Record loop failures through diagnostics; imports perform no I/O.
[Invariants] Deduplication trusts only a marker this module stored after a validated call; externally supplied diagnostics are not proof.
[Timeouts & Locks] Diagnostic writes are bounded and never wait on a model or network operation.
[Side-Effect Policy] Writes local diagnostic records when available and otherwise reports the logging failure explicitly.

Dedup trusts only a marker this module stored after a validated call.
An externally supplied ``diagnostic`` dict is never treated as proof.
"""

import os
from pathlib import Path


_SOURCE_ROOT = Path(__file__).resolve().parents[1]
_warned = False


def _unavailable():
    return {"recorded": False, "logging_failed": True}


class _DiagnosticMarker:
    """Private proof that this module already recorded ``exception``."""

    __slots__ = ("result",)

    def __init__(self, result):
        self.result = result


def _validated(result):
    if not isinstance(result, dict):
        return None
    recorded = result.get("recorded")
    incident_id = result.get("incident_id")
    logging_failed = result.get("logging_failed")
    if recorded is not True or type(logging_failed) is not bool:
        return None
    if (
        type(incident_id) is not str
        or len(incident_id) != 32
        or any(c not in "0123456789abcdef" for c in incident_id)
    ):
        return None
    return {
        "recorded": recorded,
        "incident_id": incident_id,
        "logging_failed": logging_failed,
    }


def attach_reference(exception, value):
    """Reuse only a reference from the authenticated same-component service."""
    if not isinstance(value, dict) or type(value.get("logging_failed")) is not bool:
        return
    raw = dict(value, recorded=True)
    checked = _validated(raw)
    if checked is None:
        if value != {"logging_failed": True}:
            return
        checked = {"recorded": False, "logging_failed": True}
    reference = {k: v for k, v in checked.items() if k != "recorded"}
    exception._mindie_diagnostic = _DiagnosticMarker(checked)
    exception.mindie_diagnostic = reference


def failure(
    operation,
    *,
    stage,
    category,
    exception=None,
    elapsed_ms=None,
    exit_code=None,
    reportable=True,
    reason_code=None,
    substage=None,
    contract_name=None,
):
    """Return a validated result or a visible logging_failed reference."""
    try:
        if exception is not None:
            seen = set()
            pending = [exception]
            while pending and len(seen) < 32:
                cur = pending.pop(0)
                if cur is None or id(cur) in seen:
                    continue
                seen.add(id(cur))
                marker = getattr(cur, "_mindie_diagnostic", None)
                if type(marker) is _DiagnosticMarker:
                    try:
                        exception._mindie_diagnostic = marker
                        exception.mindie_diagnostic = {
                            **({"incident_id": marker.result["incident_id"]} if "incident_id" in marker.result else {}),
                            "logging_failed": marker.result["logging_failed"],
                        }
                    except Exception:
                        pass
                    return marker.result
                if getattr(cur, "__cause__", None) is not None:
                    pending.append(cur.__cause__)
                if getattr(cur, "__context__", None) is not None and not getattr(cur, "__suppress_context__", False):
                    pending.append(cur.__context__)
        from mindie_diagnostics.integration import record_failure
    except Exception:
        raw = _unavailable()
    else:
        base_kwargs = {
            "component": "mindie-knowledge",
            "operation": operation,
            "stage": stage,
            "category": category,
            "exception": exception,
            "elapsed_ms": elapsed_ms,
            "exit_code": exit_code,
            "reportable": reportable,
        }
        v2_kwargs = {"source_root": _SOURCE_ROOT}
        if reason_code is not None:
            v2_kwargs["reason_code"] = reason_code
        if substage is not None:
            v2_kwargs["substage"] = substage
        if contract_name is not None:
            v2_kwargs["contract_name"] = contract_name
        try:
            try:
                raw = record_failure(**base_kwargs, **v2_kwargs)
            except TypeError:
                raw = record_failure(**base_kwargs)
        except Exception:
            raw = _unavailable()
    try:
        result = _validated(raw)
    except Exception:
        result = None
    if result is None:
        result = _unavailable()
    try:
        from .agent_diagnostics import enqueue
        enqueue('mindie-knowledge', operation, stage, category, result)
    except Exception:
        result['delivery_failed'] = True
    if exception is None:
        return result
    try:
        exception._mindie_diagnostic = _DiagnosticMarker(result)
        exception.mindie_diagnostic = {
            **({"incident_id": result["incident_id"]} if "incident_id" in result else {}),
            "logging_failed": result["logging_failed"],
        }
    except Exception:
        return result
    return result


def record_contract_violation(
    operation,
    *,
    stage="contract_check",
    contract_name,
    reason_code,
    exception=None,
):
    """Record an authoritative local state or schema contract violation."""
    return failure(
        operation,
        stage=stage,
        category="contract_violation",
        exception=exception,
        reason_code=reason_code,
        contract_name=contract_name,
    )
