"""Read-only admission against the adapter's lease store.

The adapter owns creating and updating lease rows; core only reads them, and
only ever read-only (``mode=ro``), so a missing store never gets created by a
status check. Capture requires the current lease schema with ``project_root``,
``root_session`` and ``activated_at``: an older missing-column store stays
readable for status but authorizes no capture — there is no anonymous bypass.

The configuration fingerprint covers the static runtime bindings (adapter
config + engine config bytes). The community-sharing generation is reread
from its own file instead, so toggling sharing never invalidates ordinary
plugin use.
"""

import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import time

from .store import session_key

BASE_COLUMNS = {"session", "token", "fingerprint", "expires", "enabled", "failures"}
CAPTURE_COLUMNS = BASE_COLUMNS | {"project_root", "root_session", "activated_at"}


class Admission:
    def __init__(self, adapter_config):
        self.config = Path(adapter_config).absolute()
        self.path = self.config.with_suffix(".sessions.sqlite3")

    def _fingerprint(self):
        raw = self.config.read_bytes()
        config = json.loads(raw)
        return hashlib.sha256(
            raw + b"\0" + Path(config["engine_config"]).read_bytes()
        ).hexdigest()

    def leases(self):
        """Currently valid lease dicts; empty on any read problem."""
        fingerprint = self._fingerprint()
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=0.1)
        try:
            columns = {row[1] for row in db.execute("PRAGMA table_info(leases)")}
            if not BASE_COLUMNS <= columns:
                return []
            rows = db.execute(
                "SELECT * FROM leases WHERE enabled=1 AND expires>? AND failures<3 "
                "AND fingerprint=?",
                (time.time(), fingerprint),
            ).fetchall()
            names = [row[1] for row in db.execute("PRAGMA table_info(leases)")]
            result = []
            for row in rows:
                lease = dict(zip(names, row))
                lease["capture_schema"] = CAPTURE_COLUMNS <= columns
                result.append(lease)
            return result
        finally:
            db.close()

    def _match(self, session, token):
        if not isinstance(session, str) or not isinstance(token, str):
            raise ValueError("manual session activation required")
        try:
            for lease in self.leases():
                if lease["session"] == session and hmac.compare_digest(
                    lease["token"], token
                ):
                    return lease
        except (OSError, ValueError, KeyError, sqlite3.Error):
            pass
        raise ValueError("session is not manually activated")

    def check(self, session, token):
        """Identity check for admitted read/feedback calls."""
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
        try:
            for lease in self.leases():
                if lease["session"] == session:
                    return lease
        except (OSError, ValueError, KeyError, sqlite3.Error):
            pass
        return None

    def allows_hash(self, hashed_session):
        try:
            return any(
                session_key(lease["session"]) == hashed_session
                for lease in self.leases()
            )
        except (OSError, ValueError, KeyError, sqlite3.Error):
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
