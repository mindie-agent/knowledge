"""Read the workspace's local contribution decision at publication boundaries.

The workspace owns this file. It is not a task identity, an authentication
credential, or permission to execute code. Missing/invalid decisions deny
automatic contribution while leaving local capture and shared reads available.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mindie_diagnostics import read_policy


class ContributionPaused(Exception):
    """The current contribution decision no longer authorizes this operation."""


@dataclass(frozen=True)
class Consent:
    workspace_id: str
    revision: str


def read_consent(path: str | Path) -> Consent | None:
    value = read_policy(path)
    if value is None or value["decision"] != "enabled":
        return None
    return Consent(value["workspace_id"], value["revision"])
