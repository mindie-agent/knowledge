"""Component checks for the pre-delivery review. Not native acceptance."""

import json
import sqlite3
import subprocess
import threading
import time

import pytest

from mindie_knowledge.loop.budget import MAX_CHECKPOINT_RESULT
from mindie_knowledge.loop.cli import capture_hook
from mindie_knowledge.loop.diagnostics import _DELIVERY_RECOVERY
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.handoff import request_wake
from mindie_knowledge.loop.store import Store


def _ready(tmp_path):
    from tests.conftest import make_admission, write_settings

    project = tmp_path / "proj"
    project.mkdir()
    settings = tmp_path / "community.json"
    write_settings(settings, enabled=True, roots=[project])
    admission = make_admission(tmp_path, project_root=project)
    Store(tmp_path / "root", "test").close()
    config = tmp_path / "engine.json"
    config.write_text(json.dumps(dict(
        root=str(tmp_path / "root"), domain="test",
        community_config=str(settings), admission_path=str(admission),
    )))
    return config, admission, project, settings


def test_anchor_growth_matches_observed_snapshot_not_new_identity(tmp_path):
    store = Store(tmp_path, "vllm-ascend")
    other = Store(tmp_path, "vllm-ascend")
    try:
        key = "wire.jsonl"
        old = json.dumps({"anchor_len": 145, "dev": 1, "ino": 2})
        new = json.dumps({"anchor_len": 300, "dev": 1, "ino": 2})
        first = store.reserve_region(
            capture_id="first", file_identity=key, identity=old,
            start=0, finish=145, region_digest="a" * 64, observed_cursor=None,
        )
        second = other.reserve_region(
            capture_id="second", file_identity=key, identity=new,
            start=145, finish=300, region_digest="b" * 64,
            observed_cursor={"identity": old, "finish": 145},
        )
        cursor = store.cursor(key)
        assert first and second
        assert cursor["finish"] == 300 and cursor["identity"] == new
    finally:
        other.close()
        store.close()


def test_expected_absence_conflicts_when_another_connection_inserted(tmp_path):
    store = Store(tmp_path, "vllm-ascend")
    other = Store(tmp_path, "vllm-ascend")
    try:
        key = "other.jsonl"
        assert store.cursor(key) is None
        inserted = other.reserve_region(
            capture_id="zero", file_identity=key, identity="same",
            start=0, finish=0, region_digest="0" * 64, observed_cursor=None,
        )
        stale = store.reserve_region(
            capture_id="stale", file_identity=key, identity="same",
            start=0, finish=80, region_digest="c" * 64, observed_cursor=None,
        )
        assert inserted and stale is None
        assert store.cursor(key)["finish"] == 0
    finally:
        other.close()
        store.close()


def test_cursor_conflict_is_not_due_immediately(tmp_path):
    store = Store(tmp_path, "vllm-ascend")
    try:
        captured = store.add_capture(
            root_session="root", session="manual-A", turn="t", transcript=None,
            summary="kept", namespace="codex",
        )
        engine = Engine(store, agent_command=["false"])
        engine._defer_reread(captured["id"])
        row = store.db.execute(
            "SELECT due, eligible, reason FROM continuations WHERE capture_id=?",
            (captured["id"],),
        ).fetchone()
        assert row["reason"] == "cursor-conflict:1"
        assert row["eligible"] == 1 and row["due"] > time.time()
        assert store.due_capture() is None
        for _ in range(4):
            engine._defer_reread(captured["id"])
        row = store.db.execute(
            "SELECT due, eligible, reason FROM continuations WHERE capture_id=?",
            (captured["id"],),
        ).fetchone()
        assert row["reason"] == "cursor-conflict" and row["eligible"] == 0
        assert store.due_capture() is None
    finally:
        store.close()


def test_checkpoint_accepts_local_entry_metadata_past_32kib(tmp_path):
    store = Store(tmp_path, "vllm-ascend")
    try:
        engine = Engine(store, agent_command=["false"])
        ident = "organize:" + "a" * 64 + ":marker"
        engine.budget.reserve(ident, "session", "organize")
        body = "x" * (32 * 1024 + 100)
        assert len(body.encode()) < MAX_CHECKPOINT_RESULT
        engine.budget.checkpoint(ident, body, "{}")
        assert engine.budget.application(ident)["result"] == body
        with pytest.raises(ValueError):
            engine.budget.checkpoint(ident, "y" * (MAX_CHECKPOINT_RESULT + 1), "{}")
    finally:
        store.close()


def test_start_arms_apply_and_does_not_apply_it(tmp_path):
    _config, _admission, _project, settings = _ready(tmp_path)
    store = Store(tmp_path / "root", "test")
    try:
        captured = store.add_capture(
            root_session="root", session="manual-A", turn="t", transcript=None,
            summary="kept", namespace="codex",
        )
        store.mark_capture(captured["id"], "apply-pending", "saved")
        # mark_capture drops continuations for a non-pending status.
        attempt = f"organize:{captured['id']}:marker"
        engine = Engine(
            store, agent_command=["false"], settings_path=str(settings),
        )
        engine.budget.reserve(attempt, "root", "organize")
        engine.budget.checkpoint(attempt, '{"entries":[]}', "{}")
        store.db.execute("DELETE FROM continuations WHERE capture_id=?", (captured["id"],))
        store.db.commit()
        calls = []
        engine._apply_saved = lambda *args, **kwargs: calls.append(args)
        engine.thread.start = lambda: None
        engine.outbox_thread.start = lambda: None
        engine.start()
        assert calls == []
        assert store.continuation_reason(captured["id"]) == "apply-pending"
        assert store.due_application() == attempt
    finally:
        store.close()


def test_notification_without_token_and_locked_admission(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "mindie_knowledge.loop.handoff.request_wake",
        lambda *_a, **_k: dict(
            wake="not-requested", runtime="unavailable", reason="absent", recovery=None,
        ),
    )
    config, admission, project, _settings = _ready(tmp_path)
    transcript = project / "wire.jsonl"
    transcript.write_text("{}\n")
    event = dict(
        hook_event_name="Stop", identity_kind="notification",
        session_id="manual-A", event_id="11111111-1111-4111-8111-111111111111",
        harness="kimi", transcript_path=str(transcript), budget_seconds=0.8,
    )
    accepted = capture_hook(config, event)
    assert accepted["stage"] in {"accepted-local", "accepted-runtime"}
    assert "token" not in accepted
    held = sqlite3.connect(admission)
    held.execute("BEGIN EXCLUSIVE")
    try:
        blocked = capture_hook(config, dict(
            event, event_id="22222222-2222-4222-8222-222222222222",
        ))
    finally:
        held.rollback()
        held.close()
    assert blocked["stage"] == "unavailable"
    assert blocked["reason"] == "admission-unreadable"
    assert "retry this stop" not in (blocked.get("recovery") or "").lower()


def test_recovery_text_does_not_ask_for_a_stop_replay():
    blob = "\n".join(_DELIVERY_RECOVERY.values()).lower()
    assert "retry this stop" not in blob
    assert "replay this stop" in blob


def test_apply_transient_failure_resumes_without_terminal_dormancy(tmp_path):
    """A saved organizer result blocked by a transient local failure keeps its
    persisted backoff and stays eligible — no terminal attempt count parks it
    and no model is replayed."""
    _config, _admission, _project, settings = _ready(tmp_path)
    store = Store(tmp_path / "root", "test")
    try:
        captured = store.add_capture(
            root_session="root", session="manual-A", turn="t", transcript=None,
            summary="kept", namespace="codex",
        )
        store.mark_capture(captured["id"], "apply-pending", "saved")
        engine = Engine(store, agent_command=["false"], settings_path=str(settings))
        attempt = f"organize:{captured['id']}:marker"
        engine.budget.reserve(attempt, "root", "organize")
        payload = json.dumps({"entries": [dict(
            entry_id="e" * 64, title="Transient case", summary="s",
            content="kept body", conditions={}, new=True,
        )]})
        engine.budget.checkpoint(attempt, payload, "{}")

        from mindie_knowledge.loop.store import Store as _Store

        calls = []
        real_create = _Store.create_draft

        def busy_create(*args, **kwargs):
            calls.append(1)
            raise OSError("disk busy")

        store.create_draft = busy_create
        row = store.capture_row(captured["id"])
        engine._apply_saved(attempt, row)
        assert store.continuation_reason(captured["id"]) == "apply-transient:1"
        cont = store.db.execute(
            "SELECT due, eligible FROM continuations WHERE capture_id=?",
            (captured["id"],),
        ).fetchone()
        assert cont["eligible"] == 1 and cont["due"] > time.time()
        # Still apply-pending with the saved result intact; repeated transient
        # faults grow the backoff but never park the valid result.
        engine._apply_saved(attempt, store.capture_row(captured["id"]))
        assert store.continuation_reason(captured["id"]) == "apply-transient:2"
        assert engine.budget.application(attempt)["result"] == payload
        store.create_draft = real_create.__get__(store, _Store)
        engine._apply_saved(attempt, store.capture_row(captured["id"]))
        assert store.capture_row(captured["id"])["status"] == "organized"
        assert engine.budget.application(attempt)["result"] is None  # settled
    finally:
        store.close()


def test_apply_due_counts_activity_and_does_not_start_when_frozen(tmp_path):
    store = Store(tmp_path, "vllm-ascend")
    try:
        captured = store.add_capture(
            root_session="root", session="manual-A", turn="t", transcript=None,
            summary="kept", namespace="codex",
        )
        store.mark_capture(captured["id"], "apply-pending", "saved")
        attempt = f"organize:{captured['id']}:marker"
        engine = Engine(store, agent_command=["false"])
        engine.budget.reserve(attempt, "root", "organize")
        engine.budget.checkpoint(attempt, '{"entries":[{"kept":true}]}', "{}")
        store.schedule_continuation(
            captured["id"], due=0, reason="apply-pending", eligible=1,
        )
        entered = threading.Event()
        release = threading.Event()

        def hold(_attempt_id, _row):
            entered.set()
            release.wait(timeout=2)

        engine._apply_saved = hold
        worker = threading.Thread(target=engine._apply_due)
        worker.start()
        try:
            assert entered.wait(2)
            decision = engine.stop_if_idle()
            assert decision["idle"] is False
            assert decision["activity"] >= 1
        finally:
            release.set()
            worker.join(2)
        assert not worker.is_alive()
        assert engine.status()["activity"] == 0
        assert engine.budget.application(attempt)["result"]

        engine._frozen = True
        calls = []
        engine._apply_saved = lambda *_a, **_k: calls.append(1)
        engine._apply_due()
        assert calls == []
        assert engine.status()["activity"] == 0
        assert engine.budget.application(attempt)["result"]
    finally:
        store.close()


def test_live_pid_in_wake_json_does_not_coalesce(tmp_path, monkeypatch):
    config, _admission, _project, _settings = _ready(tmp_path)
    monkeypatch.setattr(
        "mindie_knowledge.loop.handoff._probe", lambda *_a, **_k: "absent",
    )
    spawned = []

    class _Proc:
        pid = 424242

    live = subprocess.Popen(["sleep", "30"])
    monkeypatch.setattr(
        "mindie_knowledge.loop.handoff.subprocess.Popen",
        lambda *args, **kwargs: spawned.append(args) or _Proc(),
    )
    wake = tmp_path / "root" / "test" / "wake.json"
    wake.write_text(json.dumps({"wake_pid": live.pid, "service_pid": live.pid}))
    try:
        result = request_wake(config, session_id="manual-A", budget_seconds=0.8)
    finally:
        live.kill()
        live.wait(timeout=2)
    assert result["wake"] == "requested"
    assert spawned


def _force_cas_races(store, count):
    """Make the in-transaction base check see a moved draft ``count`` times."""
    real_row = store._row
    races = {"left": count}

    def racing_row(entry_id):
        if store.db.in_transaction and races["left"] > 0:
            row = real_row(entry_id)
            if row is not None and row["draft_revision"]:
                races["left"] -= 1
                return dict(row, draft_revision="0" * 64)
            return row
        return real_row(entry_id)

    store._row = racing_row
    return races


def test_append_cas_exhaustion_is_transient_not_content_failure(tmp_path):
    """A normal concurrent draft update that keeps winning the compare race
    is transient contention, classified for scheduler resume — never an
    invalid-content failure. The single-call bound of three stays."""
    store = Store(tmp_path, "test")
    try:
        doc = store.create_draft(kind="experience", title="Contended case",
                                 summary="s", content="base body", owner="a" * 64)
        races = _force_cas_races(store, 3)
        with pytest.raises(BlockingIOError):  # an OSError: transient class
            store.append_observation(doc["entry_id"], "contended observation",
                                     marker="ab" * 32, producer="a" * 64)
        assert races["left"] == 0  # exactly the bounded three attempts
        assert not store.get(store.ref(doc["entry_id"]))["content"].count("concurrent")
        # Contention over: the same append applies normally, once.
        store._row = store.__class__._row  # restore the real method binding
        del store._row  # instance attribute gone
        updated, appended = store.append_observation(
            doc["entry_id"], "contended observation", marker="ab" * 32,
            producer="a" * 64)
        assert appended
        again, again_appended = store.append_observation(
            doc["entry_id"], "contended observation", marker="ab" * 32,
            producer="a" * 64)
        assert not again_appended and again["revision"] == updated["revision"]
    finally:
        store.close()


def test_apply_saved_cas_contention_recovers_via_existing_backoff(tmp_path):
    """A saved organizer result whose append exhausts the CAS bound resumes
    through the existing persisted apply backoff — never recorded as a
    content failure, and applied exactly once after contention clears."""
    _config, _admission, _project, settings = _ready(tmp_path)
    store = Store(tmp_path / "root", "test")
    try:
        captured = store.add_capture(
            root_session="root", session="manual-A", turn="t", transcript=None,
            summary="kept", namespace="codex",
        )
        store.mark_capture(captured["id"], "apply-pending", "saved")
        engine = Engine(store, agent_command=["false"], settings_path=str(settings))
        attempt = f"organize:{captured['id']}:" + "ab" * 32
        engine.budget.reserve(attempt, "root", "organize")
        opaque = store.opaque_for("root")
        base = store.create_draft(kind="experience", title="Contended saved",
                                  summary="s", content="base body", owner=opaque)
        payload = json.dumps({"entries": [
            dict(entry_id=base["entry_id"], title=None, summary="s",
                 content="observation one", conditions={}),
        ]})
        engine.budget.checkpoint(attempt, payload, "{}")
        races = _force_cas_races(store, 3)
        engine._apply_saved(attempt, store.capture_row(captured["id"]))
        assert store.continuation_reason(captured["id"]) == "apply-transient:1"
        cont = store.db.execute(
            "SELECT due, eligible FROM continuations WHERE capture_id=?",
            (captured["id"],),
        ).fetchone()
        assert cont["eligible"] == 1 and cont["due"] > time.time()
        assert engine.budget.application(attempt)["result"] == payload
        # The saved result is intact; no entry was half-applied.
        assert "observation one" not in store.get(store.ref(base["entry_id"]))["content"]
        # Contention cleared: the same saved result applies exactly once.
        del store._row
        engine._apply_saved(attempt, store.capture_row(captured["id"]))
        assert store.capture_row(captured["id"])["status"] == "organized"
        body = store.get(store.ref(base["entry_id"]))["content"]
        assert "observation one" in body
        assert engine.budget.application(attempt)["result"] is None
    finally:
        store.close()
