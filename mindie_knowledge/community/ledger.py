"""Durable publication ledger (SQLite), written before any effect.

The publication ledger is the idempotence backbone: an intent row plus
append-only step receipts exist before the first Git mutation, so a restart
can distinguish "never attempted" from "attempted, outcome unknown" and never
replays a failed or unknown revision of the same candidate digest.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from .common import MAX_DETAIL, canonical

LEDGER_NAME = "community-ledger.sqlite3"

PUBLISH_FINAL = ("submitted", "updated", "unchanged", "failed", "rejected",
                 "needs_review", "disabled")
PUBLISH_UNRESOLVED = ("intent", "unknown")


class Ledger:
    def __init__(self, state_dir: Path, *, authority_guard=None):
        root = Path(state_dir)
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError:
            pass
        self.lock = threading.RLock()
        self._owner_authority = authority_guard
        from ..owned_state import open_database
        self.db = open_database(
            root / LEDGER_NAME, schema='mindie-community-ledger/1',
            required={
                'publication': 'batch_id revision domain repository branch pr_number pr_url head_sha status detail created updated actual_files'.split(),
                'publication_step': 'id batch_id revision step detail at'.split(),
                'state': 'key value'.split(),
            },
            initialize=self._initialize, residue=(root / 'git',),
        )

    @staticmethod
    def _initialize(db):
        db.executescript("""
            CREATE TABLE publication(
                batch_id TEXT NOT NULL, revision TEXT NOT NULL, domain TEXT NOT NULL,
                repository TEXT NOT NULL, branch TEXT NOT NULL, pr_number INTEGER,
                pr_url TEXT, head_sha TEXT, status TEXT NOT NULL, detail TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, actual_files TEXT,
                PRIMARY KEY(batch_id, revision));
            CREATE TABLE publication_step(
                id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL,
                revision TEXT NOT NULL, step TEXT NOT NULL, detail TEXT NOT NULL,
                at REAL NOT NULL);
            CREATE TABLE state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def assert_authority(self):
        """Both the publication intent and its runtime source must still exist."""
        from .common import TransientError
        try:
            self.db.assert_authority()
            if self._owner_authority is not None:
                self._owner_authority()
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise TransientError('publication authority is unavailable before the next remote write') from exc

    # ------------------------------------------------------------------ #
    # Publication intents and receipts
    # ------------------------------------------------------------------ #

    def get_publication(self, batch_id: str, revision: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM publication WHERE batch_id=? AND revision=?",
                (batch_id, revision),
            ).fetchone()
        return dict(row) if row else None

    def find_by_revision(self, revision: str) -> dict[str, Any] | None:
        """A new event id alone is not new content: the digest is the identity."""
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM publication WHERE revision=? ORDER BY created DESC LIMIT 1",
                (revision,),
            ).fetchone()
        return dict(row) if row else None

    def unresolved_for_batch(self, batch_id: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM publication WHERE batch_id=? AND status IN ('intent','unknown') ORDER BY created",
                (batch_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def latest_for_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM publication WHERE batch_id=? ORDER BY created DESC LIMIT 1",
                (batch_id,),
            ).fetchone()
        return dict(row) if row else None

    def all_for_batch(self, batch_id: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM publication WHERE batch_id=? ORDER BY created",
                (batch_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_intent(
        self,
        *,
        batch_id: str,
        revision: str,
        domain: str,
        repository: str,
        branch: str,
    ) -> None:
        """Persist the publication intent BEFORE any remote write."""
        now = time.time()
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "INSERT OR IGNORE INTO publication"
                "(batch_id, revision, domain, repository, branch, status, detail, created, updated)"
                " VALUES(?,?,?,?,?,'intent','',?,?)",
                (batch_id, revision, domain, repository, branch, now, now),
            )

    def record_step(self, batch_id: str, revision: str, step: str, detail: str = "") -> None:
        """Append a step receipt BEFORE (or immediately after) the named effect."""
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO publication_step(batch_id, revision, step, detail, at)"
                " VALUES(?,?,?,?,?)",
                (batch_id, revision, step, detail[:MAX_DETAIL], time.time()),
            )

    def record_actual_files(
        self, batch_id: str, revision: str, files: list[dict[str, Any]]
    ) -> None:
        """Persist the actual per-file committed identities (path/sha256/
        revision) BEFORE the uncertain external step they belong to.

        Small identities only — never bodies — so a lost response can still
        recover exactly what the worktree held at commit time, without a
        second body store."""
        packed = canonical([
            {k: f.get(k) for k in ("path", "sha256", "revision")} for f in files
        ])
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "UPDATE publication SET actual_files=? WHERE batch_id=? AND revision=?",
                (packed, batch_id, revision),
            )

    @staticmethod
    def parse_actual_files(row: Mapping[str, Any]) -> list[dict[str, Any]] | None:
        raw = row.get("actual_files")
        if raw is None:
            return None  # No files were prepared before this intent.
        try:
            value = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError("publication file receipt is corrupt; external outcome must be reconciled") from exc
        if not isinstance(value, list) or any(
                not isinstance(item, dict) or not isinstance(item.get('path'), str)
                or not ((item.get('sha256') is None and item.get('revision') is None)
                        or (isinstance(item.get('sha256'), str) and len(item['sha256']) == 64
                            and isinstance(item.get('revision'), str) and len(item['revision']) == 64))
                for item in value):
            raise ValueError("publication file receipt is invalid; external outcome must be reconciled")
        return value

    def steps_for(self, batch_id: str, revision: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM publication_step WHERE batch_id=? AND revision=? ORDER BY id",
                (batch_id, revision),
            ).fetchall()
        return [dict(r) for r in rows]

    def finish_publication(
        self,
        batch_id: str,
        revision: str,
        *,
        status: str,
        detail: str = "",
        pr_number: int | None = None,
        pr_url: str | None = None,
        head_sha: str | None = None,
        branch: str | None = None,
    ) -> None:
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            updates = {"status": status, "detail": detail[:MAX_DETAIL], "updated": time.time()}
            if pr_number is not None:
                updates["pr_number"] = pr_number
            if pr_url is not None:
                updates["pr_url"] = pr_url
            if head_sha is not None:
                updates["head_sha"] = head_sha
            if branch is not None:
                updates["branch"] = branch
            assignments = ", ".join(f"{key}=?" for key in updates)
            self.db.execute(
                f"UPDATE publication SET {assignments} WHERE batch_id=? AND revision=?",
                (*updates.values(), batch_id, revision),
            )

    def receipt(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "status": row["status"],
            "batch_id": row["batch_id"],
            "revision": row["revision"],
            "pr_url": row.get("pr_url"),
            "head_sha": row.get("head_sha"),
            "detail": row.get("detail") or "",
            "files": self.parse_actual_files(row),
        }
