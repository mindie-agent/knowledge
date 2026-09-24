"""Stop handoff, partial reads, and apply checkpoint. Not native acceptance."""

import json
import sqlite3
import time

import pytest

from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.cli import capture_hook
from mindie_knowledge.loop.documents import DraftFull
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store


@pytest.fixture(autouse=True)
def no_real_wake(monkeypatch):
    monkeypatch.setattr(
        "mindie_knowledge.loop.handoff.request_wake",
        lambda *args, **kwargs: dict(
            wake="not-requested", runtime="unavailable", reason="absent", recovery=None,
        ),
    )


def _config(tmp_path, settings, admission):
    path = tmp_path / "engine.json"
    path.write_text(json.dumps(dict(
        root=str(tmp_path / "root"), domain="test",
        community_config=str(settings), admission_path=str(admission),
    )))
    return path


def _ready(tmp_path, project):
    from tests.conftest import make_admission, write_settings

    settings = tmp_path / "community.json"
    write_settings(settings, enabled=True, roots=[project])
    admission = make_admission(tmp_path, project_root=project)
    store = Store(tmp_path / "root", "test")
    store.close()
    return _config(tmp_path, settings, admission), admission


def _event(admission, **extra):
    from tests.conftest import admission_token

    event = dict(
        hook_event_name="Stop", identity_kind="turn", session_id="manual-A",
        turn_id="turn-1", harness="codex", mindie_activation=admission_token(admission),
        last_assistant_message="a bounded summary", budget_seconds=0.8,
    )
    event.update(extra)
    return event


def test_same_turn_is_one_row_and_sibling_sessions_are_not(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    config, admission = _ready(tmp_path, project)
    Admission(admission).activate("manual-B", project_root=str(project), root_session="manual-A")
    first = capture_hook(config, _event(admission))
    again = capture_hook(config, _event(admission))
    other = capture_hook(config, _event(
        admission, session_id="manual-B", turn_id="turn-1",
        mindie_activation=Admission(admission).active_lease("manual-B")["token"],
    ))
    assert first["stage"] in {"accepted-local", "accepted-runtime"}
    assert again["duplicate"] is True and again["capture_id"] == first["capture_id"]
    assert other["capture_id"] != first["capture_id"]
    store = Store(tmp_path / "root", "test")
    try:
        assert len(store.status()["captures"]) >= 2
    finally:
        store.close()


def test_locked_store_is_not_accepted(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    config, admission = _ready(tmp_path, project)
    path = tmp_path / "root" / "test" / "store-v3.sqlite3"
    held = sqlite3.connect(path)
    held.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        result = capture_hook(config, _event(admission, budget_seconds=0.4))
    finally:
        held.rollback()
        held.close()
    assert result["stage"] == "unavailable"
    assert result["reason"] == "store-locked"
    assert result["capture_id"] is None
    assert time.monotonic() - started < 1.5
    stored = capture_hook(config, _event(admission))
    assert stored["capture_id"]


def test_overlong_summary_keeps_the_transcript_reference(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    config, admission = _ready(tmp_path, project)
    transcript = project / "turn.jsonl"
    transcript.write_text("{}\n")
    kept = capture_hook(config, _event(
        admission, transcript_path=str(transcript),
        last_assistant_message="x" * 32769,
    ))
    refused = capture_hook(config, _event(
        admission, turn_id="turn-2", last_assistant_message="y" * 32769,
    ))
    assert kept["summary_dropped"] is True and kept["capture_id"]
    assert refused["stage"] == "rejected" and refused["capture_id"] is None
    store = Store(tmp_path / "root", "test")
    try:
        row = store.capture_row(kept["capture_id"])
        assert row["summary"] == ""
        assert row["transcript"] == str(transcript)
    finally:
        store.close()


def test_reactivated_epoch_cancels_the_queued_turn(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    config, admission = _ready(tmp_path, project)
    transcript = project / "turn.jsonl"
    transcript.write_text('{"type":"session_meta"}\n')
    accepted = capture_hook(config, _event(admission, transcript_path=str(transcript)))
    gate = Admission(admission)
    gate.deactivate("manual-A")
    gate.activate("manual-A", project_root=str(project))
    store = Store(tmp_path / "root", "test")
    try:
        from mindie_knowledge.loop import settings as settings_mod

        engine = Engine(
            store, agent_command=[__import__("sys").executable, "-c", "print('{}')"],
            settings_path=str(tmp_path / "community.json"),
            admission=Admission(admission),
        )
        calls = []
        engine.transcript = type("P", (), {
            "identify": staticmethod(lambda *_a, **_k: None),
            "read_material": staticmethod(lambda *_a, **_k: calls.append(True)),
        })()
        engine._process(accepted["capture_id"])
        assert calls == []
        assert store.capture_row(accepted["capture_id"])["status"] == "cancelled"
    finally:
        store.close()


def test_partial_tail_is_not_no_new_material(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    config, admission = _ready(tmp_path, project)
    accepted = capture_hook(config, _event(admission, transcript_path=str(project / "t.jsonl")))
    store = Store(tmp_path / "root", "test")
    try:
        engine = Engine(store, agent_command=["false"], settings_path=str(tmp_path / "community.json"),
                        admission=Admission(admission))

        def read_material(*_a, **_k):
            return dict(status="unchanged", start=0, end=0, partial=True, more=True, text="", digest="0" * 64)

        engine.transcript = type("P", (), {
            "identify": staticmethod(lambda _path: None),
            "read_material": staticmethod(read_material),
        })()
        engine._process(accepted["capture_id"])
        row = store.capture_row(accepted["capture_id"])
        assert row["status"] == "pending"
        assert "no-new-material" not in (row["detail"] or "")
        assert store.continuation_reason(accepted["capture_id"]).startswith("partial-tail:")
    finally:
        store.close()


def test_apply_failure_keeps_the_scanned_result(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    config, admission = _ready(tmp_path, project)
    accepted = capture_hook(config, _event(admission))
    store = Store(tmp_path / "root", "test")
    script = tmp_path / "agent.py"
    script.write_text(
        "import json,sys\nsys.stdin.read()\n"
        "print(json.dumps({'entries':[{'entry_id':None,'title':'Case',"
        "'summary':'Short','conditions':{},'content':'Observed rank 0 failed.'}]}))\n"
    )
    try:
        engine = Engine(
            store, agent_command=[__import__("sys").executable, str(script)],
            settings_path=str(tmp_path / "community.json"), admission=Admission(admission),
        )
        monkeypatch.setattr(store, "create_draft", lambda **_k: (_ for _ in ()).throw(DraftFull("full")))
        engine._process(accepted["capture_id"])
        assert store.capture_row(accepted["capture_id"])["status"] == "apply-pending"
        attempt = engine.budget.pending_applications()
        assert attempt and engine.budget.application(attempt[0])["result"]
        engine._apply_saved(attempt[0], store.capture_row(accepted["capture_id"]))
        assert store.capture_row(accepted["capture_id"])["status"] == "apply-pending"
    finally:
        store.close()
