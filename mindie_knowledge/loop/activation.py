"""Neutral admission store owned by the knowledge core.

``Admission(path)`` takes an explicit, neutral admission SQLite path from the
engine configuration (``admission_path``) — never an adapter config path, a
Codex session database or a runtime-path fingerprint. Each harness uses a
distinct admission file under its own local domain root (default ``codex`` and
``kimi`` namespaces); the native session identity is supplied only by that
adapter, never by a model argument or a newest-session fallback.

Core owns the minimal schema. Constructing the object or running any read-only
check (``check``/``capture_lease``/``active_lease``/``leases``/``scope_root``/
``allows_hash``/``resolve``) never creates the file; only the mutating calls
(``activate``/``claim``/``finish``/``deactivate``) do. The store holds grants
and receipts only: no transcript payload or body history.

Authorization persists for the same native task until it is revoked
(``deactivate``), the failure circuit trips, or the user actually changes its
domain/project scope — there is no fixed wall-clock expiry and no
config-byte/runtime-path fingerprint, so a healthy repeated ``activate`` keeps
the same token and original capture boundary (``activated_at``). The failure
circuit (3 recorded failures) pauses a lease; ``finish`` and ``activate``
never silently reset it — only an explicit fresh ``activate`` after revocation
starts a new lease.
"""

from __future__ import annotations

import hashlib
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
CREATE TABLE IF NOT EXISTS receipts(
    token TEXT PRIMARY KEY,
    session TEXT NOT NULL,
    kind TEXT NOT NULL,
    identity TEXT NOT NULL,
    status TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL);
"""


def _token_for(admission_path, session):
    """Deterministic, unguessable activation token for one admission file and
    native session: derived from this store's own random secret, so a repeated
    healthy activate preserves the token while another file cannot forge it."""
    return hmac.new(
        _secret(admission_path), session.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _secret(admission_path):
    db = sqlite3.connect(admission_path, timeout=1.0)
    try:
        db.execute(
            "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        row = db.execute("SELECT value FROM meta WHERE key='secret'").fetchone()
        if row:
            return bytes.fromhex(row[0])
        secret = secrets.token_bytes(32)
        with db:
            db.execute(
                "INSERT OR IGNORE INTO meta VALUES('secret', ?)", (secret.hex(),)
            )
        row = db.execute("SELECT value FROM meta WHERE key='secret'").fetchone()
        return bytes.fromhex(row[0])
    finally:
        db.close()


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
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=1.0)
            try:
                db.executescript(_SCHEMA)
                with db:
                    return fn(db)
            finally:
                db.close()

    def _rows(self, sql="", args=()):
        """Read-only lease rows; empty on any problem and never creates state."""
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

    def activate(self, session, *, project_root, root_session=None):
        """Explicitly authorize one native task for capture.

        A repeated healthy activate for the same session preserves its token
        and original ``activated_at`` capture boundary; a paused
        (failure-circuit) lease is NOT silently reset — only an activate after
        revocation starts a fresh boundary. Returns the lease dict.
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
            if row and row[3] >= MAX_FAILURES:
                # Paused by the failure circuit: activate is never a bypass.
                pass
            elif row and row[2] == 1 and row[4] == scope:
                # Healthy re-activation: keep the token and original boundary.
                db.execute(
                    "UPDATE leases SET root_session=COALESCE(?, root_session) "
                    "WHERE session=?",
                    (root_session, session),
                )
            else:
                db.execute(
                    "INSERT OR REPLACE INTO leases VALUES(?,?,?,?,?,?,?)",
                    (
                        session,
                        _token_for(self.path, session),
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

    def deactivate(self, session):
        """Revoke one task's authorization. Returns True when one existed."""
        if not isinstance(session, str) or not session:
            return False
        with self._lock:
            if not self.path.is_file():
                return False

            def op(db):
                cursor = db.execute("DELETE FROM leases WHERE session=?", (session,))
                db.execute("DELETE FROM receipts WHERE session=?", (session,))
                return cursor.rowcount > 0

            return self._write(op)

    # ---------------------------------------------------------- lease checks

    def leases(self):
        """Currently valid lease dicts; empty on any read problem."""
        return self._rows()

    def _match(self, session, token):
        if not isinstance(session, str) or not isinstance(token, str):
            raise ValueError("manual session activation required")
        for lease in self._rows():
            if lease["session"] == session and hmac.compare_digest(
                lease["token"], token
            ):
                return lease
        raise ValueError("session is not manually activated")

    def check(self, session, token=None):
        """Identity check for admitted read/feedback calls."""
        if token is None:
            raise ValueError("manual session activation required")
        return self._match(session, token)

    def capture_lease(self, session, token):
        """Lease check for capture; fails closed on the old lease schema."""
        lease = self._match(session, token)
        if not lease.get("capture_schema"):
            raise ValueError(
                "capture requires an adapter lease with project_root, root_session "
                "and activated_at; the existing lease store predates this schema"
            )
        return lease

    def active_lease(self, session):
        """Valid lease for one native session id, without a token (worker re-check)."""
        if not isinstance(session, str):
            return None
        for lease in self._rows():
            if lease["session"] == session:
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

    # ---------------------------------------------------- operation receipts

    def resolve(self, token):
        """Resolve an operation receipt token to ``(session, kind, identity)``.

        Read-only; unknown or already-failed tokens resolve to None and never
        authorize anything on their own."""
        if not isinstance(token, str) or len(token) > 128:
            return None
        if not self.path.is_file():
            return None
        try:
            db = sqlite3.connect(
                self.path.as_uri() + "?mode=ro", uri=True, timeout=0.1
            )
        except sqlite3.Error:
            return None
        try:
            row = db.execute(
                "SELECT session, kind, identity, status FROM receipts WHERE token=?",
                (token,),
            ).fetchone()
        except sqlite3.Error:
            return None
        finally:
            db.close()
        if row is None or row[3] != "open":
            return None
        return {"session": row[0], "kind": row[1], "identity": row[2]}

    def claim(self, session, kind, identity, token=None):
        """Record one in-flight operation (an adapter wrapper hook/update/etc.)
        for an activated task and return its receipt token. A genuine retry of
        the same operation returns the same token; a finished one may be
        claimed again. Claims by unactivated sessions are refused."""
        if self.active_lease(session) is None:
            raise ValueError("session is not manually activated")
        for value, name in ((kind, "kind"), (identity, "identity")):
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ValueError(f"{name} must be nonempty text")
        receipt = token or hashlib.sha256(
            f"{session}\0{kind}\0{identity}".encode()
        ).hexdigest()[:32]

        def op(db):
            db.execute(
                "INSERT INTO receipts VALUES(?,?,?,?,'open',?,?) "
                "ON CONFLICT(token) DO NOTHING",
                (receipt, session, kind.strip(), identity.strip(),
                 time.time(), time.time()),
            )
            return receipt

        return self._write(op)

    def finish(self, session, token, succeeded):
        """Record the outcome of a claimed operation. A failed outcome feeds
        the task's failure circuit; a succeeded one leaves it untouched."""
        resolved = self.resolve(token)
        if resolved is None or resolved["session"] != session:
            raise ValueError("unknown or already-closed operation receipt")

        def op(db):
            db.execute(
                "UPDATE receipts SET status=?, updated=? WHERE token=?",
                ("succeeded" if succeeded else "failed", time.time(), token),
            )
            if not succeeded:
                db.execute(
                    "UPDATE leases SET failures=failures+1 WHERE session=?",
                    (session,),
                )

        self._write(op)
