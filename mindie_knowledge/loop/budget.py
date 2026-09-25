"""Durable admission limits for optional model work, never a retry queue."""

import time

from .store import _upsert_continuation

# The model may return 32 KiB. Saving that result also stores at most three
# assigned entry ids and new flags. Those local fields are not a second gate.
MAX_CHECKPOINT_RESULT = 32 * 1024 + 3 * 128


# Categories that are one item's own content failure: the attempt is consumed
# (never replayed) but it must not pause the whole domain's maintenance for
# other tasks. Shared prerequisites (configuration) and systemic runtime
# failures still count toward the domain pause circuit.
CONTENT_FAILURE_CATEGORIES = frozenset({"invalid_result", "output_limit"})


class BudgetExceeded(RuntimeError):
    def __init__(self, message, *, retry_at=None):
        super().__init__(message)
        self.retry_at = retry_at  # only unattempted quota-deferred work may resume


class MaintenanceBudget:
    SESSION_LIMIT = 6
    SESSION_WINDOW = 3600
    HOURLY_LIMIT = 20
    FAILURE_LIMIT = 3

    def __init__(self, store):
        self.store = store
        with store.lock, store.db:
            store.db.execute(
                "CREATE TABLE IF NOT EXISTS maintenance_attempts("
                "id TEXT PRIMARY KEY, session TEXT NOT NULL, role TEXT NOT NULL, "
                "started REAL NOT NULL, status TEXT NOT NULL, "
                "result TEXT, apply_receipt TEXT)"
            )
            columns = {
                row[1]
                for row in store.db.execute("PRAGMA table_info(maintenance_attempts)")
            }
            if "result" not in columns:
                store.db.execute("ALTER TABLE maintenance_attempts ADD COLUMN result TEXT")
            if "apply_receipt" not in columns:
                store.db.execute(
                    "ALTER TABLE maintenance_attempts ADD COLUMN apply_receipt TEXT"
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
                    "SELECT count(*) FROM maintenance_attempts WHERE session=? AND started>?",
                    (session, now - self.SESSION_WINDOW),
                ).fetchone()[0]
                >= self.SESSION_LIMIT
            ):
                first = db.execute(
                    "SELECT MIN(started) FROM maintenance_attempts WHERE session=? AND started>?",
                    (session, now - self.SESSION_WINDOW),
                ).fetchone()[0]
                raise BudgetExceeded("session rolling maintenance limit reached",
                                     retry_at=first + self.SESSION_WINDOW + 1)
            if (
                db.execute(
                    "SELECT count(*) FROM maintenance_attempts WHERE started>?",
                    (now - 3600,),
                ).fetchone()[0]
                >= self.HOURLY_LIMIT
            ):
                first = db.execute("SELECT MIN(started) FROM maintenance_attempts WHERE started>?",
                                   (now - 3600,)).fetchone()[0]
                raise BudgetExceeded("domain hourly maintenance call limit reached",
                                     retry_at=first + 3601)
            if db.execute(
                "SELECT 1 FROM maintenance_attempts WHERE status='running' AND started>?",
                (now - 135,),
            ).fetchone():
                raise BudgetExceeded("another maintenance call is in progress", retry_at=now + 135)
            db.execute(
                "INSERT INTO maintenance_attempts"
                "(id, session, role, started, status) VALUES(?,?,?,?,?)",
                (ident, session, role, now, "running"),
            )

    def _record_circuit(self, db):
        last = db.execute(
            "SELECT status FROM maintenance_attempts ORDER BY started DESC, rowid DESC LIMIT ?",
            (self.FAILURE_LIMIT,),
        ).fetchall()
        if len(last) == self.FAILURE_LIMIT and all(row[0] == "failed" for row in last):
            db.execute(
                "INSERT OR REPLACE INTO state VALUES('maintenance_paused', 'consecutive failures')"
            )

    def finish(self, ident, succeeded, *, category=None):
        """Record the outcome. ``succeeded=None`` is a cancellation (sharing
        revoked or shutdown): the attempt is consumed but never counted as a
        failure toward the pause circuit. A failed attempt carrying a
        per-item content category (invalid model result, output overflow) is
        recorded as ``invalid``: it stays consumed and visible, but it does
        not pause unrelated tasks' maintenance — only shared or systemic
        failures (configuration, deadline, native, unknown) feed the domain
        circuit."""
        status = "succeeded" if succeeded else "failed"
        if succeeded is None:
            status = "cancelled"
        elif not succeeded and category in CONTENT_FAILURE_CATEGORIES:
            status = "invalid"
        with self.store.lock, self.store.db:
            db = self.store.db
            db.execute(
                "UPDATE maintenance_attempts SET status=? WHERE id=?",
                (status, ident),
            )
            self._record_circuit(db)

    def abandon_unstarted(self, ident):
        """Drop a reserved attempt whose model process never started.

        A crash after the model starts still leaves the running row, and
        service start consumes it. This delete is only for an attempt that
        has no saved result and did not spawn.
        """
        with self.store._write_txn():
            self.store.db.execute(
                "DELETE FROM maintenance_attempts WHERE id=? AND status='running' "
                "AND result IS NULL",
                (ident,),
            )

    def recover_interrupted(self):
        """On service start, consume model crashes that saved no result.

        A row already checkpointed for application stays runnable. It is not
        a failed model attempt and it is not run again.
        """
        with self.store.lock:
            interrupted = self.store.db.execute(
                "SELECT id FROM maintenance_attempts WHERE status='running' "
                "AND result IS NULL ORDER BY started"
            ).fetchall()
            for row in interrupted:
                self.finish(row[0], False)

    def checkpoint(self, ident, result_text, receipt_text, *, capture_id=None,
                   capture_status=None, capture_detail="", region_id=None,
                   region_status=None, region_detail=""):
        """Persist one scanned result together with its capture and region state.

        Result text, apply-pending (or the given capture status), and the
        region receipt commit in the one store transaction. A crash rolls
        all of them back, so a success receipt cannot exist without the result.
        """
        if (
            not isinstance(result_text, str)
            or len(result_text.encode("utf-8")) > MAX_CHECKPOINT_RESULT
        ):
            raise ValueError("checkpoint result exceeds the saved-result limit")
        with self.store._write_txn():
            db = self.store.db
            updated = db.execute(
                "UPDATE maintenance_attempts SET status='apply', result=?, "
                "apply_receipt=? WHERE id=? AND status IN ('running', 'apply')",
                (result_text, receipt_text, ident),
            )
            if updated.rowcount != 1:
                raise ValueError("maintenance attempt is not checkpointable")
            if capture_id and capture_status:
                db.execute(
                    "UPDATE captures SET status=?, detail=? WHERE id=?",
                    (capture_status, str(capture_detail)[:1000], capture_id),
                )
                if capture_status == "apply-pending":
                    existing = db.execute(
                        "SELECT 1 FROM continuations WHERE capture_id=?",
                        (capture_id,),
                    ).fetchone()
                    if existing is None:
                        _upsert_continuation(
                            db, capture_id, time.time(), "apply-pending", True,
                        )
                elif capture_status not in {"queued", "pending", "deferred"}:
                    db.execute(
                        "DELETE FROM continuations WHERE capture_id=?", (capture_id,)
                    )
            if region_id and region_status:
                self.store.finish_region(region_id, region_status, region_detail)

    def application(self, ident):
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT id, status, result, apply_receipt FROM maintenance_attempts WHERE id=?",
                (ident,),
            ).fetchone()
        if row is None:
            return None
        return dict(id=row[0], status=row[1], result=row[2], apply_receipt=row[3])

    def pending_applications(self):
        with self.store.lock:
            rows = self.store.db.execute(
                "SELECT id FROM maintenance_attempts WHERE status='apply' "
                "AND result IS NOT NULL ORDER BY started"
            ).fetchall()
        return [row[0] for row in rows]

    def _finish_capture_locked(self, capture_id, detail, more):
        if not capture_id:
            return
        db = self.store.db
        if more:
            reason = "organized one bounded increment; continuation pending"
            db.execute(
                "UPDATE captures SET status='pending', detail=? WHERE id=?",
                (reason, capture_id),
            )
            _upsert_continuation(db, capture_id, time.time() + 1, reason, True)
            return
        db.execute(
            "UPDATE captures SET status='organized', detail=? WHERE id=?",
            (str(detail)[:1000], capture_id),
        )
        db.execute("DELETE FROM continuations WHERE capture_id=?", (capture_id,))

    def settle_empty(self, ident, receipt_text, *, capture_id, capture_detail="",
                     region_id=None, region_status="succeeded", more=False):
        """No scanned entry to apply. Success, cleanup, and the region commit once."""
        with self.store._write_txn():
            db = self.store.db
            updated = db.execute(
                "UPDATE maintenance_attempts SET status='succeeded', result=NULL, "
                "apply_receipt=? WHERE id=? AND status='running' AND result IS NULL",
                (receipt_text, ident),
            )
            if updated.rowcount != 1:
                raise ValueError("maintenance attempt is not settleable")
            self._finish_capture_locked(capture_id, capture_detail, more)
            if region_id and region_status:
                self.store.finish_region(region_id, region_status, "")
            self._record_circuit(db)

    def complete_application(self, ident, receipt_text, *, capture_id=None,
                             capture_detail="", more=False):
        """Model succeeded and every scanned entry was applied. Drop the body.

        Success status, result cleanup, and the capture completion are one
        commit. An interruption cannot leave status=apply with result NULL.
        """
        with self.store._write_txn():
            db = self.store.db
            updated = db.execute(
                "UPDATE maintenance_attempts SET status='succeeded', result=NULL, "
                "apply_receipt=? WHERE id=? AND status='apply' AND result IS NOT NULL",
                (receipt_text, ident),
            )
            if updated.rowcount != 1:
                raise ValueError("application is not completable")
            self._finish_capture_locked(capture_id, capture_detail, more)
            self._record_circuit(db)

    def release_application(self, ident, receipt_text):
        """Authorization changed. Drop the body and do not count a model failure."""
        with self.store.lock, self.store.db:
            self.store.db.execute(
                "UPDATE maintenance_attempts SET status='cancelled', result=NULL, "
                "apply_receipt=? WHERE id=?",
                (receipt_text, ident),
            )

    def resume(self):
        # Keep attempts and quotas: explicit resume does not replay failed work.
        # Captures held only because the circuit was paused become due again.
        now = time.time()
        with self.store._write_txn():
            self.store.db.execute("DELETE FROM state WHERE key='maintenance_paused'")
            self.store.db.execute(
                "UPDATE continuations SET due=?, eligible=1 "
                "WHERE eligible=0 OR reason='maintenance-paused'",
                (now,),
            )
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
                session_window_seconds=self.SESSION_WINDOW,
                hourly_call_limit=self.HOURLY_LIMIT,
                max_concurrent_calls=1,
            )
