"""[Role] Validate the full C5 Git projection inside the existing publisher.

[Invariants] Current remote bytes are authoritative. Stable IDs cannot split
across paths, withdrawn ancestors cannot be retained implicitly, and content
changes advance the declared integer revision. Derived indexes match the same
projected tree. This helper does not create another publication state machine.
[Timeouts & Locks] Uses the caller's existing publication Deadline.
[Side-Effect Policy] Reads immutable Git blobs into an ephemeral private staging
folder. No commit, push, API write or model call occurs here.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile

from ..community.common import CommunityError, TransientError
from .contribution import DERIVED_PATHS, feedback_api, is_unit_path, parse_unit
from .feed import load_snapshot, validate_derived
from .index import RoutingIndex
from .feedback import validate_bindings


def validate_projection(checked, repo, revision, deadline, env):
    try:
        with tempfile.TemporaryDirectory(prefix="mindie-graded-projection-") as temporary:
            snapshot = load_snapshot(repo, revision, staging=Path(temporary),
                                     deadline=deadline.limit, env=env, cancel=deadline.cancel)
        before = {unit.id: unit for unit in snapshot.units}
        current = {unit.path: unit for unit in snapshot.units}
        manifest, topology = snapshot.manifest, snapshot.topology
        for item in checked["files"]:
            path = item["path"]
            if is_unit_path(path):
                if item.get("delete"):
                    current.pop(path, None)
                    continue
                unit = parse_unit(item["content"], path)
                old = before.get(unit.id)
                if old:
                    if old.content_sha256 == unit.content_sha256 and old.revision != unit.revision:
                        raise ValueError("unchanged graded content must retain its observed revision")
                    if old.content_sha256 != unit.content_sha256 and unit.revision <= old.revision:
                        raise ValueError("changed graded content must advance its integer revision")
                current[path] = unit
            elif path in DERIVED_PATHS:
                if path.endswith("routing-manifest.json"):
                    manifest = json.loads(item["content"])
                else:
                    topology = json.loads(item["content"])
        index = RoutingIndex(current.values())
        validate_derived(manifest, topology, index.units, index)
        votes = list(snapshot.feedback)
        for item in checked["files"]:
            if item["path"].startswith("feedback/"):
                incoming_votes = feedback_api(item["content"]).validate_feedback(item["content"], item["path"])["votes"]
                if any(vote["unit_id"] not in index.units for vote in incoming_votes):
                    raise ValueError("feedback target was withdrawn from the current public collection")
                votes.extend(incoming_votes)
        validate_bindings(votes, index.units)
    except (OSError, TimeoutError) as exc:
        raise TransientError(f"cannot read graded publication base: {exc}") from exc
    except ValueError as exc:
        raise CommunityError(f"graded publication projection rejected: {exc}", status="needs_review") from exc
