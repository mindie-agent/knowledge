"""Neutral admission store owned by the knowledge core.

``Admission(path)`` takes an explicit, neutral admission SQLite path from the
engine configuration (``admission_path``) — never an adapter config path, a
Codex session database or a runtime-path fingerprint. Each harness uses a
distinct admission file under its own local domain root (default ``codex`` and
``kimi`` namespaces); the native session identity is supplied only by that
adapter, never by a model argument or a newest-session fallback.

Core owns the minimal schema (one lease per native session plus durable
attempt identities). Construction and all read-only checks
(``check``/``capture_lease``/``active_lease``/``leases``/``scope_root``/
``allows_hash``/``resolve``) never create the file; only the mutating calls
(``activate``/``claim``/``finish``/``deactivate``) do. The store holds grants
and attempt receipts only: no transcript payload or body history. The file
and its directory get restrictive permissions (0700/0600), like any token or
config state.

Canonical semantics (shared by every adapter):

- Authorization persists for the same native task until revoked
  (``deactivate``), the failure circuit pauses it (3 consecutive failures) or
  the user actually changes its project scope. There is no wall-clock expiry
  and no config-byte/runtime-path fingerprint.
- A repeated healthy ``activate`` preserves the existing random token and the
  original ``activated_at`` capture boundary. A fresh activation (new,
  revoked or changed scope) ROTATES the token and boundary — no deterministic
  reusable capability survives revocation. A paused lease is never unpaused
  by ``activate``; recovery is explicit revoke/reactivate.
- ``resolve(activation_token)`` returns the owning VALID lease (or None).
- ``claim(session, kind, identity, token=None)`` atomically consumes a
  durable attempt identity and returns bool: True exactly once per
  ``(session, kind, identity)`` — any repeat is False, even after a failure
  or crash, and attempts survive ``deactivate`` so revoke/reactivate never
  replays old work.
- ``finish(session, activation_token, succeeded)`` updates the task's
  consecutive-failure counter: a valid successful call resets it only while
  the lease is not paused; failures >= 3 never auto-reset.
"""

from __future__ import annotations

import hmac
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from .store import session_key

BASE_COLUMNS = {"session", "token", "enabled", "failures"}
CAPTURE_COLUMNS = BASE_COLUMNS | {"project_root", "root_session", "activated_at"}
MAX_FAILURES = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leases(
    session TEXT PRIMARY KEY,
    token TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    failures INTEGER NOT NULL,
    project_root TEXT,
    root_session TEXT,
    activated_at REAL);
CREATE TABLE IF NOT EXISTS attempts(
    session TEXT NOT NULL,
    kind TEXT NOT NULL,
    identity TEXT NOT NULL,
    created REAL NOT NULL,
    PRIMARY KEY(session, kind, identity));
"""


class Admission:
    def __init__(self, path):
        candidate = Path(path)
        if candidate.suffix.lower() in {".json", ".toml", ".yaml", ".yml", ".ini", ".cfg"}:
            raise ValueError(
                "admission_path must be an explicit SQLite admission file "
                "(for example admission.sqlite3), not an adapter config path"
            )
        self.path = candidate.absolute()
        self._lock = threading.RLock()

    # ------------------------------------------------------------- internals

    def _write(self, fn):
        """Mutating path: BEGIN IMMEDIATE so concurrent processes serialize
        the read+modify of activate/claim/finish (a deferred transaction
        would let two healthy activates both observe the empty row and
        issue distinct tokens)."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.path.parent.chmod(0o700)
            except OSError:
                pass
            db = sqlite3.connect(self.path, timeout=5.0)
            try:
                db.executescript(_SCHEMA)
                db.execute("BEGIN IMMEDIATE")
                try:
                    result = fn(db)
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
            finally:
                db.close()
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
            return result

    def _rows(self, sql="", args=()):
        """Read-only valid lease rows; empty on any problem, never creates state."""
        if not self.path.is_file():
            return []
        try:
            db = sqlite3.connect(
                self.path.as_uri() + "?mode=ro", uri=True, timeout=0.1
            )
        except sqlite3.Error:
            return []
        try:
            columns = {row[1] for row in db.execute("PRAGMA table_info(leases)")}
            if not BASE_COLUMNS <= columns:
                return []
            names = [row[1] for row in db.execute("PRAGMA table_info(leases)")]
            rows = db.execute(
                "SELECT * FROM leases WHERE enabled=1 AND failures<? " + sql,
                (MAX_FAILURES, *args),
            ).fetchall()
            result = []
            for row in rows:
                lease = dict(zip(names, row))
                lease["capture_schema"] = CAPTURE_COLUMNS <= columns
                result.append(lease)
            return result
        except sqlite3.Error:
            return []
        finally:
            db.close()

    # ------------------------------------------------------------ activation

    def inspect(self, session):
        """Read one diagnostic lease, including paused/disabled rows; never grant.

        Unlike ``active_lease``, this reports a broken or locked store honestly.
        It never returns a token, initializes schema, or resets the circuit.
        """
        result = dict(status="missing", enabled=False)
        if not isinstance(session, str) or not session.strip() or len(session) > 256:
            return dict(status="unavailable", enabled=False, error_class="ValueError")
        db = None
        try:
            self.path.stat()
            db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=0.1)
            deadline = time.monotonic() + 0.25
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            row = db.execute(
                "SELECT enabled, failures, project_root FROM leases WHERE session=? LIMIT 1",
                (session,),
            ).fetchone()
            if row is not None:
                enabled, failures, project_root = row
                paused = bool(enabled) and failures >= MAX_FAILURES
                result = dict(status="paused" if paused else "active" if enabled else "inactive",
                              enabled=bool(enabled) and not paused, failures=failures,
                              project_root=project_root)
        except FileNotFoundError:
            pass
        except (OSError, sqlite3.Error, TypeError) as exc:
            result = dict(status="unavailable", enabled=False, error_class=type(exc).__name__)
        finally:
            if db is not None:
                db.close()
        return result

    def activate(self, session, *, project_root, root_session=None):
        """Explicitly authorize one native task for capture.

        A repeated healthy activate for the same session and scope preserves
        its token and original ``activated_at`` capture boundary. A paused
        lease is NOT reset — recovery is explicit ``deactivate`` plus a fresh
        activate. A fresh activation (new session, after revocation, or a
        changed project scope) rotates the token and boundary. Returns the
        lease dict.
        """
        if not isinstance(session, str) or not session.strip() or len(session) > 256:
            raise ValueError("session must be nonempty text of at most 256 characters")
        session = session.strip()
        if not isinstance(project_root, str) or not Path(project_root).is_absolute():
            raise ValueError("project_root must be an absolute path")
        scope = str(Path(project_root).expanduser().resolve(strict=False))
        if root_session is not None and not isinstance(root_session, str):
            raise ValueError("root_session must be text or None")

        def op(db):
            row = db.execute(
                "SELECT * FROM leases WHERE session=?", (session,)
            ).fetchone()
            now = time.time()
            if row and row[2] == 1 and row[3] >= MAX_FAILURES:
                # Paused by the failure circuit: activate is never a bypass
                # and never a success-shaped enabled grant.
                return row
            if row and row[2] == 1 and row[4] == scope:
                # Healthy re-activation: keep the token and original boundary.
                db.execute(
                    "UPDATE leases SET root_session=COALESCE(?, root_session) "
                    "WHERE session=?",
                    (root_session, session),
                )
            else:
                # Fresh activation: rotate the token and capture boundary.
                db.execute(
                    "INSERT OR REPLACE INTO leases VALUES(?,?,?,?,?,?,?)",
                    (
                        session,
                        secrets.token_hex(32),
                        1,
                        0,
                        scope,
                        root_session or session,
                        now,
                    ),
                )
            return db.execute(
                "SELECT * FROM leases WHERE session=?", (session,)
            ).fetchone()

        row = self._write(op)
        paused = bool(row[2]) and row[3] >= MAX_FAILURES
        return {
            "session": row[0],
            "token": row[1],
            "enabled": bool(row[2]) and not paused,
            "failures": row[3],
            "paused": paused,
            "project_root": row[4],
            "root_session": row[5],
            "activated_at": row[6],
            "capture_schema": True,
        }

    def deactivate(self, session):
        """Disable one task's lease; durable attempt identities are preserved
        so a later fresh activation never replays old work. The row (and its
        failure state) is kept for audit; a fresh activate rotates the token
        and boundary. Returns True when a live lease was disabled."""
        if not isinstance(session, str) or not session:
            return False
        if not self.path.is_file():
            return False

        def op(db):
            cursor = db.execute(
                "UPDATE leases SET enabled=0 WHERE session=? AND enabled=1",
                (session,),
            )
            return cursor.rowcount > 0

        return self._write(op)

    # ---------------------------------------------------------- lease checks

    def leases(self):
        """Currently valid lease dicts; empty on any read problem."""
        return self._rows()

    def active_lease(self, session):
        """Valid lease for one native session id, without a token (worker re-check)."""
        if not isinstance(session, str):
            return None
        for lease in self._rows():
            if lease["session"] == session:
                return lease
        return None

    def check(self, session, token=None):
        """Active-lease check for admitted read/feedback calls; when an
        activation token is supplied it must also match exactly."""
        lease = self.active_lease(session)
        if lease is None:
            raise ValueError("session is not manually activated")
        if token is not None and not hmac.compare_digest(lease["token"], token):
            raise ValueError("session is not manually activated")
        return lease

    def capture_lease(self, session, token):
        """Lease check for capture; fails closed on the old lease schema."""
        if not isinstance(token, str):
            raise ValueError("manual session activation required")
        lease = self.check(session, token)
        if not lease.get("capture_schema"):
            raise ValueError(
                "capture requires an adapter lease with project_root, root_session "
                "and activated_at; the existing lease store predates this schema"
            )
        return lease

    def resolve(self, token):
        """Resolve an ACTIVATION token to its owning valid lease (or None)."""
        if not isinstance(token, str) or not token or len(token) > 128:
            return None
        for lease in self._rows():
            if hmac.compare_digest(lease["token"], token):
                return lease
        return None

    def allows_hash(self, hashed_session):
        try:
            return any(
                session_key(lease["session"]) == hashed_session
                for lease in self._rows()
            )
        except (OSError, ValueError, KeyError):
            return False

    def scope_root(self, session):
        """The lease's own authorized canonical project root (never caller cwd)."""
        lease = self.active_lease(session)
        if not lease or not isinstance(lease.get("project_root"), str):
            return None
        try:
            return str(Path(lease["project_root"]).expanduser().resolve(strict=False))
        except OSError:
            return None

    # --------------------------------------------------------- attempt guard

    def claim(self, session, kind, identity, token=None):
        """Atomically consume one durable attempt identity; returns True
        exactly once per ``(session, kind, identity)`` — any repeat is False,
        even after a failure or crash, and attempts survive deactivation.
        Enabled/token/failure-threshold checks and the insert run in the
        SAME BEGIN IMMEDIATE transaction so a revoke racing this claim
        cannot admit after the lease is gone."""
        if not isinstance(session, str) or not session.strip():
            raise ValueError("session is not manually activated")
        for value, name in ((kind, "kind"), (identity, "identity")):
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ValueError(f"{name} must be nonempty text")
        if not self.path.is_file():
            raise ValueError("session is not manually activated")

        def op(db):
            row = db.execute(
                "SELECT token, enabled, failures FROM leases WHERE session=?",
                (session,),
            ).fetchone()
            if (
                row is None
                or row[1] != 1
                or row[2] >= MAX_FAILURES
            ):
                raise ValueError("session is not manually activated")
            if token is not None and (
                not isinstance(token, str)
                or not hmac.compare_digest(row[0], token)
            ):
                raise ValueError("session is not manually activated")
            cursor = db.execute(
                "INSERT OR IGNORE INTO attempts VALUES(?,?,?,?)",
                (session, kind.strip(), identity.strip(), time.time()),
            )
            return cursor.rowcount == 1

        return self._write(op)

    def finish(self, session, activation_token, succeeded):
        """Record the outcome of one admitted call against the task's
        consecutive-failure counter. The UPDATE is conditioned on the exact
        session + activation token + enabled, so an old in-flight outcome
        cannot mutate a newly rotated lease. A valid successful call resets
        the counter only while the lease is not paused; a lease at 3+
        failures never auto-recovers."""
        if not isinstance(session, str) or not session:
            raise ValueError("invalid activation token")
        if not isinstance(activation_token, str) or not activation_token:
            raise ValueError("invalid activation token")
        if not self.path.is_file():
            raise ValueError("invalid activation token")

        def op(db):
            row = db.execute(
                "SELECT session, token, enabled, failures FROM leases "
                "WHERE session=?",
                (session,),
            ).fetchone()
            if (
                row is None
                or row[2] != 1
                or not hmac.compare_digest(row[1], activation_token)
            ):
                raise ValueError("invalid activation token")
            if succeeded:
                db.execute(
                    "UPDATE leases SET failures=0 WHERE session=? AND token=? "
                    "AND enabled=1 AND failures<?",
                    (session, activation_token, MAX_FAILURES),
                )
            else:
                db.execute(
                    "UPDATE leases SET failures=failures+1 WHERE session=? "
                    "AND token=? AND enabled=1",
                    (session, activation_token),
                )

        self._write(op)

    def _paused(self):
        """Enabled but circuit-paused lease rows (read-only)."""
        if not self.path.is_file():
            return []
        try:
            db = sqlite3.connect(
                self.path.as_uri() + "?mode=ro", uri=True, timeout=0.1
            )
        except sqlite3.Error:
            return []
        try:
            names = [row[1] for row in db.execute("PRAGMA table_info(leases)")]
            return [
                dict(zip(names, row))
                for row in db.execute(
                    "SELECT * FROM leases WHERE enabled=1 AND failures>=?",
                    (MAX_FAILURES,),
                )
            ]
        except sqlite3.Error:
            return []
        finally:
            db.close()
