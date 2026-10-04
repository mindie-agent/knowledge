"""Shared-core seams: transcript adapter config, confirmed-PR compaction with
safe later continuation, and the deterministic contribution recovery CLI."""

import json
import subprocess
import sys

import pytest

from mindie_knowledge.loop import documents
from mindie_knowledge.loop.cli import (
    config_at,
    contribution_recovery,
    load_transcript_adapter,
)
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.export import build_batch
from mindie_knowledge.loop.store import Store

import transcript_double
from conftest import make_admission, write_settings
from lane_support import parser_path
from material_worker_fixture import command as summary_command
from mindie_knowledge.loop.transcript_redaction import install_scanner

PRODUCER = "b" * 64


def _revision_double(files, domain, base_commit, entry_refs):
    return "a" * 64


def _confirmed_batch(store, settings):
    doc = store.create_draft(
        kind="experience", title="Sent case", summary="s",
        content="the sent body", owner=PRODUCER,
        generation=settings.generation,
    )
    batch_id, revision, batch, _ids, _votes = build_batch(
        store, settings=settings
    )
    store.mark_batch(batch_id, "submitted", pr_url="https://x/pr/1",
                     head_sha="a" * 40)
    return doc, batch_id


def test_transcript_adapter_config_seam(tmp_path):
    engine_config = tmp_path / "engine.json"
    engine_config.write_text(json.dumps(dict(
        root=str(tmp_path), domain="test", redactor_executable=install_scanner(), summary_command=summary_command(),
        transcript_adapter="relative/parser.py",
    )))
    with pytest.raises(ValueError, match="absolute local parser module"):
        config_at(engine_config)
    assert load_transcript_adapter({}) is None
    engine_config.write_text(json.dumps(dict(
        root=str(tmp_path), domain="test", redactor_executable=install_scanner(), summary_command=summary_command(),
        transcript_adapter=str(tmp_path / "parser.py"),
    )))
    with pytest.raises(ValueError, match="missing"):
        load_transcript_adapter(config_at(engine_config))
    adapter = tmp_path / "parser.py"
    adapter.write_text("x = 1\n")
    with pytest.raises(ValueError, match="must export"):
        load_transcript_adapter(config_at(engine_config))
    # A real parser file with a frozen dataclass loads: the module is
    # registered in sys.modules before execution.
    import shutil

    shutil.copy(transcript_double.__file__, adapter)
    module = load_transcript_adapter(config_at(engine_config))
    assert hasattr(module, "FileIdentity") and hasattr(module, "read_material")
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(json.dumps(
        {"type": "session_meta", "payload": {"id": "s"}}) + "\n")
    assert module.identify(rollout) is not None
    assert module.read_material(str(rollout), 0, session_id="s")["status"] in {
        "ok", "unchanged"
    }


def test_missing_adapter_fails_without_substituting_summary(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    settings = write_settings(tmp_path / "community.json", enabled=True,
                              roots=[project])
    adapter = make_admission(tmp_path, project_root=project)
    store = Store(tmp_path / "store", "test")
    from mindie_knowledge.loop.activation import Admission

    runner = tmp_path / "runner.py"
    runner.write_text(
        "import json,sys\n"
        "p=json.loads(sys.stdin.buffer.read().decode('utf-8'))\n"
        "assert p['coverage']['summary_only'] and 'no transcript adapter' "
        "in p['coverage']['notes'][0]\n"
        "sys.stdout.buffer.write(json.dumps({'entries':[{'entry_id':None,'title':'T',"
        "'summary':'s','content':p['increment'][:100],'conditions':{}}]}).encode('utf-8'))\n"
    )
    engine = Engine(
        store, redactor_executable=install_scanner(),
        settings_path=tmp_path / "community.json",
        admission=Admission(adapter),  # no transcript_adapter configured
    )
    result = engine.capture(session_id="manual-A", turn_id="t1",
                            transcript_path=str(tmp_path / "rollout.jsonl"),
                            summary="bounded summary only")
    engine._process(result["id"])
    row = store.capture_row(result["id"])
    assert row["status"] == "failed" and "no transcript adapter" in row["detail"]
    assert store.drafts_changed() == []
    assert engine.status()['summary_usage']['model_calls'] == 0
    assert store.coverage_gaps() == []  # no transcript region was consumed
    store.close()


def test_compact_confirmed_removes_sent_payload_and_keeps_receipts(tmp_path):
    from mindie_knowledge.loop.store import canonical

    settings = write_settings(tmp_path / "community.json", enabled=True,
                              roots=[tmp_path])
    store = Store(tmp_path / "store", "test")
    # Freeze the sent batch before creating independent unsent work.
    doc, batch_id = _confirmed_batch(store, settings)
    other_doc = store.create_draft(
        kind="experience", title="Other case", summary="o",
        content="unsent other body", owner=PRODUCER,
        generation=settings.generation,
    )
    other_cap = store.add_capture(root_session="rh", session="s", turn="t-other",
                                  transcript=None, summary="other draft evidence",
                                  generation=settings.generation)
    store.mark_capture(
        other_cap["id"], "organized",
        canonical({"refs": [store.ref(other_doc["entry_id"])], "notes": []}),
    )
    with store._write_txn():
        store.db.execute(
            "UPDATE captures SET created=1.0 WHERE id=?", (other_cap["id"],)
        )
    covered = store.add_capture(root_session="rh", session="s", turn="t-old",
                                transcript=None, summary="covered summary",
                                generation=settings.generation)
    store.mark_capture(
        covered["id"], "organized",
        canonical({"refs": [store.ref(doc["entry_id"], doc["revision"])], "notes": []}),
    )
    # A newer unsent capture (no refs to the sent entry) stays intact.
    newer = store.add_capture(root_session="rh", session="s", turn="t-new",
                              transcript=None, summary="newer unsent summary",
                              generation=settings.generation)
    store.mark_capture(newer["id"], "organized")
    assert (store.root / "outbox" / "staging" / batch_id).is_dir()

    removed = store.compact_confirmed(batch_id)
    assert removed["entries"] == 0 and removed["staging"] == 1
    assert removed["captures"] == 1  # only the capture whose refs are this batch
    row = store._row(doc["entry_id"])
    assert row["draft_revision"] == doc["revision"]
    assert row["batched_revision"]  # hash receipt kept
    header = json.loads(row["doc"])
    assert header["title"] == "Sent case" and "content" not in header
    assert store._revision_doc(doc["entry_id"], doc["revision"])["content"] == doc["content"]
    # Outbox payloads no longer pin redundant local draft history.
    assert store.capture_row(covered["id"])["summary"] == ""
    assert store.capture_row(newer["id"])["summary"] == "newer unsent summary"
    # A different draft's earlier capture is not cleared by timestamp.
    assert store.capture_row(other_cap["id"])["summary"] == "other draft evidence"
    assert not any((store.root / "outbox" / "staging" / batch_id).iterdir())
    receipt = json.loads(store.batch(batch_id)["batch"])
    assert receipt["schema"] == "mindie-contribution/1"
    assert all("content" not in f for f in receipt["files"])
    manifest = next(item for item in receipt["files"] if item["path"].endswith("/index.md"))
    assert manifest["path"].endswith(f"{doc['entry_id']}/index.md")
    assert store.sent_file_hash(doc["entry_id"]) == manifest["sha256"]
    assert store.sent_receipt(doc["entry_id"])["head_sha"] == "a" * 40
    assert store.compact_confirmed("batch-unknown") is None
    store.close()


def test_compact_keeps_newer_unsent_draft_and_drops_sent_history(tmp_path):
    settings = write_settings(tmp_path / "community.json", enabled=True,
                              roots=[tmp_path])
    store = Store(tmp_path / "store", "test")
    doc, batch_id = _confirmed_batch(store, settings)
    updated, appended = store.append_observation(
        doc["entry_id"], "newer unsent observation", marker="cd" * 32,
        producer=PRODUCER, generation=settings.generation,
    )
    assert appended
    newer_rev = updated["revision"]
    removed = store.compact_confirmed(batch_id)
    assert removed["entries"] == 0  # current draft is newer than the sent one
    row = store._row(doc["entry_id"])
    assert row["draft_revision"] == newer_rev
    assert store._revision_doc(doc["entry_id"], newer_rev) is not None
    assert store._revision_doc(doc["entry_id"], doc["revision"]) is None
    assert "newer unsent observation" in store.get(store.ref(doc["entry_id"]))["content"]
    store.close()


def test_per_entry_receipt_survives_later_lineage_batch(tmp_path):
    settings = write_settings(tmp_path / "community.json", enabled=True,
                              roots=[tmp_path])
    store = Store(tmp_path / "store", "test")
    doc_a, batch_id = _confirmed_batch(store, settings)
    store.compact_confirmed(batch_id)
    hash_a = store.sent_file_hash(doc_a["entry_id"])
    head_a = store.sent_receipt(doc_a["entry_id"])["head_sha"]
    store.create_draft(
        kind="experience", title="Later case", summary="b",
        content="entry B only", owner=PRODUCER,
        generation=settings.generation,
    )
    built = build_batch(store, settings=settings)
    assert built is not None
    assert store.batch(batch_id)["status"] == "pending"  # lineage row replaced
    assert store.sent_file_hash(doc_a["entry_id"]) == hash_a
    assert store.sent_receipt(doc_a["entry_id"])["head_sha"] == head_a
    pending = json.loads(store.batch(batch_id)["batch"])
    assert all(doc_a["entry_id"] not in f.get("path", "") for f in pending["files"])
    store.close()


def test_contribution_recovery_ops(tmp_path):
    settings = write_settings(tmp_path / "community.json", enabled=True,
                              roots=[tmp_path])
    store = Store(tmp_path / "store", "test")
    doc, batch_id = _confirmed_batch(store, settings)
    store.close()
    engine_config = tmp_path / "engine.json"
    engine_config.write_text(json.dumps(dict(
        root=str(tmp_path / "store"), domain="test",
        community_config=str(tmp_path / "community.json"),
        capture_mode="public-transcript", redactor_executable=__import__("mindie_knowledge.loop.transcript_redaction", fromlist=["install_scanner"]).install_scanner(),
        summary_command=summary_command(), transcript_adapter=str(parser_path("codex")),
    )))
    config = config_at(engine_config)

    inspected = contribution_recovery(config, "contribution-inspect", batch_id)
    assert inspected["outbox"]["status"] == "submitted"
    assert inspected["outbox"]["pr_url"] == "https://x/pr/1"

    compacted = contribution_recovery(config, "contribution-compact", batch_id)
    assert compacted["compacted"]["entries"] == 0
    absent = contribution_recovery(config, "contribution-inspect", "batch-nope")
    assert absent["outbox"] is None and absent["ledger"] == []
    with pytest.raises(ValueError, match="unknown contribution batch"):
        contribution_recovery(config, "contribution-compact", "batch-nope")
    # Retry is explicit-only and reserved for proven failures.
    with pytest.raises(ValueError, match="proven failed"):
        contribution_recovery(config, "contribution-retry", batch_id)

    # The CLI exposes the operations and refuses a missing --batch.
    completed = subprocess.run(
        [sys.executable, "-m", "mindie_knowledge.loop.cli",
         "contribution-inspect", "--config", str(engine_config),
         "--batch", batch_id],
        text=True, capture_output=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["outbox"]["status"] == "submitted"
    completed = subprocess.run(
        [sys.executable, "-m", "mindie_knowledge.loop.cli",
         "contribution-retry", "--config", str(engine_config)],
        text=True, capture_output=True, timeout=10,
    )
    assert completed.returncode == 2
    assert "--batch" in completed.stderr


def test_compact_capture_coverage_requires_all_exact_sent_revisions(tmp_path):
    """Old A confirmation cannot discard newer or ambiguous A observations."""
    from mindie_knowledge.loop.store import canonical

    settings = write_settings(tmp_path / "community.json", enabled=True,
                              roots=[tmp_path])
    store = Store(tmp_path / "store", "test")
    old, batch_id = _confirmed_batch(store, settings)
    newer, appended = store.append_observation(
        old["entry_id"], "not yet sent", marker="de" * 32,
        producer=PRODUCER, generation=settings.generation,
    )
    assert appended and newer["revision"] != old["revision"]
    old_ref = store.ref(old["entry_id"], old["revision"])
    new_ref = store.ref(newer["entry_id"], newer["revision"])
    cases = {
        "covered": [old_ref],
        "same-entry-newer": [new_ref],
        "mixed-revisions": [old_ref, new_ref],
        "ambiguous": [old_ref, store.ref(old["entry_id"])],
        "unparseable": [old_ref, None],
    }
    captures = {}
    for name, refs in cases.items():
        cap = store.add_capture(root_session="rh", session="s", turn=name,
                                transcript=None, summary=name,
                                generation=settings.generation)
        store.mark_capture(cap["id"], "organized", canonical({"refs": refs}))
        captures[name] = cap["id"]
    removed = store.compact_confirmed(batch_id)
    assert removed["captures"] == 1
    assert store.capture_row(captures["covered"])["summary"] == ""
    for name in cases.keys() - {"covered"}:
        assert store.capture_row(captures[name])["summary"] == name
    assert store._revision_doc(old["entry_id"], old["revision"]) is None
    assert store._revision_doc(newer["entry_id"], newer["revision"])["content"].endswith("not yet sent")
    store.close()
