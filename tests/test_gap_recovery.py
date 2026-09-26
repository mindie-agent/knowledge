"""Bounded deadline-gap recovery (R3): one delayed recovery per failed
region, persisted across restarts; succeeded regions are never re-organized,
saved results never re-applied, and unverifiable sources keep locatable gaps.
Real SQLite, real subprocess runner doubles, no network."""

import json
import sys

import pytest

import transcript_double as transcript_mod
from conftest import make_admission, write_settings

from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store


def _stamp():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _line(payload, stamp):
    return json.dumps({"timestamp": stamp, "type": "response_item",
                       "payload": payload}) + "\n"


def _write_transcript(path, session, texts):
    stamp = _stamp()
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"type": "session_meta", "payload": {"id": session}}) + "\n")
        for text in texts:
            f.write(_line({"type": "message", "role": "user",
                           "content": [{"type": "input_text", "text": text}]}, stamp))


def _append_transcript(path, texts):
    stamp = _stamp()
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for text in texts:
            f.write(_line({"type": "message", "role": "assistant", "channel": "final",
                           "content": [{"type": "output_text", "text": text}]}, stamp))


SUCCESS_RUNNER = (
    "import json,sys\n"
    "p=json.load(sys.stdin)\n"
    "print(json.dumps({'entries':[{'entry_id':None,'title':'Recovered observation',"
    "'summary':'bounded recovery outcome','content':p['increment'][:400],'conditions':{}}]}))\n"
)
DEADLINE_RUNNER = "import sys\nsys.exit(124)\n"


@pytest.fixture
def gated(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    settings = write_settings(tmp_path / "community.json", enabled=True,
                              roots=[project])
    adapter = make_admission(tmp_path, project_root=project)
    runner = tmp_path / "runner.py"
    runner.write_text(SUCCESS_RUNNER)
    spawns = tmp_path / "spawns"
    store = Store(tmp_path / "store", "test")

    def engine():
        return Engine(
            store,
            agent_command=[sys.executable, str(runner)],
            settings_path=tmp_path / "community.json",
            admission=Admission(adapter),
            transcript_adapter=transcript_mod,
        )

    yield dict(store=store, engine=engine, runner=runner, spawns=spawns,
               tmp_path=tmp_path, settings=settings, adapter=adapter,
               project=project)
    store.close()


def _counting_runner(gated, body):
    """Runner that appends one marker line per real process spawn."""
    marker = gated["tmp_path"] / "spawn-count"
    gated["runner"].write_text(
        "import sys\n"
        f"mark = open({str(marker)!r}, 'a'); mark.write('x\\n'); mark.close()\n"
        + body
    )
    return marker


def _fire_due(store, capture_id):
    store.db.execute(
        "UPDATE continuations SET due=0, eligible=1 WHERE capture_id=?",
        (capture_id,),
    )
    store.db.commit()


def _regions(store):
    with store.lock:
        return [dict(r) for r in store.db.execute("SELECT * FROM regions ORDER BY created")]


def test_deadline_gap_recovers_once_after_restart_without_reorganizing(gated):
    """Segment one organizes; segment two hits an explicit deadline; after a
    service restart the persisted recovery fires once, applies the saved
    observation, and never re-calls the model for segment one."""
    store, engine = gated["store"], gated["engine"]()
    marker = _counting_runner(gated, SUCCESS_RUNNER)
    rollout = gated["tmp_path"] / "rollout.jsonl"
    _write_transcript(rollout, "manual-A", ["first investigated mapping " + "x" * 2048])
    first = engine.capture(session_id="manual-A", turn_id="t1",
                           transcript_path=str(rollout), summary="")
    engine._process(first["id"])
    assert store.capture_row(first["id"])["status"] == "organized"
    assert marker.read_text().count("x") == 1

    _append_transcript(rollout, ["second observed device reset " + "y" * 2048])
    second = engine.capture(session_id="manual-A", turn_id="t2",
                            transcript_path=str(rollout), summary="")
    _counting_runner(gated, DEADLINE_RUNNER)
    engine._process(second["id"])
    row = store.capture_row(second["id"])
    assert row["status"] == "pending", row
    assert "gap-recovery:" in (store.continuation_reason(second["id"]) or "")
    gaps = [r for r in _regions(store) if r["status"] == "failed"]
    assert len(gaps) == 1 and gaps[0]["recovery"] == 1
    assert marker.read_text().count("x") == 2  # one attempt per region so far

    # Restart: a fresh engine over the same store; the persisted continuation
    # and recovery counter survive.
    restarted = gated["engine"]()
    _counting_runner(gated, SUCCESS_RUNNER)
    _fire_due(store, second["id"])
    restarted._process(second["id"])
    row = store.capture_row(second["id"])
    assert row["status"] == "organized", row["detail"]
    regions = _regions(store)
    assert {r["status"] for r in regions} == {"succeeded"}
    # Exactly three model spawns total: segment one, the deadline, the recovery.
    assert marker.read_text().count("x") == 3
    # The recovered observation is applied exactly once and is retrievable.
    hits = store.query("device reset")["results"]
    assert hits, store.status()
    assert "device reset" in store.get(hits[0]["ref"])["content"]

    # Nothing is due afterwards: no extra model call, no repeat apply.
    _fire_due(store, second["id"])
    restarted._process(second["id"])
    assert marker.read_text().count("x") == 3


def test_second_recovery_failure_keeps_a_locatable_gap(gated):
    store, engine = gated["store"], gated["engine"]()
    marker = _counting_runner(gated, DEADLINE_RUNNER)
    rollout = gated["tmp_path"] / "rollout.jsonl"
    _write_transcript(rollout, "manual-A", ["doomed segment " + "z" * 2048])
    capture = engine.capture(session_id="manual-A", turn_id="t1",
                             transcript_path=str(rollout), summary="")
    engine._process(capture["id"])
    assert store.capture_row(capture["id"])["status"] == "pending"
    _fire_due(store, capture["id"])
    engine._process(capture["id"])
    row = store.capture_row(capture["id"])
    assert row["status"] == "failed"
    assert "gap retained" in row["detail"]
    gaps = [r for r in _regions(store) if r["status"] == "failed"]
    assert len(gaps) == 1 and gaps[0]["recovery"] == 1
    assert marker.read_text().count("x") == 2  # initial + single recovery
    # No third attempt, ever, even when forced due again.
    _fire_due(store, capture["id"])
    engine._process(capture["id"])
    assert marker.read_text().count("x") == 2
    # Other captures are unaffected by the local gap.
    _counting_runner(gated, SUCCESS_RUNNER)
    _append_transcript(rollout, ["fresh material " + "w" * 2048])
    other = engine.capture(session_id="manual-A", turn_id="t2",
                           transcript_path=str(rollout), summary="")
    engine._process(other["id"])
    assert store.capture_row(other["id"])["status"] == "organized"


def test_replaced_source_aborts_recovery_without_a_model_call(gated):
    store, engine = gated["store"], gated["engine"]()
    marker = _counting_runner(gated, DEADLINE_RUNNER)
    rollout = gated["tmp_path"] / "rollout.jsonl"
    _write_transcript(rollout, "manual-A", ["original segment " + "q" * 2048])
    capture = engine.capture(session_id="manual-A", turn_id="t1",
                             transcript_path=str(rollout), summary="")
    engine._process(capture["id"])
    assert store.capture_row(capture["id"])["status"] == "pending"
    # The transcript is replaced (new inode/anchor): the recorded identity no
    # longer verifies, so the same source range cannot be proven.
    _write_transcript(rollout, "manual-A", ["rewritten history " + "q" * 2048])
    _fire_due(store, capture["id"])
    _counting_runner(gated, SUCCESS_RUNNER)
    engine._process(capture["id"])
    row = store.capture_row(capture["id"])
    assert row["status"] == "failed" and "gap retained" in row["detail"]
    assert marker.read_text().count("x") == 1  # no recovery spawn


def test_disable_before_recovery_cancels_without_a_model_call(gated):
    store, engine = gated["store"], gated["engine"]()
    marker = _counting_runner(gated, DEADLINE_RUNNER)
    rollout = gated["tmp_path"] / "rollout.jsonl"
    _write_transcript(rollout, "manual-A", ["revoked segment " + "v" * 2048])
    capture = engine.capture(session_id="manual-A", turn_id="t1",
                             transcript_path=str(rollout), summary="")
    engine._process(capture["id"])
    assert store.capture_row(capture["id"])["status"] == "pending"
    write_settings(gated["tmp_path"] / "community.json", enabled=False,
                   roots=[gated["project"]])
    _fire_due(store, capture["id"])
    _counting_runner(gated, SUCCESS_RUNNER)
    engine._process(capture["id"])
    assert store.capture_row(capture["id"])["status"] == "cancelled"
    assert marker.read_text().count("x") == 1


def test_crash_between_schedule_and_recovery_still_attempts_exactly_once(gated):
    """recovery=1 persists before the second attempt: a simulated crash that
    leaves the recovery continuation due must not earn a third model call."""
    store, engine = gated["store"], gated["engine"]()
    marker = _counting_runner(gated, DEADLINE_RUNNER)
    rollout = gated["tmp_path"] / "rollout.jsonl"
    _write_transcript(rollout, "manual-A", ["crash window " + "u" * 2048])
    capture = engine.capture(session_id="manual-A", turn_id="t1",
                             transcript_path=str(rollout), summary="")
    engine._process(capture["id"])
    # Crash after the recovery attempt was consumed but before its outcome
    # landed: the attempt row stays 'running' until service-start recovery.
    _fire_due(store, capture["id"])
    engine._process(capture["id"])  # recovery attempt also deadlines
    assert store.capture_row(capture["id"])["status"] == "failed"
    restarted = gated["engine"]()
    restarted.budget.recover_interrupted()
    _fire_due(store, capture["id"])
    restarted._process(capture["id"])
    assert marker.read_text().count("x") == 2


def test_sequence_restart_duplicate_event_and_recovery_converges(gated):
    """接收→首段成功→第二段 deadline→重启→重复事件→恢复: after convergence the
    recovered content exists exactly once, no work is unfinished, the external
    side effect (applied observation) happened once, and model calls are exact."""
    store, engine = gated["store"], gated["engine"]()
    marker = _counting_runner(gated, SUCCESS_RUNNER)
    rollout = gated["tmp_path"] / "rollout.jsonl"
    _write_transcript(rollout, "manual-A", ["alpha mapped device ids " + "a" * 2048])
    first = engine.capture(session_id="manual-A", turn_id="t1",
                           transcript_path=str(rollout), summary="")
    engine._process(first["id"])
    assert store.capture_row(first["id"])["status"] == "organized"

    _append_transcript(rollout, ["beta observed reset window " + "b" * 2048])
    _counting_runner(gated, DEADLINE_RUNNER)
    second = engine.capture(session_id="manual-A", turn_id="t2",
                            transcript_path=str(rollout), summary="")
    engine._process(second["id"])
    assert "gap-recovery:" in (store.continuation_reason(second["id"]) or "")

    # Restart, then the SAME Stop event is delivered again (at-least-once).
    restarted = gated["engine"]()
    duplicate = restarted.capture(session_id="manual-A", turn_id="t2",
                                  transcript_path=str(rollout), summary="")
    assert duplicate["duplicate"] is True and duplicate["id"] == second["id"]
    # The repeat neither creates work nor re-arms the scheduled recovery early.
    assert "gap-recovery:" in (store.continuation_reason(second["id"]) or "")
    assert marker.read_text().count("x") == 2

    _counting_runner(gated, SUCCESS_RUNNER)
    _fire_due(store, second["id"])
    restarted._process(second["id"])
    assert store.capture_row(second["id"])["status"] == "organized"
    # Converged state: beta applied exactly once; nothing unfinished; 3 model calls.
    hits = store.query("reset window")["results"]
    assert hits
    body = store.get(hits[0]["ref"])["content"]
    assert body.count("beta observed reset window") == 1
    with store.lock:
        unfinished = store.db.execute(
            "SELECT count(*) FROM captures WHERE status IN "
            "('queued','pending','deferred','processing','apply-pending')"
        ).fetchone()[0]
    assert unfinished == 0
    assert marker.read_text().count("x") == 3
    # A third identical Stop still changes nothing and spawns no model.
    again = restarted.capture(session_id="manual-A", turn_id="t2",
                              transcript_path=str(rollout), summary="")
    assert again["duplicate"] is True
    assert marker.read_text().count("x") == 3


def test_non_deadline_failures_do_not_schedule_recovery(gated):
    store, engine = gated["store"], gated["engine"]()
    _counting_runner(gated, "import sys\nsys.exit(1)\n")  # category: unknown
    rollout = gated["tmp_path"] / "rollout.jsonl"
    _write_transcript(rollout, "manual-A", ["plain failure " + "t" * 2048])
    capture = engine.capture(session_id="manual-A", turn_id="t1",
                             transcript_path=str(rollout), summary="")
    engine._process(capture["id"])
    row = store.capture_row(capture["id"])
    assert row["status"] == "failed"
    assert store.continuation_reason(capture["id"]) is None
    gaps = [r for r in _regions(store) if r["status"] == "failed"]
    assert len(gaps) == 1 and gaps[0]["recovery"] == 0
