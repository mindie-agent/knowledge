"""Behavioral checks for the rewritten knowledge loop: gates, drafts, votes,
outbox coalescing and the bounded MCP/transport surface. Runner fixtures are
controlled mechanism doubles, never evidence of model quality."""

import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from mindie_knowledge.loop import settings as settings_mod
from mindie_knowledge.loop.cli import capture_hook
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store, canonical, session_key
from mindie_knowledge.loop.transport import Service

import transcript_double as transcript_mod
from conftest import admission_token, make_admission, write_settings
from package_fixture import install_documents
from material_worker_fixture import command as summary_command
from test_history_import import summary_due

PRODUCER = "a" * 64


def draft(store, title="ACL graph investigation", content="Compare eager first.",
          summary="Narrow the execution mode before debugging capture.",
          generation=None):
    return store.create_draft(
        kind="experience", title=title, summary=summary, content=content,
        owner=PRODUCER, generation=generation,
    )


def _revision_double(files, domain, base_commit, entry_refs):
    from mindie_knowledge.community.batch import batch_revision
    return batch_revision(files, domain, base_commit, entry_refs)


def capture_public(engine, text, turn):
    from datetime import datetime, timezone
    source = Path(engine.settings_path).parent / "public-transcript.jsonl"
    if not source.exists():
        source.write_text(json.dumps(dict(type="session_meta", payload=dict(id="manual-A"))) + "\n")
    with source.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(dict(type="response_item", timestamp=datetime.now(timezone.utc).isoformat(),
            payload=dict(type="message", role="user", content=[dict(type="input_text", text=text)]))) + "\n")
    return engine.capture(session_id="manual-A", turn_id=turn, transcript_path=str(source))


def test_stable_id_latest_draft_and_expired_pins(store):
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
    with pytest.raises(ValueError, match="unknown pinned revision"):
        store.get(store.ref(doc["entry_id"], doc["revision"]))
    assert "Later:" in store.get(store.ref(doc["entry_id"]))["content"]
    with pytest.raises(ValueError, match="owning task"):
        store.append_observation(doc["entry_id"], "foreign", marker="c" * 32,
                                 producer="d" * 64)


def test_same_title_entries_keep_distinct_identities(store):
    first = draft(store, title="Shared symptom title")
    second = draft(store, title="Shared symptom title")
    assert first["entry_id"] != second["entry_id"]
    hits = store.query("Shared symptom title")["results"]
    assert {h["entry_id"] for h in hits} == {
        first["entry_id"], second["entry_id"]
    }


def test_imported_content_cannot_forge_ownership(store):
    from mindie_knowledge.loop.documents import make_entry

    doc = draft(store)
    # A feed body for the same entry arrives; ownership is unaffected.
    foreign = make_entry(
        entry_id=doc["entry_id"], domain="vllm-ascend", kind="experience",
        title="Downloaded entry", summary="Published elsewhere.",
        content="A published body carrying no ownership claim.",
    )
    install_documents(store, [foreign], feed_ident="f" * 64)
    with pytest.raises(ValueError, match="owning task"):
        store.append_observation(doc["entry_id"], "forged update", marker="b" * 32,
                                 producer="d" * 64)
    kept, appended = store.append_observation(
        doc["entry_id"], "legitimate update", marker="b" * 32, producer=PRODUCER,
    )
    assert appended and kept["entry_id"] == doc["entry_id"]


def test_title_is_stable_unless_explicitly_corrected(store):
    doc = draft(store, title="Misleading old title")
    # Ordinary append with a null title preserves the existing title.
    kept, _ = store.append_observation(
        doc["entry_id"], "Later: more evidence.", marker="b" * 32,
        producer=PRODUCER, header={"title": None,
                                   "summary": "Corrected current finding."},
    )
    assert kept["title"] == "Misleading old title"
    assert kept["summary"] == "Corrected current finding."
    # No title key at all also preserves it.
    kept2, _ = store.append_observation(
        doc["entry_id"], "Later: even more evidence.", marker="c" * 32,
        producer=PRODUCER, header={"summary": "Still current."},
    )
    assert kept2["title"] == "Misleading old title"
    # An explicitly supplied corrected title updates the misleading one.
    fixed, _ = store.append_observation(
        doc["entry_id"], "Later: final evidence.", marker="d" * 32,
        producer=PRODUCER, header={"title": "Accurate corrected title"},
    )
    assert fixed["title"] == "Accurate corrected title"
    assert store.get(store.ref(doc["entry_id"]))["title"] == "Accurate corrected title"


def test_query_returns_distinct_full_block_and_feedback_references(store):
    from mindie_knowledge.materials.references import parse_read_ref, parse_feedback_ref

    doc = draft(store)
    updated, _ = store.append_observation(
        doc["entry_id"], "Later: second revision.", marker="b" * 32,
        producer=PRODUCER,
    )
    hit = store.query("ACL graph")["results"][0]
    parsed = parse_read_ref(hit["ref"], domain=store.domain)
    assert parsed["kind"] == "block" and parsed["task_id"] == doc["entry_id"]
    assert len(parsed["block_id"]) == 64 and len(parsed["sha256"]) == 64
    assert parse_feedback_ref(hit["feedback_ref"])["revision"] == updated["revision"]
    assert store.explain(hit["ref"])["current_revision"] == updated["revision"]
    with pytest.raises(ValueError, match="feedback only"):
        store.explain(hit["feedback_ref"])
    with pytest.raises(ValueError, match="reference"):
        store.explain(doc["entry_id"][:16])
    other = store.create_draft(
        kind="experience", title="Collision entry", summary="s",
        content="c", entry_id=doc["entry_id"][:16] + "f" * 48,
    )
    collision = store.query("Collision entry")["results"][0]
    assert parse_read_ref(collision["ref"])["task_id"] == other["entry_id"]


def test_correction_changes_retrieval_header_but_preserves_old_body(store):
    old=draft(store,summary='Prefix cache is the suspected cause.',content='Initial hypothesis: prefix cache.')
    new,_=store.append_observation(old['entry_id'],'Later evidence: workspace underallocated.',
        marker='f'*32,producer=PRODUCER,header={'summary':'Workspace underallocated; prefix hypothesis disproven.'})
    assert new['summary'].startswith('Workspace')
    assert 'Initial hypothesis' in new['content'] and 'Later evidence' in new['content']
    assert store.get(store.ref(old['entry_id']))['summary']==new['summary']


def test_pending_index_uses_saved_material_without_reopening_source(gated, tmp_path):
    store, engine, _, _ = gated
    calls = tmp_path / "summary-calls"
    engine.summary_command = summary_command(calls=calls, title="Device mapping")
    captured = capture_public(engine, "Observed device mapping evidence.", "index-later")
    engine._process(captured["id"])
    assert store.capture_row(captured["id"])["status"] == "organized"
    assert not calls.exists()
    source = tmp_path / "public-transcript.jsonl"
    source.rename(tmp_path / "archived-native.jsonl")
    summary_due(engine)
    assert calls.read_text() == "x"
    assert store.query("Observed device mapping")["results"]
    assert store.db.execute("SELECT summary_status FROM transcript_tasks").fetchone()[0] == "complete"


def test_vote_replaces_per_root_and_stays_opaque(store):
    doc = draft(store)
    first = store.record_vote(root_hash=session_key("root-1"), ref=store.ref(doc["entry_id"], doc["revision"]),
                              rating="up", reason="helped", publishable=False)
    second = store.record_vote(root_hash=session_key("root-1"), ref=store.ref(doc["entry_id"], doc["revision"]),
                               rating="down", reason="stale", publishable=True,
                               generation="gen-1")
    assert first["vote_id"] == second["vote_id"]
    assert first["root_id"] == second["root_id"] != session_key("root-1")
    # The off-period vote has no grant; only the publishable granted one counts.
    votes = store.unbatched_votes(generation="gen-1")
    assert len(votes) == 1 and votes[0]["rating"] == "down"
    assert store.unbatched_votes(generation="gen-2") == []
    other = store.record_vote(root_hash=session_key("root-2"), ref=store.ref(doc["entry_id"], doc["revision"]),
                              rating="up", reason="", publishable=True,
                              generation="gen-1")
    assert other["vote_id"] != first["vote_id"]
    assert len(store.unbatched_votes(generation="gen-1")) == 2


@pytest.fixture
def gated(tmp_path):
    """Enabled settings + admission lease + fixture runner engine."""
    project = tmp_path / "proj"
    project.mkdir()
    settings = write_settings(tmp_path / "community.json", enabled=True,
                              roots=[project])
    adapter = make_admission(tmp_path, project_root=project)
    store = Store(tmp_path / "store", "test")
    from mindie_knowledge.loop.activation import Admission

    engine = Engine(
        store,
        settings_path=tmp_path / "community.json",
        admission=Admission(adapter),
        transcript_adapter=__import__("lane_support").load_parser("codex"),
        redactor_executable=__import__("mindie_knowledge.loop.transcript_redaction", fromlist=["install_scanner"]).install_scanner(),
        summary_command=summary_command(title="Device mapping"),
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
        store,
        settings_path=settings_path, admission=Admission(adapter),
    )
    result = engine.capture(session_id="manual-A", turn_id="t1",
                            summary="should never be processed")
    assert result["status"] == "skipped"
    assert engine.queue.empty()
    status = store.status()
    assert status["captures"] == [] and status["entries"] == {}
    store.close()


def test_capture_preserves_public_transcript_without_a_query(gated):
    store, engine, _, _ = gated
    result = capture_public(engine, "Device mapping: physical device 8; container uses logical 0.", "t1")
    assert result["status"] == "queued"
    engine._process(result["id"])
    row = store.capture_row(result["id"])
    assert row["status"] == "organized", row["detail"]
    hits = store.query("device mapping")["results"]
    assert hits and hits[0]["origin"] == "draft"
    assert "logical 0" in store.explain(hits[0]["ref"])["content"]


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


def test_cursor_persists_full_anchor_identity_and_tamper_fails_closed(gated, tmp_path):
    store, engine, _, _ = gated
    rollout = tmp_path / "rollout.jsonl"
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with open(rollout, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"type":"session_meta","payload":{"id":"manual-A"}})+"\n")
        f.write(json.dumps({"timestamp": stamp, "type": "response_item",
                            "payload": {"type": "message", "role": "user",
                                        "content": [{"type": "input_text",
                                                     "text": "investigate"}]}}) + "\n")
    first = engine.capture(session_id="manual-A", turn_id="t1",
                           transcript_path=str(rollout), summary="")
    engine._process(first["id"])
    assert store.capture_row(first["id"])["status"] == "organized"
    cursor = next(iter(store.db.execute("SELECT * FROM cursors")), None)
    assert cursor is not None
    persisted = engine.transcript.FileIdentity.unserialize(
        cursor["identity"], cursor["file_identity"]
    )
    assert persisted is not None and persisted.anchor_len > 0
    assert len(persisted.anchor_digest) == 64  # no inode-only lossy form
    # A tampered/malformed persisted identity fails closed; it never resets
    # the cursor to start=0 or rereads consumed history.
    with store._write_txn():
        store.db.execute("UPDATE cursors SET identity='corrupt'")
    finish_before = store.cursor(cursor["file_identity"])["finish"]
    second = engine.capture(session_id="manual-A", turn_id="t2",
                            transcript_path=str(rollout), summary="")
    engine._process(second["id"])
    assert store.capture_row(second["id"])["status"] == "failed"
    assert "unusable" in store.capture_row(second["id"])["detail"]
    assert store.cursor(cursor["file_identity"])["finish"] == finish_before


def test_failed_index_keeps_complete_body_and_later_capture_does_not_replay_it(gated):
    store, engine, _, _ = gated
    engine.summary_command = summary_command(fail=True)
    first = capture_public(engine, "Original public failure evidence.", "failed-index")
    engine._process(first["id"])
    assert store.capture_row(first["id"])["status"] == "organized"
    summary_due(engine)
    assert store.db.execute("SELECT summary_status FROM transcript_tasks").fetchone()[0] == "failed"
    second = capture_public(engine, "Later public correction.", "later-evidence")
    engine._process(second["id"])
    body = store.drafts_changed()[0]["content"]
    assert body.count("Original public failure evidence.") == 1
    assert body.count("Later public correction.") == 1
    from mindie_knowledge.loop.export import build_batch
    assert build_batch(store, settings=engine._settings()) is None


def test_disable_while_queued_cancels_without_model(gated, tmp_path):
    store, engine, settings, _ = gated
    result = capture_public(engine, "Queued public material.", "t1")
    write_settings(tmp_path / "community.json", enabled=False, roots=[tmp_path])
    engine._process(result["id"])
    assert store.capture_row(result["id"])["status"] == "cancelled"
    assert store.db.execute("SELECT count(*) FROM material_batches").fetchone()[0] == 0


def test_outbox_coalesces_and_disable_cancels_unsent(gated, tmp_path):
    store, engine, _, _ = gated
    doc = draft(store)
    from mindie_knowledge.loop.export import build_batch

    current = settings_mod.load(tmp_path / "community.json")
    doc_entry = store.get(store.ref(doc["entry_id"]))
    store.grant("draft", doc["entry_id"], doc_entry["revision"], current.generation)
    vote = store.record_vote(root_hash=session_key("root-1"), ref=store.ref(doc["entry_id"], doc["revision"]),
                             rating="up", reason="", publishable=True,
                             generation=current.generation)
    built = build_batch(store, settings=current, revision_fn=_revision_double)
    batch_id, revision, batch, ids, votes = built
    paths = {f["path"] for f in batch["files"]}
    assert any(p.startswith("tasks/") for p in paths)
    assert any(p.startswith("feedback/") for p in paths)
    assert (store.root / "outbox" / "staging" / batch_id).is_dir()
    current = settings_mod.load(tmp_path / "community.json")
    assert store.drafts_changed(generation=current.generation) == []
    assert store.unbatched_votes(generation=current.generation) == []
    with pytest.raises(ValueError, match="invalid batch status"):
        store.mark_batch(batch_id, "maybe")
    # Forced shutdown keeps an unattempted batch pending; disable cancels it.
    assert store.outbox_pending()[0]["batch_id"] == batch_id
    engine._cancel_unsent("sharing disabled; unsent work cancelled")
    assert store.batch(batch_id)["status"] == "disabled"
    assert store.unbatched_votes(generation=current.generation) == []


def test_core_mcp_host_shim_is_retired(tmp_path):
    """Native MCP dispatch belongs to the adapters; core no longer interprets
    any host's turn metadata. The retired operation is refused and starts
    nothing."""
    config = tmp_path / "engine.json"
    config.write_text(json.dumps(dict(root=str(tmp_path / "root"), domain="test")))
    completed = subprocess.run(
        [sys.executable, "-m", "mindie_knowledge.loop.cli", "mcp", "--config",
         str(config)],
        input="", text=True, capture_output=True, timeout=10,
    )
    assert completed.returncode == 2  # invalid choice; no dispatch surface
    assert not (tmp_path / "root").exists()


def test_legacy_session_activation_config_is_rejected(tmp_path):
    config = tmp_path / "engine.json"
    config.write_text(json.dumps(dict(
        root=str(tmp_path / "root"), domain="test",
        session_activation=str(tmp_path / "adapter.json"),
    )))
    completed = subprocess.run(
        [sys.executable, "-m", "mindie_knowledge.loop.cli", "status", "--config",
         str(config)],
        text=True, capture_output=True, timeout=10,
    )
    assert completed.returncode == 2
    assert "admission_path" in completed.stderr
    assert not (tmp_path / "root").exists()


def test_hook_short_circuits_when_sharing_off(tmp_path):
    engine_config = tmp_path / "engine.json"
    settings_path = tmp_path / "community.json"
    write_settings(settings_path, enabled=False, roots=[tmp_path])
    adapter = make_admission(tmp_path, project_root=tmp_path)
    engine_config.write_text(json.dumps(dict(
        root=str(tmp_path / "root"), domain="test",
        community_config=str(settings_path),
        admission_path=str(adapter),
    )))
    start = time.monotonic()
    capture_hook(engine_config, dict(
        hook_event_name="Stop", identity_kind="turn",
        session_id="manual-A", turn_id="t",
        mindie_activation=admission_token(adapter), last_assistant_message="summary",
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

    engine = Engine(store, settings_path=settings_path,
                    admission=Admission(adapter))
    service = Service(engine, admission=Admission(adapter))
    thread = threading.Thread(target=service.http.serve_forever, daemon=True)
    thread.start()
    try:
        doc = store.create_draft(kind="experience", title="Graph capture",
                                 summary="s", content="c", owner=PRODUCER)
        with pytest.raises(ValueError, match="identity"):
            service.call("query", dict(query="graph"))
        hit = service.call("query", dict(query="graph", _session_id="manual-A",
                                         _session_verified=True))
        assert doc["entry_id"][:16] in hit["results"][0]["ref"]
        assert "@" in hit["results"][0]["ref"]  # block refs pin exact file bytes
        # The block reference resolves without assembling the full task.
        assert service.call("explain", dict(ref=hit["results"][0]["ref"],
                                            _session_id="manual-A",
                                            _session_verified=True))["entry_id"] == doc["entry_id"]
        vote = service.call("feedback", dict(ref=hit["results"][0]["feedback_ref"], rating="up",
                                             reason="", _session_id="manual-A",
                                             _session_verified=True))
        assert vote["publishable"] is True
        native = project / "native.jsonl"
        native.write_text(json.dumps(dict(type="session_meta", payload=dict(id="manual-A"))) + "\n")
        queued = service.call(
            "capture",
            dict(session_id="manual-A", turn_id="t", transcript_path=str(native), summary="x",
                 _session_id="manual-A", _activation=admission_token(adapter)),
        )
        assert queued["status"] == "queued"  # admission passes; runner is absent
        engine._process(queued["id"])
        assert store.capture_row(queued["id"])["status"] == "failed"
        assert store.db.execute("SELECT count(*) FROM material_batches").fetchone()[0] == 0  # no model attempt
        from mindie_knowledge.loop.transport import rpc

        assert rpc(service.connection, "status", timeout=5)["domain"] == "test"
    finally:
        service.http.shutdown()
        service.http.server_close()
        store.close()


def test_unsent_draft_never_backfills_after_reenable(gated, tmp_path):
    """Generation-A material organized and granted, then OFF/ON: the new
    generation must never auto-prepare the old draft for publication."""
    from mindie_knowledge.loop.export import build_batch

    store, engine, _, _ = gated
    result = capture_public(engine, "Device mapping: physical device 8; container uses 0.", "t1")
    engine._process(result["id"])
    assert store.capture_row(result["id"])["status"] == "organized"
    gen_a = settings_mod.load(tmp_path / "community.json").generation
    assert store.drafts_changed(generation=gen_a)  # publishable under A
    engine._cancel_unsent("sharing disabled; unsent work cancelled")
    write_settings(tmp_path / "community.json", enabled=False, roots=[tmp_path / "proj"])
    write_settings(tmp_path / "community.json", enabled=True, roots=[tmp_path / "proj"])
    gen_c = settings_mod.load(tmp_path / "community.json").generation
    assert gen_c != gen_a
    assert store.drafts_changed(generation=gen_c) == []
    assert build_batch(store, settings=settings_mod.load(tmp_path / "community.json"),
                       revision_fn=_revision_double) is None
    # The old draft stays local and inert — content and history preserved.
    assert store.query("device mapping")["results"]
    engine.revoke_stale()  # restart/missed-poll-edge path agrees
    assert build_batch(store, settings=settings_mod.load(tmp_path / "community.json"),
                       revision_fn=_revision_double) is None


def test_old_pending_batch_disabled_after_restart(gated, tmp_path):
    from mindie_knowledge.loop.export import build_batch

    store, engine, _, _ = gated
    current = settings_mod.load(tmp_path / "community.json")
    doc = draft(store, generation=current.generation)
    built = build_batch(store, settings=current, revision_fn=_revision_double)
    batch_id = built[0]
    assert store.batch(batch_id)["status"] == "pending"
    write_settings(tmp_path / "community.json", enabled=False, roots=[tmp_path / "proj"])
    write_settings(tmp_path / "community.json", enabled=True, roots=[tmp_path / "proj"])
    restarted = Engine(store,
                       settings_path=tmp_path / "community.json",
                       admission=engine.admission)
    restarted.revoke_stale()  # what start() runs before any thread
    row = store.batch(batch_id)
    assert row["status"] == "disabled"
    assert row["generation"] == current.generation
    # A pending batch also cannot slip past the submit-time generation check.
    restarted._submit(store.batch(batch_id))
    assert store.batch(batch_id)["status"] == "disabled"


def test_old_generation_update_id_cannot_republish(gated, tmp_path):
    store, engine, _, _ = gated
    gen_a = settings_mod.load(tmp_path / "community.json").generation
    doc = draft(store, generation=gen_a)
    write_settings(tmp_path / "community.json", enabled=False, roots=[tmp_path / "proj"])
    write_settings(tmp_path / "community.json", enabled=True, roots=[tmp_path / "proj"])
    gen_c = settings_mod.load(tmp_path / "community.json").generation
    with pytest.raises(ValueError, match="generation"):
        store.append_observation(doc["entry_id"], "smuggled update", producer=PRODUCER,
                                 marker="f" * 32, generation=gen_c)
    assert store.drafts_changed(generation=gen_c) == []
    fresh = draft(store, title="Fresh note", content="Current generation body.", generation=gen_c)
    assert [item["entry_id"] for item in store.drafts_changed(generation=gen_c)] == [fresh["entry_id"]]


def test_current_generation_draft_and_vote_publish(gated, tmp_path):
    from mindie_knowledge.loop.export import build_batch

    store, engine, _, _ = gated
    current = settings_mod.load(tmp_path / "community.json")
    doc = draft(store, generation=current.generation)
    store.record_vote(root_hash=session_key("root-9"), ref=store.ref(doc["entry_id"], doc["revision"]),
                      rating="up", reason="", publishable=True,
                      generation=current.generation)
    store.record_vote(root_hash=session_key("root-9"), ref=store.ref(doc["entry_id"], doc["revision"]),
                      rating="down", reason="counterexample", publishable=True,
                      generation=current.generation)
    built = build_batch(store, settings=current, revision_fn=_revision_double)
    batch = built[2]
    cases = [f for f in batch["files"] if f["path"].endswith("/index.md")]
    feedback = [f for f in batch["files"] if f["path"].startswith("feedback/")]
    assert len(cases) == 1 and len(feedback) == 1
    votes = json.loads(feedback[0]["content"])["votes"]
    assert len(votes) == 1 and votes[0]["rating"] == "down"


def test_v4_store_keeps_old_private_files_inert_and_retains_ownership(tmp_path):
    root = tmp_path / 'test'
    root.mkdir()
    prior = root / 'store-v2.sqlite3'
    prior.write_bytes(b'old private state; do not open, migrate or delete')
    before = prior.read_bytes()
    first = Store(tmp_path, 'test')
    marker = first.root / "state-v4.sqlite3.owner"
    identity = marker.read_bytes()
    assert first.query('old private')['results'] == []
    first.close()
    second = Store(tmp_path, 'test')
    assert marker.read_bytes() == identity
    assert prior.read_bytes() == before
    assert (first.root / 'state-v4.sqlite3').is_file()
    second.close()


def test_title_correction_keeps_publication_path(store):
    first = draft(store)
    first_index = f"tasks/{first['entry_id']}/index.md"
    second, _ = store.append_observation(first['entry_id'], 'Correction.',
        marker='c' * 32, producer=PRODUCER, header={'title': 'Corrected scope'})
    assert first_index == f"tasks/{second['entry_id']}/index.md"
    assert 'Corrected scope' in store.materials.export_task(second['entry_id'])['files']['index.md']


def test_pending_votes_are_not_sent_after_upstream_withdrawal(gated):
    from mindie_knowledge.loop.export import build_batch
    store, engine, _, _ = gated
    settings = engine._settings()
    doc = draft(store)
    install_documents(store, [doc], feed_ident='f' * 64)
    ref = store.ref(doc['entry_id'], doc['revision'])
    store.record_vote(ref=ref, rating='down', reason='', root_hash=session_key('consumer'),
                      publishable=True, generation=settings.generation)
    built = build_batch(store, settings=settings)
    pending = store.batch(built[0])
    store.install_feed([], feed_ident='f' * 64)
    writes = []
    engine.community = {'submit_batch': lambda *args, **kwargs: writes.append(args)}
    engine._submit(pending)
    assert writes == []
    assert store.batch(built[0])['status'] == 'disabled'
    assert store.query(doc["title"])["results"] == []
