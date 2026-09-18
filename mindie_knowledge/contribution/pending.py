"""Recoverable pending-submit records.

This is a small digest-keyed store, not a generic task queue or scheduler.
Retrying the same public content reuses the same record (and later the same
branch/PR). Offline or auth failure leaves ``awaiting_transport``.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from mindie_knowledge.contribution.documents import digest_token
from mindie_knowledge.contribution.errors import IdentityError

SCHEMA = "mindie-knowledge-contribution-pending/v1"
PENDING_STATUSES = (
    "pending",
    "awaiting_transport",
    "blocked_redaction",
    "submitted",
    "pr_open",
    "merged",
    "closed",
)

STATUS_PENDING = "pending"
STATUS_AWAITING = "awaiting_transport"
STATUS_BLOCKED = "blocked_redaction"
STATUS_SUBMITTED = "submitted"
STATUS_PR_OPEN = "pr_open"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def pending_dir(state_root: Path) -> Path:
    return Path(state_root) / "contribution" / "pending"


def pending_path(state_root: Path, digest: str) -> Path:
    return pending_dir(state_root) / f"{digest_token(digest)}.json"


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@dataclass
class PendingRecord:
    content_digest: str
    title: str
    public_relpath: str
    status: str = STATUS_PENDING
    branch: str = ""
    candidate_relpath: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    head_sha: str | None = None
    last_error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    notes: list[str] = field(default_factory=list)
    consent_workspace_id: str | None = None
    consent_revision: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema"] = SCHEMA
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PendingRecord":
        if payload.get("schema") != SCHEMA:
            raise IdentityError("unknown pending record schema")
        digest = str(payload.get("content_digest") or "")
        digest_token(digest)
        status = str(payload.get("status") or STATUS_PENDING)
        if status not in PENDING_STATUSES:
            raise IdentityError("unknown pending status")
        pr_number = payload.get("pr_number")
        if pr_number is not None and (not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0):
            raise IdentityError("malformed pr_number")
        return cls(
            content_digest=digest,
            title=str(payload.get("title") or ""),
            public_relpath=str(payload.get("public_relpath") or ""),
            status=status,
            branch=str(payload.get("branch") or ""),
            candidate_relpath=payload.get("candidate_relpath") if isinstance(payload.get("candidate_relpath"), str) else None,
            pr_number=pr_number,
            pr_url=payload.get("pr_url") if isinstance(payload.get("pr_url"), str) else None,
            head_sha=payload.get("head_sha") if isinstance(payload.get("head_sha"), str) else None,
            last_error=payload.get("last_error") if isinstance(payload.get("last_error"), str) else None,
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            notes=list(payload.get("notes") or []),
            consent_workspace_id=payload.get("consent_workspace_id"),
            consent_revision=payload.get("consent_revision"),
        )


def save_pending(state_root: Path, record: PendingRecord) -> PendingRecord:
    record.updated_at = utc_now()
    if not record.created_at:
        record.created_at = record.updated_at
    _atomic_write_json(pending_path(state_root, record.content_digest), record.to_dict())
    return record


def load_pending(state_root: Path, digest: str) -> PendingRecord | None:
    path = pending_path(state_root, digest)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    return PendingRecord.from_dict(payload)


def iter_pending(state_root: Path) -> list[PendingRecord]:
    root = pending_dir(state_root)
    if not root.is_dir():
        return []
    records: list[PendingRecord] = []
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        try:
            records.append(PendingRecord.from_dict(payload))
        except (IdentityError, KeyError, TypeError, ValueError):
            continue
    return records
