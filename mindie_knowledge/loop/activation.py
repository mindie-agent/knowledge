"""Optional adapter-issued session admission for shared knowledge services."""

import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import time

from .store import session_key


class SessionAdmission:
    def __init__(self, adapter_config):
        self.config = Path(adapter_config).absolute()
        self.path = self.config.with_suffix(".sessions.sqlite3")

    def rows(self):
        raw = self.config.read_bytes()
        config = json.loads(raw)
        fingerprint = hashlib.sha256(
            raw + b"\0" + Path(config["engine_config"]).read_bytes()
        ).hexdigest()
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=0.1)
        try:
            return list(
                db.execute(
                    "SELECT session,token FROM leases WHERE enabled=1 AND expires>? AND failures<3 AND fingerprint=?",
                    (time.time(), fingerprint),
                )
            )
        finally:
            db.close()

    def require(self, session, token):
        if not isinstance(session, str) or not isinstance(token, str):
            raise ValueError("manual session activation required")
        try:
            if any(
                s == session and hmac.compare_digest(t, token) for s, t in self.rows()
            ):
                return
        except (OSError, ValueError, KeyError, sqlite3.Error):
            pass
        raise ValueError("session is not manually activated")

    def allows(self, hashed_session):
        try:
            return any(session_key(s) == hashed_session for s, _ in self.rows())
        except (OSError, ValueError, KeyError, sqlite3.Error):
            return False
