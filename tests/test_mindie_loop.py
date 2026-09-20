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
from mindie_knowledge.loop import transcript as transcript_mod
from mindie_knowledge.loop.cli import capture_hook
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store, canonical, session_key
from mindie_knowledge.loop.transport import Service

from conftest import admission_token, make_admission, write_settings

PRODUCER = "a" * 64


def draft(store, title="ACL graph investigation", content="Compare eager first.",
          summary="Narrow the execution mode before debugging capture.",
          generation=None):
    return store.create_draft(
        kind="experience", title=title, summary=summary, content=content,
        owner=PRODUCER, generation=generation,
    )


def _revision_double(files, domain, base_commit, entry_refs):
    # Clearly labeled mechanism double for the community-owned batch digest.
    from mindie_knowledge.loop.store import digest as _digest

    return _digest({"files": sorted((f["path"], f["sha256"]) for f in files),
                    "domain": domain, "base_commit": base_commit,
                    "entry_refs": sorted(entry_refs)})


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
    with pytest.raises(ValueError, match="owning task"):
        store.append_observation(doc["entry_id"], "foreign", marker="c" * 32,
                                 producer="d" * 64)


def test_same_title_entries_keep_distinct_identities(store):
    first = draft(store, title="Shared symptom title")
    second = draft(store, title="Shared symptom title")
    assert first["entry_id"] != second["entry_id"]
    hits = store.query("Shared symptom title")["results"]
    assert {h["ref"].split("@")[0].rsplit("/", 1)[-1] for h in hits} == {
        first["entry_id"][:16], second["entry_id"][:16]
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
    store.install_feed([foreign], feed_ident="f" * 64)
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
    assert store.get(store.ref(doc["entry_id"], doc["revision"]))["title"] == \
        "Misleading old title"  # old pinned body untouched


def test_short_refs_resolve_exactly_and_ambiguity_fails(store):
    doc = draft(store)
    updated, _ = store.append_observation(
        doc["entry_id"], "Later: second revision.", marker="b" * 32,
        producer=PRODUCER,
    )
    short = store.query("ACL graph")["results"][0]["ref"]
    # Query pins a short ref: 16-hex prefixes, no separate revision field.
    entry_tok, rev_tok = short.rsplit("/", 1)[-1].split("@")
    assert entry_tok == doc["entry_id"][:16] and rev_tok == updated["revision"][:16]
    assert store.get(short)["revision"] == updated["revision"]
    # A short prefix pinned to the OLD revision reads the exact old body.
    old_pin = f"{doc['entry_id'][:16]}@{doc['revision'][:16]}"
    assert store.get(old_pin)["content"] == doc["content"]
    # Full refs keep working.
    assert store.get(store.ref(doc["entry_id"], doc["revision"]))["content"] == doc["content"]
    # An entry prefix naming two entries fails, never picks the first.
    other = store.create_draft(
        kind="experience", title="Collision entry", summary="s",
        content="c", entry_id=doc["entry_id"][:16] + "f" * 48,
    )
    with pytest.raises(ValueError, match="ambiguous"):
        store.get(doc["entry_id"][:16])
    assert store.get(doc["entry_id"])["entry_id"] == doc["entry_id"]
    # The colliding entry's query ref falls back to its full identity.
    refs = {h["ref"] for h in store.query("Collision entry")["results"]}
    assert any(other["entry_id"] + "@" in ref for ref in refs)


def test_correction_changes_retrieval_header_but_preserves_old_body(store):
    old=draft(store,summary='Prefix cache is the suspected cause.',content='Initial hypothesis: prefix cache.')
    new,_=store.append_observation(old['entry_id'],'Later evidence: workspace underallocated.',
        marker='f'*32,producer=PRODUCER,header={'summary':'Workspace underallocated; prefix hypothesis disproven.'})
    assert new['summary'].startswith('Workspace')
    assert 'Initial hypothesis' in new['content'] and 'Later evidence' in new['content']
    assert store.get(store.ref(old['entry_id'],old['revision']))['summary']==old['summary']


def test_quota_deferred_material_keeps_cursor_and_resumes(gated,tmp_path):
    store,engine,_,_=gated
    from datetime import datetime,timezone
    stamp=datetime.now(timezone.utc).isoformat()
    rollout=tmp_path/'native.jsonl'
    rollout.write_text(json.dumps({'type':'session_meta','payload':{'id':'manual-A'}})+'\n'+
        json.dumps({'timestamp':stamp,'type':'response_item','payload':{'type':'message','role':'assistant','phase':'final_answer','content':[{'type':'output_text','text':'Observed device mapping: physical id does not equal logical id.'}]}})+'\n')
    root=session_key('manual-A')
    for i in range(6):engine.budget.reserve(str(i),root,'organize');engine.budget.finish(str(i),True)
    capture=engine.capture(session_id='manual-A',turn_id='defer',transcript_path=str(rollout))
    # The fixture may root this native child elsewhere; use its actual root key.
    actual_root=store.capture_row(capture['id'])['root_session']
    with store.db:store.db.execute('UPDATE maintenance_attempts SET session=?',(actual_root,))
    engine._process(capture['id'])
    assert store.capture_row(capture['id'])['status']=='pending'
    assert store.cursor(str(rollout.resolve())) is None
    with store.db:store.db.execute('UPDATE maintenance_attempts SET started=started-3602')
    engine._process(capture['id'])
    assert store.capture_row(capture['id'])['status']=='organized'
    assert store.cursor(str(rollout.resolve()))['finish']==rollout.stat().st_size


def test_vote_replaces_per_root_and_stays_opaque(store):
    doc = draft(store)
    first = store.record_vote(root_hash=session_key("root-1"), ref=doc["entry_id"],
                              rating="up", reason="helped", publishable=False)
    second = store.record_vote(root_hash=session_key("root-1"), ref=doc["entry_id"],
                               rating="down", reason="stale", publishable=True,
                               generation="gen-1")
    assert first["vote_id"] == second["vote_id"]
    assert first["root_id"] == second["root_id"] != session_key("root-1")
    # The off-period vote has no grant; only the publishable granted one counts.
    votes = store.unbatched_votes(generation="gen-1")
    assert len(votes) == 1 and votes[0]["rating"] == "down"
    assert store.unbatched_votes(generation="gen-2") == []
    other = store.record_vote(root_hash=session_key("root-2"), ref=doc["entry_id"],
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
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import json,sys\n"
        "p=json.load(sys.stdin)\n"
        "print(json.dumps({'entries':[{'entry_id':None,'title':'Device mapping',"
        "'summary':'Container logical ids restart at zero.',"
        "'content':p['increment'][:400],'conditions':{}}]}))\n"
    )
    store = Store(tmp_path / "store", "test")
    from mindie_knowledge.loop.activation import Admission

    engine = Engine(
        store,
        agent_command=[sys.executable, str(runner)],
        settings_path=tmp_path / "community.json",
        admission=Admission(adapter),
        transcript_adapter=transcript_mod,
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
    from mindie_knowledge.loop import transcript as transcript_mod

    cursor = next(iter(store.db.execute("SELECT * FROM cursors")), None)
    assert cursor is not None
    persisted = transcript_mod.FileIdentity.unserialize(
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


def test_failed_region_is_consumed_and_never_replayed(gated, tmp_path):
    store, engine, settings_path, _ = gated
    engine.agent_command = [sys.executable, "-c", "import sys; sys.exit(1)"]
    rollout = tmp_path / "rollout.jsonl"
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with open(rollout, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"type":"session_meta","payload":{"id":"manual-A"}})+"\n")
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
    from mindie_knowledge.loop.export import build_batch

    current = settings_mod.load(tmp_path / "community.json")
    doc_entry = store.get(store.ref(doc["entry_id"]))
    store.grant("draft", doc["entry_id"], doc_entry["revision"], current.generation)
    vote = store.record_vote(root_hash=session_key("root-1"), ref=doc["entry_id"],
                             rating="up", reason="", publishable=True,
                             generation=current.generation)
    built = build_batch(store, settings=current, revision_fn=_revision_double)
    batch_id, revision, batch, ids, votes = built
    paths = {f["path"] for f in batch["files"]}
    assert any(p.startswith("cases/") for p in paths)
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
        hook_event_name="Stop", session_id="manual-A", turn_id="t",
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

    engine = Engine(store, agent_command=None, settings_path=settings_path,
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
        assert "@" in hit["results"][0]["ref"]  # refs pin their observed revision
        # The short pinned ref resolves back to the exact same document.
        assert service.call("explain", dict(ref=hit["results"][0]["ref"],
                                            _session_id="manual-A",
                                            _session_verified=True))["entry_id"] == doc["entry_id"]
        vote = service.call("feedback", dict(ref=doc["entry_id"], rating="up",
                                             reason="", _session_id="manual-A",
                                             _session_verified=True))
        assert vote["publishable"] is True
        queued = service.call(
            "capture",
            dict(session_id="manual-A", turn_id="t", summary="x",
                 _session_id="manual-A", _activation=admission_token(adapter)),
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


def test_unsent_draft_never_backfills_after_reenable(gated, tmp_path):
    """Generation-A material organized and granted, then OFF/ON: the new
    generation must never auto-prepare the old draft for publication."""
    from mindie_knowledge.loop.export import build_batch

    store, engine, _, _ = gated
    result = engine.capture(session_id="manual-A", turn_id="t1",
                            summary="Mapped physical device 8; container uses 0.")
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
    write_settings(tmp_path / "community.json", enabled=True, roots=[tmp_path / "proj"])
    restarted = Engine(store, agent_command=None,
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
    write_settings(tmp_path / "community.json", enabled=True, roots=[tmp_path / "proj"])
    gen_c = settings_mod.load(tmp_path / "community.json").generation
    # A (malicious or confused) organizer result naming the old draft as an
    # update id must not append — that would republish the whole old body.
    result = {"entries": [dict(entry_id=doc["entry_id"], title=None,
                               summary=doc["summary"], content="smuggled update",
                               conditions={})]}
    refs, notes = engine._apply(result, opaque=PRODUCER, marker="f" * 32,
                                generation=gen_c)
    assert refs == [] and any("generation" in note for note in notes)
    assert store.drafts_changed(generation=gen_c) == []
    # Fresh material in the current generation works normally.
    result = {"entries": [dict(entry_id=None, title="Fresh note", summary="s",
                               content="current generation body", conditions={})]}
    refs, _ = engine._apply(result, opaque=PRODUCER, marker="e" * 32,
                            generation=gen_c)
    assert len(refs) == 1
    assert len(store.drafts_changed(generation=gen_c)) == 1


def test_current_generation_draft_and_vote_publish(gated, tmp_path):
    from mindie_knowledge.loop.export import build_batch

    store, engine, _, _ = gated
    current = settings_mod.load(tmp_path / "community.json")
    doc = draft(store, generation=current.generation)
    store.record_vote(root_hash=session_key("root-9"), ref=doc["entry_id"],
                      rating="up", reason="", publishable=True,
                      generation=current.generation)
    store.record_vote(root_hash=session_key("root-9"), ref=doc["entry_id"],
                      rating="down", reason="counterexample", publishable=True,
                      generation=current.generation)
    built = build_batch(store, settings=current, revision_fn=_revision_double)
    batch = built[2]
    cases = [f for f in batch["files"] if f["path"].startswith("cases/")]
    feedback = [f for f in batch["files"] if f["path"].startswith("feedback/")]
    assert len(cases) == 1 and len(feedback) == 1
    votes = json.loads(feedback[0]["content"])["votes"]
    assert len(votes) == 1 and votes[0]["rating"] == "down"


def test_v3_store_keeps_old_private_files_inert_and_persists_capture_floor(tmp_path):
    root = tmp_path / 'test'
    root.mkdir()
    prior = root / 'store-v2.sqlite3'
    prior.write_bytes(b'old private state; do not open, migrate or delete')
    before = prior.read_bytes()
    first = Store(tmp_path, 'test')
    floor = first.capture_floor
    assert first.query('old private')['results'] == []
    first.close()
    second = Store(tmp_path, 'test')
    assert second.capture_floor == floor
    assert prior.read_bytes() == before
    assert (root / 'store-v3.sqlite3').is_file()
    second.close()


def test_title_correction_keeps_publication_path(store):
    from mindie_knowledge.loop.export import _filename
    first = draft(store)
    second, _ = store.append_observation(first['entry_id'], 'Correction.',
        marker='c' * 32, producer=PRODUCER, header={'title': 'Corrected scope'})
    assert _filename(first) == _filename(second)


def test_pending_votes_are_not_sent_after_upstream_withdrawal(gated):
    from mindie_knowledge.loop.export import build_batch
    store, engine, _, _ = gated
    settings = engine._settings()
    doc = draft(store)
    store.install_feed([doc], feed_ident='f' * 64)
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
    assert store.draft_headers(owner=PRODUCER) == []
