"""Active shared-version join point.

distribution.current_shared(state_root) will return None or
{source_git_sha, root_uri, manifest_path}. This module reads that result when
the distribution package is present, otherwise a local current.json. Search
uses the local bootstrap and the active release as separate roots. It never
searches their shared parent, which can also contain inactive releases.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from mindie_knowledge.markdown import SHARED_BOOTSTRAP_URI, URI_ROOT

DEFAULT_SHARED_URI = f"{URI_ROOT}/shared"


def current_shared(state_root: Path | None) -> dict[str, Any] | None:
    if state_root is None:
        return None
    root = Path(state_root)
    imported = None
    try:
        from mindie_knowledge.distribution import current_shared as imported_current

        imported = imported_current
    except Exception:  # noqa: BLE001 - distribution is a later batch
        imported = None
    if imported is not None:
        try:
            payload = imported(root)
        except Exception:  # noqa: BLE001
            payload = None
        if isinstance(payload, dict) and payload.get("root_uri"):
            return {
                "source_git_sha": payload.get("source_git_sha"),
                "root_uri": payload.get("root_uri"),
                "manifest_path": payload.get("manifest_path"),
                "prepared_root": payload.get("prepared_root"),
                "references_sha256": payload.get("references_sha256"),
                "metadata_status": payload.get("metadata_status"),
                "prepared_manifest_sha256": payload.get("prepared_manifest_sha256"),
            }
    path = root / "shared" / "current.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("root_uri"):
        return None
    return {
        "source_git_sha": data.get("source_git_sha"),
        "root_uri": data.get("root_uri"),
        "manifest_path": data.get("manifest_path"),
        "prepared_root": data.get("prepared_root"),
        "references_sha256": data.get("references_sha256"),
        "metadata_status": data.get("metadata_status"),
        "prepared_manifest_sha256": data.get("prepared_manifest_sha256"),
    }


def shared_search_uri(state_root: Path | None) -> str:
    """The active release root, or bootstrap when no safe release is active."""

    return shared_search_uris(state_root)[-1]


def shared_search_uris(state_root: Path | None) -> tuple[str, ...]:
    """Return non-overlapping bootstrap and active-release search roots."""

    roots = [SHARED_BOOTSTRAP_URI]
    current = current_shared(state_root)
    active = str(current.get("root_uri") or "").rstrip("/") if current else ""
    # Releases are direct children, or one exact repair generation. Reject
    # broad parents rather than retrieving inactive versions or bootstrap a
    # second time. The repair layout is owned by distribution.sync.
    prefix = DEFAULT_SHARED_URI + "/"
    version = active[len(prefix):] if active.startswith(prefix) else ""
    direct = version and version not in {".", "..", "bootstrap", "repairs"} and "/" not in version
    repair = re.fullmatch(r"repairs/[0-9a-f]{16}/v[0-9a-f]{12}", version)
    if direct or repair:
        roots.append(active)
    return tuple(roots)
