"""Durable admission limits for optional model work, never a retry queue."""

import time


class BudgetExceeded(RuntimeError):
    pass


class MaintenanceBudget:
    SESSION_LIMIT = 6
    HOURLY_LIMIT = 20
    FAILURE_LIMIT = 3

    def __init__(self, store):
        self.store = store
        with store.lock, store.db:
            store.db.execute(
                "CREATE TABLE IF NOT EXISTS maintenance_attempts("
                "id TEXT PRIMARY KEY, session TEXT NOT NULL, role TEXT NOT NULL, "
                "started REAL NOT NULL, status TEXT NOT NULL)"
            )

    def reserve(self, ident, session, role):
        now = time.time()
        with self.store.lock, self.store.db:
            # Serialize admission even across separate processes sharing this root.
            self.store.db.execute("BEGIN IMMEDIATE")
            db = self.store.db
            if db.execute(
                "SELECT 1 FROM maintenance_attempts WHERE id=?", (ident,)
            ).fetchone():
                raise BudgetExceeded("this maintenance item has already been attempted")
            if db.execute(
                "SELECT 1 FROM state WHERE key='maintenance_paused'"
            ).fetchone():
                raise BudgetExceeded(
                    "maintenance paused after consecutive failures; explicit resume required"
                )
            if (
                db.execute(
                    "SELECT count(*) FROM maintenance_attempts WHERE session=?",
                    (session,),
                ).fetchone()[0]
                >= self.SESSION_LIMIT
            ):
                raise BudgetExceeded("session maintenance call limit reached")
            if (
                db.execute(
                    "SELECT count(*) FROM maintenance_attempts WHERE started>?",
                    (now - 3600,),
                ).fetchone()[0]
                >= self.HOURLY_LIMIT
            ):
                raise BudgetExceeded("domain hourly maintenance call limit reached")
            if db.execute(
                "SELECT 1 FROM maintenance_attempts WHERE status='running' AND started>?",
                (now - 75,),
            ).fetchone():
                raise BudgetExceeded("another maintenance call is in progress")
            db.execute(
                "INSERT INTO maintenance_attempts VALUES(?,?,?,?,?)",
                (ident, session, role, now, "running"),
            )

    def finish(self, ident, succeeded):
        """Record the outcome. ``succeeded=None`` is a cancellation (sharing
        revoked or shutdown): the attempt is consumed but never counted as a
        failure toward the pause circuit."""
        status = "succeeded" if succeeded else "failed"
        if succeeded is None:
            status = "cancelled"
        with self.store.lock, self.store.db:
            db = self.store.db
            db.execute(
                "UPDATE maintenance_attempts SET status=? WHERE id=?",
                (status, ident),
            )
            last = db.execute(
                "SELECT status FROM maintenance_attempts ORDER BY started DESC, rowid DESC LIMIT ?",
                (self.FAILURE_LIMIT,),
            ).fetchall()
            if len(last) == self.FAILURE_LIMIT and all(
                row[0] == "failed" for row in last
            ):
                db.execute(
                    "INSERT OR REPLACE INTO state VALUES('maintenance_paused', 'consecutive failures')"
                )

    def resume(self):
        # Keep attempts and quotas: explicit resume does not replay failed work.
        with self.store.lock, self.store.db:
            self.store.db.execute("DELETE FROM state WHERE key='maintenance_paused'")
        return self.status()

    def status(self):
        with self.store.lock:
            return dict(
                paused=bool(
                    self.store.db.execute(
                        "SELECT 1 FROM state WHERE key='maintenance_paused'"
                    ).fetchone()
                ),
                calls_last_hour=self.store.db.execute(
                    "SELECT count(*) FROM maintenance_attempts WHERE started>?",
                    (time.time() - 3600,),
                ).fetchone()[0],
                session_call_limit=self.SESSION_LIMIT,
                hourly_call_limit=self.HOURLY_LIMIT,
                max_concurrent_calls=1,
            )
