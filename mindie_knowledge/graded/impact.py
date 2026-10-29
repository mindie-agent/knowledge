"""[Role] Static cross-component impact completeness admission.

[Invariants] A shared-contract strategy declares consumers, validation scope or
reverification commands. This checks completeness, never behavioral correctness.
[Timeouts & Locks] Bounded in-memory parsing; no subprocesses or locks.
[Side-Effect Policy] Raises a diagnostic rejection; never runs declared commands.
"""
from __future__ import annotations

import re

IMPACT_FIELDS = ("shared_contracts", "cross_component_parameters", "impact_surface",
                 "validation_scope", "reverification_commands")
SHARED_CONTRACT = re.compile(
    r"\bshared (?:underlying |runtime )?(?:contract|protocol|parameter)s?\b"
    r"|\bcross[- ]component (?:contract|protocol|parameter)s?\b"
    r"|共享(?:底层)?(?:契约|协议|参数)|跨(?:组件|部件)(?:参数|契约|协议)",
    re.IGNORECASE,
)


class ImpactCompletenessError(ValueError):
    code = "cross_component_impact_incomplete"
    missing_conditions = ("impact_surface or validation_scope or reverification_commands",)

    def __init__(self, message, unit_id=None):
        super().__init__(message)
        self.unit_id = unit_id


def check_impact_completeness(metadata, body, inputs=()):
    declared = any(metadata.get(key) for key in IMPACT_FIELDS[:2])
    referenced = SHARED_CONTRACT.search(body) or any(
        SHARED_CONTRACT.search(item.get("content", ""))
        for item in inputs
        if item.get("source_locator") == metadata.get("provenance", {}).get("source_locator")
    )
    for key in IMPACT_FIELDS:
        if key in metadata and (
                not isinstance(metadata[key], list)
                or any(not isinstance(item, str) or not item.strip() for item in metadata[key])):
            raise ImpactCompletenessError(
                f"cross_component_impact_incomplete: {key} requires a list of named consumers, contracts or commands",
                metadata.get("id"))
    if (declared or referenced) and not any(metadata.get(key) for key in IMPACT_FIELDS[2:]):
        raise ImpactCompletenessError(
            "cross_component_impact_incomplete: shared contracts or cross-component parameters "
            "require impact_surface consumers, validation_scope or reverification_commands",
            metadata.get("id"))
