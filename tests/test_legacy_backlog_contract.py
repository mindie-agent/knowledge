"""Old maintenance-paused backlog versus every other parked state.

The fixture builds the pre-migration rows: state.maintenance_paused plus
continuations with reason maintenance-paused and eligible 0. Reopening the
real Store is the migration. The worker is Engine._process / _apply_due,
not a flag edit. Saved model results must be applied without another spawn.
"""

import json
import sys

from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.budget import MaintenanceBudget
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store, canonical, new_identity

from conftest import write_settings


def _runner(path, marker):
    path.write_text(
        "import pathlib, json, sys\n"
        f"mark = pathlib.Path({str(marker)!r})\n"
        "mark.write_text((mark.read_text() if mark.exists() else '') + 'x\\n')\n"
        "payload = json.load(sys.stdin)\n"
        "text = payload.get('increment', '')[:400]\n"
        "print(json.dumps({'entries':[{'entry_id':None,'title':'Backlog',"
        "'summary':'legacy pause outcome','content':text,'conditions':{}}]}))\n"
    )


def _engine(store, settings, admission, runner):
    return Engine(
        store,
        agent_command=[sys.executable, str(runner)],
        settings_path=settings,
        admission=Admission(admission),
    )


def _continuations(store):
    with store.lock:
        return {
            row[0]: (row[1], row[2])
            for row in store.db.execute(
                "SELECT capture_id, reason, eligible FROM continuations"
            )
        }


def _park(store, capture_id, reason):
    store.dormant_capture(capture_id, reason=reason)


def _latch(store):
    with store._write_txn():
        store.db.execute(
            "INSERT OR REPLACE INTO state VALUES('maintenance_paused', 'legacy')"
        )


def _docs(store):
    with store.lock:
        return [row[0] for row in store.db.execute("SELECT doc FROM entries")]


def _drain(engine, store, limit=8):
    seen = []
    for _ in range(limit):
        ident = store.due_capture()
        if ident:
            seen.append(("process", ident))
            engine._process(ident)
            continue
        if store.due_application():
            seen.append(("apply", store.due_application()))
            engine._apply_due()
            continue
        break
    return seen


def test_only_paused_backlog_becomes_due_and_is_idempotent(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    settings = tmp_path / "community.json"
    write_settings(settings, enabled=True, roots=[project])
    admission_path = tmp_path / "admission.sqlite3"
    admission = Admission(admission_path)
    for session in ("ses-pause", "ses-revoked", "ses-tail", "ses-done", "ses-off"):
        admission.activate(session, project_root=str(project))
    store = Store(tmp_path / "store", "test")
    marker = tmp_path / "spawns"
    runner = tmp_path / "runner.py"
    _runner(runner, marker)
    engine = _engine(store, settings, admission_path, runner)

    def capture(session, turn, summary):
        row = engine.capture(session_id=session, turn_id=turn, summary=summary)
        assert row["status"] == "queued", row
        return row["id"]

    paused = capture("ses-pause", "t-pause", "PAUSECTRL only the old latch")
    revoked = capture("ses-revoked", "t-revoked", "REVOKEDCTRL must stay inactive")
    tail = capture("ses-tail", "t-tail", "TAILCTRL other dormant reason")
    done = capture("ses-done", "t-done", "DONECTRL already organized")
    disabled = capture("ses-off", "t-off", "OFFCTRL explicit disable")
    engine._process(done)
    assert store.capture_row(done)["status"] == "organized"
    before_docs = len(_docs(store))
    assert "DONECTRL" in "\n".join(_docs(store))
    pre_spawns = marker.read_text().count("x")

    _park(store, paused, "maintenance-paused")
    _park(store, revoked, "maintenance-paused")
    _park(store, tail, "incomplete-tail")
    store.mark_capture(disabled, "cancelled", "explicitly disabled")
    _latch(store)
    admission.deactivate("ses-revoked")
    root = store.root.parent
    domain = store.domain
    store.close()

    migrated = Store(root, domain)
    try:
        with migrated.lock:
            assert (
                migrated.db.execute(
                    "SELECT 1 FROM state WHERE key='maintenance_paused'"
                ).fetchone()
                is None
            )
        flags = _continuations(migrated)
        assert flags[paused][0] == "maintenance-paused" and flags[paused][1] == 1
        assert flags[revoked] == ("maintenance-paused", 1)
        assert flags[tail] == ("incomplete-tail", 0)
        assert disabled not in flags
        assert done not in flags
        assert migrated.capture_row(disabled)["status"] == "cancelled"
        assert migrated.capture_row(done)["status"] == "organized"
    finally:
        migrated.close()

    again = Store(root, domain)
    try:
        assert _continuations(again) == flags
        with again.lock:
            assert (
                again.db.execute(
                    "SELECT 1 FROM state WHERE key='maintenance_paused'"
                ).fetchone()
                is None
            )
    finally:
        again.close()

    marker.write_text("")
    reopened = Store(root, domain)
    try:
        worker = _engine(reopened, settings, admission_path, runner)
        handled = _drain(worker, reopened)
        touched = {ident for _kind, ident in handled}
        assert paused in touched
        assert revoked in touched
        assert tail not in touched
        assert done not in touched
        assert disabled not in touched
        assert reopened.capture_row(paused)["status"] == "organized"
        assert "PAUSECTRL" in "\n".join(_docs(reopened))
        assert reopened.capture_row(revoked)["status"] == "cancelled"
        assert "REVOKEDCTRL" not in "\n".join(_docs(reopened))
        assert reopened.capture_row(tail)["status"] == "pending"
        assert _continuations(reopened)[tail] == ("incomplete-tail", 0)
        assert reopened.capture_row(done)["status"] == "organized"
        assert reopened.capture_row(disabled)["status"] == "cancelled"
        assert "\n".join(_docs(reopened)).count("DONECTRL") == 1
        assert "\n".join(_docs(reopened)).count("PAUSECTRL") == 1
        # One post-migration model spawn: the paused summary. Revoked work
        # and the already organized capture do not spawn.
        assert marker.read_text().count("x") == 1
        assert len(_docs(reopened)) == before_docs + 1
        second = _drain(worker, reopened)
        assert second == []
        assert marker.read_text().count("x") == 1
        assert "\n".join(_docs(reopened)).count("PAUSECTRL") == 1
        assert pre_spawns >= 1
    finally:
        reopened.close()


def test_saved_result_under_the_old_latch_is_applied_without_a_model(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    settings = tmp_path / "community.json"
    write_settings(settings, enabled=True, roots=[project])
    admission_path = tmp_path / "admission.sqlite3"
    Admission(admission_path).activate("ses-saved", project_root=str(project))
    store = Store(tmp_path / "store", "test")
    marker = tmp_path / "spawns"
    runner = tmp_path / "runner.py"
    _runner(runner, marker)
    engine = _engine(store, settings, admission_path, runner)
    captured = engine.capture(
        session_id="ses-saved", turn_id="t-saved", summary="ignored once a result exists"
    )
    attempt = f"organize:{captured['id']}:legacy"
    entry_id = new_identity()
    result = {
        "entries": [
            {
                "entry_id": entry_id,
                "new": True,
                "title": "Saved",
                "summary": "reused result",
                "content": "SAVEDCTRL reused observation",
                "conditions": {},
            }
        ]
    }
    receipt = {"items": [], "notes": [], "refs": [], "more": False}
    budget = MaintenanceBudget(store)
    budget.reserve(attempt, captured["id"], "organize")
    budget.checkpoint(
        attempt,
        canonical(result),
        canonical(receipt),
        capture_id=captured["id"],
        capture_status="apply-pending",
    )
    with store._write_txn():
        store.db.execute(
            "UPDATE continuations SET due=?, reason='maintenance-paused', eligible=0 "
            "WHERE capture_id=?",
            (0, captured["id"]),
        )
        store.db.execute(
            "INSERT OR REPLACE INTO state VALUES('maintenance_paused', 'legacy')"
        )
    assert store.due_capture() is None
    assert store.due_application() is None
    root = store.root.parent
    domain = store.domain
    store.close()

    reopened = Store(root, domain)
    try:
        assert reopened.due_capture() is None
        assert reopened.due_application() == attempt
        worker = _engine(reopened, settings, admission_path, runner)
        handled = _drain(worker, reopened)
        assert handled == [("apply", attempt)]
        assert not marker.exists()
        docs = "\n".join(_docs(reopened))
        assert docs.count("SAVEDCTRL") == 1
        assert reopened.capture_row(captured["id"])["status"] == "organized"
        assert reopened.due_application() is None
        assert _drain(worker, reopened) == []
        assert "\n".join(_docs(reopened)).count("SAVEDCTRL") == 1
        assert not marker.exists()
    finally:
        reopened.close()
