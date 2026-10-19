"""[Role] Validate C5 files inside the existing contribution envelope.

[Invariants] A contribution binds exact unit bytes and observed semantic refs.
Contract/1 and contract/2 payloads never mix. The publisher validates the full
projected inheritance graph before any Git write; a partial batch alone cannot.
[Timeouts & Locks] Pure parsing; no waits or locks.
[Side-Effect Policy] No filesystem, model or external effects.
"""
from __future__ import annotations

import json

from ..community.common import CommunityError
from .units import GradedUnit, is_graded_unit_path as is_unit_path

DERIVED_PATHS = {"index/routing-manifest.json", "index/shard-topology.json"}



def parse_unit(text, path):
    if not is_unit_path(path):
        raise CommunityError("graded unit has an unsafe or noncanonical path")
    try:
        unit = GradedUnit.from_text(text, path)
    except (TypeError, ValueError) as exc:
        raise CommunityError(f"invalid graded unit {path}: {exc}") from exc
    if path != f"{unit.metadata['domain_path']}/{unit.id}.md":
        raise CommunityError("graded unit path differs from its declared domain_path and ID")
    return unit


def feedback_api(text):
    try:
        schema = json.loads(text).get("schema")
    except (TypeError, ValueError, AttributeError) as exc:
        raise CommunityError("feedback file must be a JSON object") from exc
    if schema == "mindie-graded-feedback/1":
        from . import feedback
        return feedback
    from ..community import batch
    return batch


def validate_files(files, refs, domain):
    units = {}
    formats = set()
    for item in files:
        path = item["path"]
        if path.startswith("tasks/"):
            formats.add("material-package")
        elif path in DERIVED_PATHS:
            formats.add("graded")
            if item.get("delete"):
                raise CommunityError("derived routing metadata cannot be deleted by a contribution")
            try:
                payload = json.loads(item["content"])
            except ValueError as exc:
                raise CommunityError("derived routing metadata must be JSON") from exc
            expected = "mindie-routing-manifest/1" if "routing" in path else "mindie-shard-topology/1"
            if not isinstance(payload, dict) or payload.get("schema_version", payload.get("schema")) != expected:
                raise CommunityError("derived routing metadata schema mismatch")
        elif path.startswith("feedback/"):
            api = feedback_api(item["content"])
            checked = api.validate_feedback(item["content"], path)
            graded = checked["schema"] == "mindie-graded-feedback/1"
            formats.add("graded" if graded else "material-package")
            if graded:
                for vote in checked["votes"]:
                    ref = f"mindie://{domain}/units/{vote['unit_id']}@{vote['content_digest'][7:]}"
                    if ref not in refs:
                        raise CommunityError("graded feedback requires its exact observed revision reference")
        else:
            formats.add("graded")
            if item.get("delete"):
                continue
            unit = parse_unit(item["content"], path)
            if unit.id in units:
                raise CommunityError("graded contribution repeats a stable unit ID")
            units[unit.id] = unit
            if f"mindie://{domain}/units/{unit.id}@{unit.content_sha256}" not in refs:
                raise CommunityError("graded unit requires its exact content revision reference")
    if len(formats) != 1:
        raise CommunityError("one contribution must use exactly one publication data format")
    return formats.pop(), units


def scan_unit(text, path):
    from .publication import PrivacyRejected, _scan_proposed
    try:
        _scan_proposed([parse_unit(text, path)], "")
    except PrivacyRejected as exc:
        raise CommunityError(str(exc)) from exc
