"""[Role] Compare declared applicability with observed task conditions.

[Invariants] Missing, explicitly unknown or uninterpretable facts never match.
Framework requirements are conjunctive; a hardware list describes all targets.
Plain lists name allowed alternatives; exact_set binds an entire workload group.
[Timeouts & Locks] Pure in-memory operations; no waits, locks or model calls.
[Side-Effect Policy] Returns reasons without changing either condition mapping.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

_FRAMEWORK_MAP: dict[str, str] = {
    "vllm-ascend": "vllm-ascend",
    "vllmascend": "vllm-ascend",
    "vllm-omni": "vllm-omni",
    "vllmomni": "vllm-omni",
    "xllm": "xllm",
    "xllm-service": "xllm",
    "xllmservice": "xllm",
    "mindie-sd": "mindie-sd",
    "mindiesd": "mindie-sd",
    "mindie-llm": "mindie-llm",
    "mindiellm": "mindie-llm",
}


def normalize_framework_name(name: str) -> str:
    """Normalize framework aliases (e.g. xllm-service -> xllm, mindiesd -> mindie-sd)."""
    canonical = re.sub(r"[-_.]+", "-", str(name).strip()).lower()
    return _FRAMEWORK_MAP.get(canonical, canonical)


@dataclass(frozen=True)
class Applicability:
    status: str
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"status": self.status, "reasons": list(self.reasons)}


def _unknown(value):
    return value == "unknown" if isinstance(value, str) else isinstance(value, list) and "unknown" in value


def _version(required: str, observed: Any, key: str) -> Applicability:
    try:
        spec = SpecifierSet(required)
        value = Version(str(observed))
    except (InvalidSpecifier, InvalidVersion):
        return Applicability("unknown", (f"{key}: version format cannot be compared",))
    return Applicability("matched" if value in spec else "mismatch", () if value in spec else (f"{key}: version outside {required}",))


def _framework(required: list[str], observed: Any) -> list[Applicability]:
    installed: dict[str, Any] = {}
    name_only_observed = False
    if isinstance(observed, dict):
        installed = {normalize_framework_name(name): version for name, version in observed.items()}
    elif isinstance(observed, str):
        if not observed.strip():
            return [Applicability("unknown", ("framework: supply observed name/version pairs",))]
        if "==" in observed:
            name, version = observed.split("==", 1)
            installed[normalize_framework_name(name)] = version
        else:
            installed[normalize_framework_name(observed)] = None
            name_only_observed = True
    elif isinstance(observed, list):
        if not observed:
            return [Applicability("unknown", ("framework: supply observed name/version pairs",))]
        for value in observed:
            if not isinstance(value, str) or not value.strip():
                return [Applicability("unknown", ("framework: supply observed name/version pairs",))]
            if "==" in value:
                name, version = value.split("==", 1)
                installed[normalize_framework_name(name)] = version
            else:
                installed[normalize_framework_name(value)] = None
                name_only_observed = True
    else:
        return [Applicability("unknown", ("framework: supply observed name/version pairs",))]
    outcomes = []
    for value in required:
        try:
            requirement = Requirement(value)
        except InvalidRequirement:
            outcomes.append(Applicability("unknown", (f"framework: unsupported requirement {value}",)))
            continue
        name = normalize_framework_name(requirement.name)
        if requirement.marker or requirement.url or requirement.extras:
            outcomes.append(Applicability("unknown", (f"framework.{name}: conditional requirement is not evaluated",)))
        elif name not in installed:
            if not requirement.specifier and name_only_observed:
                outcomes.append(Applicability("mismatch", (f"framework.{name}: outside declared conditions",)))
            else:
                outcomes.append(Applicability("unknown", (f"framework.{name}: version not supplied",)))
        elif requirement.specifier:
            if installed[name] is None:
                outcomes.append(Applicability("unknown", (f"framework.{name}: version not supplied",)))
            else:
                outcomes.append(_version(str(requirement.specifier), installed[name], f"framework.{name}"))
        else:
            outcomes.append(Applicability("matched"))
    return outcomes


def _exact_set(required, observed, key):
    """Match complete unordered members, preserving shape/case associations."""
    def unknown(value):
        if isinstance(value, dict):
            return any(unknown(item) for item in value.values())
        if isinstance(value, list):
            return any(unknown(item) for item in value)
        return value is None or value == "unknown"
    if not isinstance(required, list) or not required or not isinstance(observed, list):
        return Applicability("unknown", (f"{key}: complete observed list required",))
    if unknown(required) or unknown(observed):
        return Applicability("unknown", (f"{key}: a workload member is unknown",))
    try:
        encode = lambda values: [json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) for value in values]
        expected, actual = encode(required), encode(observed)
    except (TypeError, ValueError):
        return Applicability("unknown", (f"{key}: workload identity cannot be compared",))
    matches = len(expected) == len(set(expected)) and len(actual) == len(set(actual)) and set(expected) == set(actual)
    return Applicability("matched" if matches else "mismatch", () if matches else (f"{key}: complete workload group differs",))


def match_conditions(required: dict, observed: dict | None) -> Applicability:
    """Return matched only when every declared premise is established."""
    observed = observed or {}
    outcomes = []
    for key, expected in required.items():
        if _unknown(expected):
            outcomes.append(Applicability("unknown", (f"{key}: source applicability is unknown",)))
        elif expected == "any":
            outcomes.append(Applicability("matched"))
        elif key not in observed or observed[key] is None:
            outcomes.append(Applicability("unknown", (f"{key}: not supplied",)))
        elif _unknown(observed[key]):
            outcomes.append(Applicability("unknown", (f"{key}: observed value is unknown",)))
        elif key == "framework" and isinstance(expected, (list, str)):
            outcomes.extend(_framework(expected if isinstance(expected, list) else [expected], observed[key]))
        elif isinstance(expected, dict) and set(expected) == {"exact_set"}:
            outcomes.append(_exact_set(expected["exact_set"], observed[key], key))
        elif isinstance(expected, dict):
            actual = observed[key]
            if isinstance(actual, dict):
                nested = match_conditions(expected, actual)
                outcomes.append(Applicability(nested.status, tuple(f"{key}.{r}" for r in nested.reasons)))
            else:
                outcomes.append(Applicability("unknown", (f"{key}: structured conditions required",)))
        elif (isinstance(expected, str) and expected.startswith(("<", ">", "=", "!", "~"))
              and not re.fullmatch(r"<redacted:[a-z0-9-]+:[0-9a-f]{12}>", expected)):
            outcomes.append(_version(expected, observed[key], key))
        else:
            allowed = expected if isinstance(expected, list) else [expected]
            actual = observed[key] if isinstance(observed[key], list) else [observed[key]]
            matches = bool(actual) and all(value in allowed for value in actual)
            outcomes.append(Applicability("matched" if matches else "mismatch", () if matches else (f"{key}: outside declared conditions",)))
    status = "mismatch" if any(item.status == "mismatch" for item in outcomes) else "unknown" if any(item.status == "unknown" for item in outcomes) else "matched"
    return Applicability(status, tuple(reason for item in outcomes for reason in item.reasons))
