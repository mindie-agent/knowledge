"""Manual entry, automatic association and consent are distinct authorities."""
import sqlite3

from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.store import session_key


def test_automatic_binding_cannot_enable_any_knowledge_flywheel(tmp_path):
    gate = Admission(tmp_path / "admission.sqlite3")
    assert gate.knowledge_lease("chat") is None
    assert not list(tmp_path.iterdir())
    automatic = gate.associate("chat", project_root=str(tmp_path), not_before=1.0)
    assert gate.knowledge_lease("chat") is None
    assert not gate.allows_hash(session_key("chat"))
    assert gate.capture_authorization("chat", automatic["token"])["state"] == "inactive"
    assert gate.resolve_capture_lease("chat")["state"] == "inactive"

    manual = gate.activate("chat", project_root=str(tmp_path))
    assert manual["token"] != automatic["token"]
    assert manual["activated_at"] > automatic["activated_at"]
    assert gate.knowledge_lease("chat")["token"] == manual["token"]
    assert Admission(gate.path).knowledge_lease("chat")["token"] == manual["token"]
    assert gate.capture_authorization("chat", manual["token"])["state"] == "admitted"
    assert gate.resolve_capture_lease("chat")["state"] == "admitted"
    repeated = gate.associate("chat", project_root=str(tmp_path), not_before=2.0)
    assert repeated["token"] == manual["token"]
    assert gate.knowledge_lease("child") is None
    gate.deactivate("chat")
    assert gate.knowledge_lease("chat") is None
    assert gate.capture_authorization("chat", manual["token"])["state"] == "inactive"
    assert not gate.allows_hash(session_key("chat"))


def test_legacy_binding_needs_new_manual_entry_without_read_side_migration(tmp_path):
    path = tmp_path / "admission.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE leases(session TEXT PRIMARY KEY, token TEXT NOT NULL,
                enabled INTEGER NOT NULL, failures INTEGER NOT NULL,
                project_root TEXT, root_session TEXT, activated_at REAL);
            CREATE TABLE attempts(session TEXT NOT NULL, kind TEXT NOT NULL,
                identity TEXT NOT NULL, created REAL NOT NULL,
                PRIMARY KEY(session, kind, identity));
        """)
        db.execute("INSERT INTO leases VALUES(?,?,?,?,?,?,?)",
                   ("legacy", "old-token", 1, 0, str(tmp_path), "legacy", 1.0))
    gate = Admission(path)
    assert gate.knowledge_lease("legacy") is None
    assert gate.resolve_capture_lease("legacy")["state"] == "inactive"
    with sqlite3.connect(path) as db:
        assert "entry_enabled" not in {row[1] for row in db.execute("PRAGMA table_info(leases)")}
    manual = gate.activate("legacy", project_root=str(tmp_path))
    assert manual["token"] != "old-token"
    assert gate.knowledge_lease("legacy")["token"] == manual["token"]
