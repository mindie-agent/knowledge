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
``allows_hash``/``resolve``) never create the database; only the first
``activate``/``associate`` does. A complete existing database can acquire its
ownership marker without changing grants or receipts. The store holds grants
and attempt receipts only: no transcript payload or body history. The file
and its directory get restrictive permissions (0700/0600), like any token or
config state.

Canonical semantics (shared by every adapter):

- A lease is an internal identity binding for one native task, established
  automatically by the adapter for a verified, authorized native event. It is not a
  consent prompt: the installation-level shared settings file is the only
  persistent user choice. A binding lasts until an explicit ``deactivate``
  or an actual project-scope change. There is no wall-clock expiry, no
  failure-count pause and no config-byte/runtime-path fingerprint — ordinary
  failures never revoke a binding and never require a manual
  revoke/reactivate cycle.
- A repeated healthy ``activate`` preserves the existing random token and the
  original ``activated_at`` capture boundary. A fresh activation (new,
  revoked or changed scope) ROTATES the token and boundary — no deterministic
  reusable capability survives revocation.
- ``resolve(activation_token)`` returns the owning VALID lease (or None).
- ``claim(session, kind, identity, token=None)`` atomically consumes a
  durable attempt identity and returns bool: True exactly once per
  ``(session, kind, identity)`` — any repeat is False, even after a failure
  or crash, and attempts survive ``deactivate`` so revoke/reactivate never
  replays old work.
- ``finish(session, activation_token, succeeded)`` records the outcome in
  the task's consecutive-failure diagnostic counter. The counter is
  observability only: it never gates activation, capture or claims.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from .store import session_key
from .locks import StartInProgress

from ..owned_state import open_database

BASE_COLUMNS = {"session", "token", "enabled", "failures"}
CAPTURE_COLUMNS = BASE_COLUMNS | {"project_root", "root_session", "activated_at"}


class AdmissionUnavailable(RuntimeError):
    """The lease store could not be read. This is not an inactive lease."""


def activation_epoch(token):
    """Irreversible reference to one activation token. Not the token itself."""
    if not isinstance(token, str) or not token:
        raise ValueError("activation token required")
    return hashlib.sha256(b"mindie-activation/1\0" + token.encode("utf-8")).hexdigest()

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


def _initialize_admission(db):
    db.executescript(_SCHEMA)


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

    def _database(self, *, initialize_missing=False, timeout=0.1):
        """Use the shared authority contract; absence is valid only before use."""
        try:
            return open_database(
                self.path, schema="mindie-admission/1",
                required={"leases": CAPTURE_COLUMNS,
                          "attempts": {"session", "kind", "identity", "created"}},
                initialize=_initialize_admission, initialize_missing=initialize_missing,
                timeout=timeout,
            )
        except (OSError, sqlite3.Error, ValueError, StartInProgress) as exc:
            raise AdmissionUnavailable(str(exc)) from exc

    def _write(self, fn, *, initialize_missing=False, missing_error=None):
        """Serialize read+modify while preserving leases and attempt receipts."""
        with self._lock:
            db = self._database(initialize_missing=initialize_missing, timeout=5.0)
            if db is None:
                if missing_error is not None:
                    raise ValueError(missing_error)
                return False
            try:
                self.path.parent.chmod(0o700)
                self.path.chmod(0o600)
                db.execute("BEGIN IMMEDIATE")
                try:
                    result = fn(db)
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
                return result
            except (OSError, sqlite3.Error) as exc:
                raise AdmissionUnavailable(str(exc)) from exc
            finally:
                db.close()

    def _rows(self, sql="", args=()):
        """A missing unused store is empty; lost authority is never inactivity."""
        db = self._database()
        if db is None:
            return []
        try:
            rows = db.execute("SELECT * FROM leases WHERE enabled=1 " + sql, args).fetchall()
            return [{**dict(row), "capture_schema": True} for row in rows]
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise AdmissionUnavailable(str(exc)) from exc
        finally:
            db.close()

    # ------------------------------------------------------------ activation

    def inspect(self, session):
        """Read one diagnostic lease, including disabled rows; never grant.

        Unlike ``active_lease``, this reports a broken or locked store honestly.
        It never returns a token or initializes schema. The failure count is
        diagnostic only — there is no paused state.
        """
        result = dict(status="missing", enabled=False)
        if not isinstance(session, str) or not session.strip() or len(session) > 256:
            return dict(status="unavailable", enabled=False, error_class="ValueError")
        db = None
        try:
            db = self._database()
            if db is None:
                return result
            row = db.execute(
                "SELECT enabled, failures, project_root FROM leases WHERE session=? LIMIT 1",
                (session,),
            ).fetchone()
            if row is not None:
                enabled, failures, project_root = row
                result = dict(status="active" if enabled else "inactive",
                              enabled=bool(enabled), failures=failures,
                              project_root=project_root)
        except (AdmissionUnavailable, OSError, sqlite3.Error, ValueError, TypeError) as exc:
            result = dict(status="unavailable", enabled=False, error_class=type(exc).__name__)
        finally:
            if db is not None:
                db.close()
        return result

    def activate(self, session, *, project_root, root_session=None, not_before=None, automatic=False):
        """Bind one native task internally for capture.

        A repeated activate for the same session and scope preserves its
        token and original ``activated_at`` capture boundary, whatever the
        diagnostic failure counter says. A fresh activation (new session,
        after revocation, or a changed project scope) rotates the token and
        boundary. Returns the lease dict.
        """
        if not isinstance(session, str) or not session.strip() or len(session) > 256:
            raise ValueError("session must be nonempty text of at most 256 characters")
        session = session.strip()
        if not isinstance(project_root, str) or not Path(project_root).is_absolute():
            raise ValueError("project_root must be an absolute path")
        scope = str(Path(project_root).expanduser().resolve(strict=False))
        if root_session is not None and not isinstance(root_session, str):
            raise ValueError("root_session must be text or None")
        if not_before is not None and (type(not_before) not in (int, float)
                or not math.isfinite(not_before) or not_before < 0):
            raise ValueError("binding boundary must be finite Unix seconds")

        def op(db):
            row = db.execute(
                "SELECT * FROM leases WHERE session=?", (session,)
            ).fetchone()
            now = time.time()
            if automatic and row and not row[2]:
                raise ValueError("task binding was explicitly revoked")
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
                        now if not_before is None else not_before,
                    ),
                )
            return db.execute(
                "SELECT * FROM leases WHERE session=?", (session,)
            ).fetchone()

        row = self._write(op, initialize_missing=True)
        return {
            "session": row[0],
            "token": row[1],
            "enabled": bool(row[2]),
            "failures": row[3],
            "project_root": row[4],
            "root_session": row[5],
            "activated_at": row[6],
            "capture_schema": True,
        }

    def associate(self, session, *, project_root, not_before):
        """Associate a verified host event using its existing authorization.

        Association time is not a new authorization boundary. Never reactivate
        an explicitly revoked task as a side effect of an ordinary event.
        """
        return self.activate(session, project_root=project_root,
                             not_before=not_before, automatic=True)

    def deactivate(self, session):
        """Disable one task's lease; durable attempt identities are preserved
        so a later fresh activation never replays old work. The row (and its
        failure state) is kept for audit; a fresh activate rotates the token
        and boundary. Returns True when a live lease was disabled."""
        if not isinstance(session, str) or not session:
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
        """Currently valid leases; read faults raise AdmissionUnavailable."""
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
            raise ValueError("task binding is not active")
        if token is not None and not hmac.compare_digest(lease["token"], token):
            raise ValueError("task binding is not active")
        return lease

    def capture_lease(self, session, token):
        """Lease check for capture; fails closed on the old lease schema."""
        if not isinstance(token, str):
            raise ValueError("task binding token required")
        lease = self.check(session, token)
        if not lease.get("capture_schema"):
            raise ValueError(
                "capture requires an adapter lease with project_root, root_session "
                "and activated_at; the existing lease store predates this schema"
            )
        return lease

    def capture_authorization(self, session, token, *, timeout):
        """Read one lease for a Stop handoff.

        Returns ``state`` of ``admitted``, ``inactive``, or ``schema``. A
        locked or unreadable database raises ``AdmissionUnavailable`` instead
        of looking inactive. The token is not copied into the result.
        """
        if (
            not isinstance(session, str)
            or not session.strip()
            or session != session.strip()
            or len(session) > 256
            or not isinstance(token, str)
            or not token
            or len(token) > 512
        ):
            return {"state": "inactive"}
        timeout = max(0.05, min(float(timeout), 0.25))
        db = self._database(timeout=timeout)
        if db is None:
            return {"state": "inactive"}
        try:
            db.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
            columns = [row[1] for row in db.execute("PRAGMA table_info(leases)")]
            if not columns:
                raise AdmissionUnavailable("lease schema is missing")
            if not BASE_COLUMNS <= set(columns):
                raise AdmissionUnavailable("schema")
            if not CAPTURE_COLUMNS <= set(columns):
                return {"state": "schema"}
            row = db.execute(
                "SELECT * FROM leases WHERE session=? LIMIT 1", (session,)
            ).fetchone()
            if row is None:
                return {"state": "inactive"}
            lease = dict(zip(columns, row))
            if lease.get("enabled") != 1:
                return {"state": "inactive"}
            try:
                matches = hmac.compare_digest(str(lease.get("token") or ""), token)
            except (TypeError, ValueError):
                matches = False
            if not matches:
                return {"state": "inactive"}
            failures = int(lease.get("failures") or 0)
            root = lease.get("project_root")
            if not isinstance(root, str) or not root:
                return {"state": "schema"}
            try:
                scope = str(Path(root).expanduser().resolve(strict=False))
            except OSError as exc:
                raise AdmissionUnavailable("lease scope is unreadable") from exc
            root_session = lease.get("root_session") or session
            if not isinstance(root_session, str) or not root_session.strip():
                root_session = session
            return {
                "state": "admitted",
                "scope": scope,
                "root_session": root_session,
                "activated_at": lease.get("activated_at"),
                "epoch": activation_epoch(token),
                "failures": failures,
            }
        except AdmissionUnavailable:
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise AdmissionUnavailable(str(exc)) from exc
        finally:
            db.close()

    def resolve_capture_lease(self, session, *, timeout):
        """Read one live lease for an internal Stop that did not pass a token.

        The stored token is used only to compute the activation epoch and is
        not returned. An unreadable or locked database raises
        ``AdmissionUnavailable``. A missing or disabled lease is inactive.
        This does not use ``active_lease``, which reports both cases as None.
        External MCP and RPC callers are unchanged and still require a token.
        """
        if (
            not isinstance(session, str)
            or not session.strip()
            or session != session.strip()
            or len(session) > 256
        ):
            return {"state": "inactive"}
        timeout = max(0.05, min(float(timeout), 0.25))
        db = self._database(timeout=timeout)
        if db is None:
            return {"state": "inactive"}
        try:
            db.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
            deadline = time.monotonic() + timeout
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            columns = [row[1] for row in db.execute("PRAGMA table_info(leases)")]
            if not columns:
                raise AdmissionUnavailable("lease schema is missing")
            if not BASE_COLUMNS <= set(columns):
                raise AdmissionUnavailable("schema")
            if not CAPTURE_COLUMNS <= set(columns):
                return {"state": "schema"}
            row = db.execute(
                "SELECT * FROM leases WHERE session=? LIMIT 1", (session,)
            ).fetchone()
            if row is None:
                return {"state": "inactive"}
            lease = dict(zip(columns, row))
            if lease.get("enabled") != 1:
                return {"state": "inactive"}
            failures = int(lease.get("failures") or 0)
            token = lease.get("token")
            if not isinstance(token, str) or not token or len(token) > 512:
                return {"state": "schema"}
            root = lease.get("project_root")
            if not isinstance(root, str) or not root:
                return {"state": "schema"}
            try:
                scope = str(Path(root).expanduser().resolve(strict=False))
            except OSError as exc:
                raise AdmissionUnavailable("lease scope is unreadable") from exc
            root_session = lease.get("root_session") or session
            if not isinstance(root_session, str) or not root_session.strip():
                root_session = session
            return {
                "state": "admitted",
                "scope": scope,
                "root_session": root_session,
                "activated_at": lease.get("activated_at"),
                "epoch": activation_epoch(token),
                "failures": failures,
            }
        except AdmissionUnavailable:
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise AdmissionUnavailable(str(exc)) from exc
        finally:
            db.close()

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
        Enabled/token checks and the insert run in the SAME BEGIN IMMEDIATE
        transaction so a revoke racing this claim cannot admit after the
        lease is gone."""
        if not isinstance(session, str) or not session.strip():
            raise ValueError("task binding is not active")
        for value, name in ((kind, "kind"), (identity, "identity")):
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ValueError(f"{name} must be nonempty text")

        def op(db):
            row = db.execute(
                "SELECT token, enabled, failures FROM leases WHERE session=?",
                (session,),
            ).fetchone()
            if row is None or row[1] != 1:
                raise ValueError("task binding is not active")
            if token is not None and (
                not isinstance(token, str)
                or not hmac.compare_digest(row[0], token)
            ):
                raise ValueError("task binding is not active")
            cursor = db.execute(
                "INSERT OR IGNORE INTO attempts VALUES(?,?,?,?)",
                (session, kind.strip(), identity.strip(), time.time()),
            )
            return cursor.rowcount == 1

        return self._write(op, missing_error="task binding is not active")

    def finish(self, session, activation_token, succeeded):
        """Record the outcome of one admitted call in the task's
        consecutive-failure diagnostic counter. The UPDATE is conditioned on
        the exact session + activation token + enabled, so an old in-flight
        outcome cannot mutate a newly rotated lease. The counter is pure
        observability: it never pauses or revokes the lease."""
        if not isinstance(session, str) or not session:
            raise ValueError("invalid activation token")
        if not isinstance(activation_token, str) or not activation_token:
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
                    "AND enabled=1",
                    (session, activation_token),
                )
            else:
                db.execute(
                    "UPDATE leases SET failures=failures+1 WHERE session=? "
                    "AND token=? AND enabled=1",
                    (session, activation_token),
                )

        self._write(op, missing_error="invalid activation token")
