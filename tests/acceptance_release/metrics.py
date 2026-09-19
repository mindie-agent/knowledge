"""Task quality and cost metrics. No preset improvement percentage."""

from __future__ import annotations

from typing import Any


UNKNOWN = "unknown"

QUALITY_FIELDS = (
    "task_completed",
    "original_artifact_ok",
    "repeat_investigations",
    "repeat_experiments",
    "human_corrections",
    "wrong_method_triggered",
    "knowledge_used",
    "knowledge_contributed",
)

COST_FIELDS = (
    "foreground_input_tokens",
    "foreground_output_tokens",
    "foreground_cached_input_tokens",
    "foreground_wall_seconds",
    "foreground_calls",
    "background_input_tokens",
    "background_output_tokens",
    "background_cached_input_tokens",
    "background_wall_seconds",
    "background_calls",
)


def _num(record: dict[str, Any], *path: str) -> int | float | None:
    cursor: Any = record
    for key in path:
        if not isinstance(cursor, dict) or key not in cursor:
            return None
        cursor = cursor[key]
    if isinstance(cursor, bool) or cursor is None:
        return None
    if isinstance(cursor, (int, float)):
        return cursor
    return None


def metrics_from_record(record: dict[str, Any] | None) -> dict[str, Any]:
    """Missing measurements stay unknown. Cached input is recorded separately."""

    record = record or {}
    quality = {name: record.get("quality", {}).get(name, UNKNOWN) if isinstance(record.get("quality"), dict) else UNKNOWN for name in QUALITY_FIELDS}
    cost = {
        "foreground_input_tokens": _num(record, "foreground", "input_tokens"),
        "foreground_output_tokens": _num(record, "foreground", "output_tokens"),
        "foreground_cached_input_tokens": _num(record, "foreground", "cached_input_tokens"),
        "foreground_wall_seconds": _num(record, "foreground", "wall_seconds"),
        "foreground_calls": _num(record, "foreground", "calls"),
        "background_input_tokens": _num(record, "background", "input_tokens"),
        "background_output_tokens": _num(record, "background", "output_tokens"),
        "background_cached_input_tokens": _num(record, "background", "cached_input_tokens"),
        "background_wall_seconds": _num(record, "background", "wall_seconds"),
        "background_calls": _num(record, "background", "calls"),
    }
    for key, value in list(cost.items()):
        if value is None:
            cost[key] = UNKNOWN
    # Cached tokens are observed, not billed as new, and not ignored.
    billed_in = cost["foreground_input_tokens"]
    cached = cost["foreground_cached_input_tokens"]
    if isinstance(billed_in, (int, float)) and isinstance(cached, (int, float)):
        cost["foreground_new_input_tokens"] = billed_in  # record already splits cached
    else:
        cost["foreground_new_input_tokens"] = UNKNOWN
    return {
        "quality": quality,
        "cost": cost,
        "preset_optimization_percent": None,
        "note": "No improvement percentage is predefined. Unknown remains unknown.",
    }


def summarize_run(cases: list[dict[str, Any]]) -> dict[str, Any]:
    phases = {"planned": 0, "started": 0, "completed": 0, "valid": 0, "invalid": 0, "unknown": 0, "not_executed": 0}
    for item in cases:
        phase = item.get("phase") or "planned"
        phases[phase] = phases.get(phase, 0) + 1
    return {
        "phases": phases,
        "denominator_includes_timeout_discard_no_output": True,
        "helpful_is_not_effect_success": True,
        "db_score_is_not_effect_success": True,
    }
