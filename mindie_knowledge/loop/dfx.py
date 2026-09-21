"""Record loop failures through diagnostics; imports perform no I/O.

Dedup trusts only a marker this module stored after a validated call.
An externally supplied ``diagnostic`` dict is never treated as proof.
"""

import os


_warned = False


def _unavailable():
    global _warned
    if not _warned:
        _warned = True
        try:
            # A full redirected stderr must not delay the original failure.
            if os.name == "posix" and not os.get_blocking(2):
                os.write(2, b"mindie-knowledge: diagnostic logging unavailable\n")
        except Exception:
            pass
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
):
    """Return a validated result or a visible logging_failed reference."""
    try:
        if exception is not None:
            marker = getattr(exception, "_mindie_diagnostic", None)
            if type(marker) is _DiagnosticMarker:
                return marker.result
        from mindie_diagnostics.integration import record_failure
    except Exception:
        raw = _unavailable()
    else:
        try:
            raw = record_failure(
                component="mindie-knowledge",
                operation=operation,
                stage=stage,
                category=category,
                exception=exception,
                elapsed_ms=elapsed_ms,
                exit_code=exit_code,
                reportable=reportable,
            )
        except Exception:
            raw = _unavailable()
    try:
        result = _validated(raw)
    except Exception:
        result = None
    if result is None:
        result = _unavailable()
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
