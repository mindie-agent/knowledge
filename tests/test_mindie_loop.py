"""Behavioral checks for the rewritten knowledge loop: gates, drafts, votes,
outbox coalescing and the bounded MCP/transport surface. Runner fixtures are
controlled mechanism doubles, never evidence of model quality."""

import json
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from mindie_knowledge.loop import settings as settings_mod
from mindie_knowledge.loop.cli import capture_hook
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store, canonical, session_key
from mindie_knowledge.loop.transport import Service

from conftest import make_admission, write_settings

PRODUCER = "a" * 64


def draft(store, title="ACL graph investigation", content="Compare eager first.",
          summary="Narrow the execution mode before debugging capture."):
    return store.create_draft(
        kind="experience", title=title, summary=summary, content=content,
        producers=[PRODUCER],
    )


def test_stable_id_revisions_and_pinned_reads(store):
    doc = draft(store)
    updated, appended = store.append_observation(
        doc["entry_id"], "Later: graph mode also fails on multi-device.",
        marker="b" * 32, producer=PRODUCER,
    )
    assert appended and updated["entry_id"] == doc["entry_id"]
    again, appended = store.append_observation(
        doc["entry_id"], "ignored", marker="b" * 32, producer=PRODUCER
    )
    assert not appended and again["revision"] == updated["revision"]
    old = store.get(store.ref(doc["entry_id"], doc["revision"]))
    assert old["content"] == doc["content"]  # old ref stays fixed
    assert "Later:" in store.get(store.ref(doc["entry_id"]))["content"]
    with pytest.raises(ValueError, match="producing task"):
        store.append_observation(doc["entry_id"], "foreign", marker="c" * 32,
                                 producer="d" * 64)


def test_vote_replaces_per_root_and_stays_opaque(store):
    doc = draft(store)
    first = store.record_vote(root_hash=session_key("root-1"), ref=doc["entry_id"],
                              rating="up", reason="helped", publishable=False)
    second = store.record_vote(root_hash=session_key("root-1"), ref=doc["entry_id"],
                               rating="down", reason="stale", publishable=True)
    assert first["vote_id"] == second["vote_id"]
    assert first["root_id"] == second["root_id"] != session_key("root-1")
    votes = store.unbatched_votes()
    assert len(votes) == 1 and votes[0]["rating"] == "down"
    other = store.record_vote(root_hash=session_key("root-2"), ref=doc["entry_id"],
                              rating="up", reason="", publishable=True)
    assert other["vote_id"] != first["vote_id"]
    assert len(store.unbatched_votes()) == 2


@pytest.fixture
def gated(tmp_path):
    """Enabled settings + admission lease + fixture runner engine."""
    project = tmp_path / "proj"
    project.mkdir()
    settings = write_settings(tmp_path / "community.json", enabled=True,
                              roots=[project])
    adapter = make_admission(tmp_path, project_root=project)
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import json,sys\n"
        "p=json.load(sys.stdin)\n"
        "print(json.dumps({'entries':[{'entry_id':None,'title':'Device mapping',"
        "'summary':'Container logical ids restart at zero.',"
        "'content':p['increment'][:400],'conditions':{},'sources':[]}]}))\n"
    )
    store = Store(tmp_path / "store", "test")
    from mindie_knowledge.loop.activation import Admission

    engine = Engine(
        store,
        agent_command=[sys.executable, str(runner)],
        settings_path=tmp_path / "community.json",
        admission=Admission(adapter),
    )
    yield store, engine, settings, adapter
    store.close()


def test_community_off_produces_zero_capture_state(tmp_path):
    settings_path = tmp_path / "community.json"
    write_settings(settings_path, enabled=False, roots=[tmp_path])
    adapter = make_admission(tmp_path, project_root=tmp_path)
    store = Store(tmp_path / "store", "test")
    from mindie_knowledge.loop.activation import Admission

    engine = Engine(
        store, agent_command=[sys.executable, "-c", "import sys; sys.exit(3)"],
        settings_path=settings_path, admission=Admission(adapter),
    )
    result = engine.capture(session_id="manual-A", turn_id="t1",
                            summary="should never be processed")
    assert result["status"] == "skipped"
    assert engine.queue.empty()
    status = store.status()
    assert status["captures"] == [] and status["entries"] == {}
    store.close()


def test_capture_organizes_draft_from_summary_without_a_query(gated):
    store, engine, _, _ = gated
    result = engine.capture(session_id="manual-A", turn_id="t1",
                            summary="Mapped physical device 8; container uses logical 0.")
    assert result["status"] == "queued"
    engine._process(result["id"])
    row = store.capture_row(result["id"])
    assert row["status"] == "organized", row["detail"]
    hits = store.query("device mapping")["results"]
    assert hits and hits[0]["origin"] == "draft"
    assert "logical 0" in store.get(hits[0]["ref"])["content"]


def test_two_turn_increment_keeps_failure_detail(gated, tmp_path):
    store, engine, _, _ = gated
    rollout = tmp_path / "rollout.jsonl"
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def line(payload):
        return json.dumps({"timestamp": stamp, "type": "response_item",
                           "payload": payload}) + "\n"

    with open(rollout, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"type": "session_meta",
                            "payload": {"id": "manual-A", "cwd": "/x"}}) + "\n")
        f.write(line({"type": "message", "role": "user",
                      "content": [{"type": "input_text", "text": "fix mapping"}]}))
    first = engine.capture(session_id="manual-A", turn_id="t1",
                           transcript_path=str(rollout), summary="")
    assert first["status"] == "queued"
    engine._process(first["id"])
    with open(rollout, "a", encoding="utf-8", newline="\n") as f:
        f.write(line({"type": "message", "role": "assistant", "channel": "final",
                      "content": [{"type": "output_text",
                                   "text": "attempt failed: wrong index; corrected to 0"}]}))
    second = engine.capture(session_id="manual-A", turn_id="t2",
                            transcript_path=str(rollout), summary="")
    engine._process(second["id"])
    row = store.capture_row(second["id"])
    assert row["status"] == "organized", row["detail"]
    assert store.coverage_gaps() == []
    cursor_rows = list(store.db.execute("SELECT * FROM cursors"))
    assert len(cursor_rows) == 1 and cursor_rows[0]["finish"] > 0


def test_failed_region_is_consumed_and_never_replayed(gated, tmp_path):
    store, engine, settings_path, _ = gated
    engine.agent_command = [sys.executable, "-c", "import sys; sys.exit(1)"]
    rollout = tmp_path / "rollout.jsonl"
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with open(rollout, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"timestamp": stamp, "type": "response_item",
                            "payload": {"type": "message", "role": "user",
                                        "content": [{"type": "input_text",
                                                     "text": "investigate"}]}}) + "\n")
    result = engine.capture(session_id="manual-A", turn_id="t1",
                            transcript_path=str(rollout), summary="")
    engine._process(result["id"])
    row = store.capture_row(result["id"])
    assert row["status"] == "failed"
    gaps = store.coverage_gaps()
    assert len(gaps) == 1 and gaps[0]["status"] == "failed"
    # A later Stop starts past the failed region; it is not reread.
    with open(rollout, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"timestamp": stamp, "type": "response_item",
                            "payload": {"type": "message", "role": "user",
                                        "content": [{"type": "input_text",
                                                     "text": "new material"}]}}) + "\n")
    engine.agent_command = [sys.executable, "-c", "print('{\"entries\":[]}')"]
    second = engine.capture(session_id="manual-A", turn_id="t2",
                            transcript_path=str(rollout), summary="")
    engine._process(second["id"])
    assert store.capture_row(second["id"])["status"] == "organized"
    assert store.coverage_gaps()[0]["finish"] == gaps[0]["finish"]


def test_disable_while_queued_cancels_without_model(gated, tmp_path):
    store, engine, settings, _ = gated
    result = engine.capture(session_id="manual-A", turn_id="t1", summary="work")
    write_settings(tmp_path / "community.json", enabled=False, roots=[tmp_path])
    engine._process(result["id"])
    assert store.capture_row(result["id"])["status"] == "cancelled"
    assert engine.budget.status()["calls_last_hour"] == 0


def test_outbox_coalesces_and_disable_cancels_unsent(gated, tmp_path):
    store, engine, _, _ = gated
    doc = draft(store)
    store.record_vote(root_hash=session_key("root-1"), ref=doc["entry_id"],
                      rating="up", reason="", publishable=True)
    from mindie_knowledge.loop.export import build_batch
    from mindie_knowledge.loop.store import digest as _digest

    def test_revision_fn(files, domain, base_commit, entry_refs):
        # Clearly labeled mechanism double for the community-owned digest.
        return _digest({"files": sorted((f["path"], f["sha256"]) for f in files),
                        "domain": domain, "base_commit": base_commit,
                        "entry_refs": sorted(entry_refs)})

    built = build_batch(store, settings=settings_mod.load(tmp_path / "community.json"),
                        revision_fn=test_revision_fn)
    batch_id, revision, batch, ids, votes = built
    paths = {f["path"] for f in batch["files"]}
    assert any(p.startswith("cases/") for p in paths)
    assert any(p.startswith("feedback/") for p in paths)
    assert (store.root / "outbox" / "staging" / batch_id).is_dir()
    assert store.drafts_changed() == [] and store.unbatched_votes() == []
    with pytest.raises(ValueError, match="invalid batch status"):
        store.mark_batch(batch_id, "maybe")
    # Forced shutdown keeps an unattempted batch pending; disable cancels it.
    assert store.outbox_pending()[0]["batch_id"] == batch_id
    engine._cancel_unsent("sharing disabled; unsent work cancelled")
    assert store.batch(batch_id)["status"] == "disabled"
    assert store.unbatched_votes() == []  # no disabled-period backfill


def test_mcp_identity_binding_and_annotations(tmp_path):
    config = tmp_path / "engine.json"
    config.write_text(json.dumps(dict(root=str(tmp_path / "root"), domain="test")))
    calls = [
        dict(jsonrpc="2.0", id=1, method="initialize", params={}),
        dict(jsonrpc="2.0", id=2, method="tools/list", params={}),
        dict(jsonrpc="2.0", id=3, method="tools/call",
             params=dict(name="knowledge_query", arguments=dict(query="graph"))),
        dict(jsonrpc="2.0", id=4, method="tools/call",
             params=dict(name="knowledge_query", arguments=dict(query="graph"),
                         _meta={"threadId": "t1",
                                "x-codex-turn-metadata": {"thread_id": "t2",
                                                          "session_id": "s"}})),
    ]
    completed = subprocess.run(
        [sys.executable, "-m", "mindie_knowledge.loop.cli", "mcp", "--config",
         str(config)],
        input="\n".join(canonical(c) for c in calls) + "\n",
        text=True, capture_output=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    replies = [json.loads(line)["result"] for line in completed.stdout.splitlines()]
    tools = {t["name"]: t for t in replies[1]["tools"]}
    assert set(tools) == {"knowledge_query", "knowledge_explain", "knowledge_feedback"}
    assert tools["knowledge_query"]["annotations"]["readOnlyHint"] is True
    assert tools["knowledge_feedback"]["annotations"]["readOnlyHint"] is False
    for reply in replies[2:]:
        assert reply["isError"] is True
        assert "metadata" in reply["content"][0]["text"]
    assert not (tmp_path / "root").exists()  # discovery started nothing


def test_hook_short_circuits_when_sharing_off(tmp_path):
    engine_config = tmp_path / "engine.json"
    settings_path = tmp_path / "community.json"
    write_settings(settings_path, enabled=False, roots=[tmp_path])
    adapter = make_admission(tmp_path, project_root=tmp_path)
    engine_config.write_text(json.dumps(dict(
        root=str(tmp_path / "root"), domain="test",
        community_config=str(settings_path),
        session_activation=str(adapter),
    )))
    start = time.monotonic()
    capture_hook(engine_config, dict(
        hook_event_name="Stop", session_id="manual-A", turn_id="t",
        mindie_activation="cap-A", last_assistant_message="summary",
    ))
    assert time.monotonic() - start < 1
    assert not (tmp_path / "root").exists()


def test_transport_loopback_and_identity(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    settings_path = tmp_path / "community.json"
    write_settings(settings_path, enabled=True, roots=[project])
    adapter = make_admission(tmp_path, project_root=project)
    store = Store(tmp_path / "store", "test")
    from mindie_knowledge.loop.activation import Admission

    engine = Engine(store, agent_command=None, settings_path=settings_path,
                    admission=Admission(adapter))
    service = Service(engine, admission=Admission(adapter))
    thread = threading.Thread(target=service.http.serve_forever, daemon=True)
    thread.start()
    try:
        doc = store.create_draft(kind="experience", title="Graph capture",
                                 summary="s", content="c", producers=[PRODUCER])
        with pytest.raises(ValueError, match="identity"):
            service.call("query", dict(query="graph"))
        hit = service.call("query", dict(query="graph", _session_id="manual-A",
                                         _session_verified=True))
        assert doc["entry_id"] in hit["results"][0]["ref"]
        assert "@" in hit["results"][0]["ref"]  # refs pin their observed revision
        vote = service.call("feedback", dict(ref=doc["entry_id"], rating="up",
                                             reason="", _session_id="manual-A",
                                             _session_verified=True))
        assert vote["publishable"] is True
        queued = service.call(
            "capture",
            dict(session_id="manual-A", turn_id="t", summary="x",
                 _session_id="manual-A", _activation="cap-A"),
        )
        assert queued["status"] == "queued"  # admission passes; runner is absent
        engine._process(queued["id"])
        assert store.capture_row(queued["id"])["status"] == "discarded"
        assert engine.budget.status()["calls_last_hour"] == 0  # no model attempt
        from mindie_knowledge.loop.transport import rpc

        assert rpc(service.connection, "status", timeout=5)["domain"] == "test"
    finally:
        service.http.shutdown()
        service.http.server_close()
        store.close()
