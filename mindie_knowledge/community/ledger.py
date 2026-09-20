"""Durable publication/review ledgers (SQLite), written before any effect.

The publication ledger is the idempotence backbone: an intent row plus
append-only step receipts exist before the first Git mutation, so a restart
can distinguish "never attempted" from "attempted, outcome unknown" and never
replays a failed or unknown revision of the same candidate digest.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from .common import MAX_DETAIL, canonical

LEDGER_NAME = "community-ledger.sqlite3"

PUBLISH_FINAL = ("submitted", "updated", "unchanged", "failed", "needs_review", "disabled")
PUBLISH_UNRESOLVED = ("intent", "unknown")


class Ledger:
    def __init__(self, state_dir: Path):
        root = Path(state_dir)
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError:
            pass
        self.lock = threading.RLock()
        self.db = sqlite3.connect(root / LEDGER_NAME, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS publication(
                batch_id TEXT NOT NULL,
                revision TEXT NOT NULL,
                domain TEXT NOT NULL,
                repository TEXT NOT NULL,
                branch TEXT NOT NULL,
                pr_number INTEGER,
                pr_url TEXT,
                head_sha TEXT,
                status TEXT NOT NULL,
                detail TEXT NOT NULL,
                created REAL NOT NULL,
                updated REAL NOT NULL,
                PRIMARY KEY(batch_id, revision));
            CREATE TABLE IF NOT EXISTS publication_step(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                revision TEXT NOT NULL,
                step TEXT NOT NULL,
                detail TEXT NOT NULL,
                at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS review_attempt(
                repo TEXT NOT NULL,
                pr INTEGER NOT NULL,
                head_sha TEXT NOT NULL,
                status TEXT NOT NULL,
                verdict TEXT NOT NULL,
                detail TEXT NOT NULL,
                at REAL NOT NULL,
                patch_sha TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(repo, pr, head_sha));
            CREATE TABLE IF NOT EXISTS skill_material(
                digest TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                detail TEXT NOT NULL,
                at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        self.db.commit()
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(review_attempt)")}
        if "patch_sha" not in columns:
            self.db.execute(
                "ALTER TABLE review_attempt ADD COLUMN patch_sha TEXT NOT NULL DEFAULT ''"
            )
            self.db.commit()

    def close(self) -> None:
        with self.lock:
            self.db.close()

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
        }

    # ------------------------------------------------------------------ #
    # Review attempts: one per (repo, pr, head), durable before the model
    # ------------------------------------------------------------------ #

    def get_review(self, repo: str, pr: int, head_sha: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM review_attempt WHERE repo=? AND pr=? AND head_sha=?",
                (repo, pr, head_sha),
            ).fetchone()
        return dict(row) if row else None

    def reserve_review(self, repo: str, pr: int, head_sha: str) -> bool:
        """Record the attempt BEFORE any model call; False if already attempted."""
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.get_review(repo, pr, head_sha):
                return False
            self.db.execute(
                "INSERT INTO review_attempt(repo, pr, head_sha, status, verdict, detail, at)"
                " VALUES(?,?,?,'running','','',?)",
                (repo, pr, head_sha, time.time()),
            )
            return True

    def reviews_for_pr(self, repo: str, pr: int) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM review_attempt WHERE repo=? AND pr=? ORDER BY at",
                (repo, pr),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_patch_sha(self, repo: str, pr: int, head_sha: str, patch_sha: str) -> None:
        with self.lock, self.db:
            self.db.execute(
                "UPDATE review_attempt SET patch_sha=? WHERE repo=? AND pr=? AND head_sha=?",
                (patch_sha, repo, pr, head_sha),
            )

    def finish_review(self, repo: str, pr: int, head_sha: str, *, status: str, verdict: str, detail: str = "") -> None:
        with self.lock, self.db:
            self.db.execute(
                "UPDATE review_attempt SET status=?, verdict=?, detail=? WHERE repo=? AND pr=? AND head_sha=?",
                (status, verdict, detail[:MAX_DETAIL], repo, pr, head_sha),
            )

    # ------------------------------------------------------------------ #
    # Skill material digests: one bounded attempt per material set
    # ------------------------------------------------------------------ #

    def get_skill_material(self, material_digest: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM skill_material WHERE digest=?", (material_digest,)
            ).fetchone()
        return dict(row) if row else None

    def record_skill_material(self, material_digest: str, *, status: str, detail: str = "") -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO skill_material VALUES(?,?,?,?)",
                (material_digest, status, detail[:MAX_DETAIL], time.time()),
            )
