"""Shared-core seams: transcript adapter config, confirmed-PR compaction with
safe later continuation, and the deterministic contribution recovery CLI."""

import json
import subprocess
import sys

import pytest

from mindie_knowledge.loop import documents, transcript as transcript_mod
from mindie_knowledge.loop.cli import (
    config_at,
    contribution_recovery,
    load_transcript_adapter,
)
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.export import build_batch
from mindie_knowledge.loop.store import Store

from conftest import make_admission, write_settings

PRODUCER = "b" * 64


def _revision_double(files, domain, base_commit, entry_refs):
    return "r" * 64


def _confirmed_batch(store, settings):
    doc = store.create_draft(
        kind="experience", title="Sent case", summary="s",
        content="the sent body", owner=PRODUCER,
        generation=settings.generation,
    )
    batch_id, revision, batch, _ids, _votes = build_batch(
        store, settings=settings, revision_fn=_revision_double
    )
    store.mark_batch(batch_id, "submitted", pr_url="https://x/pr/1",
                     head_sha="a" * 40)
    return doc, batch_id


def test_transcript_adapter_config_seam(tmp_path):
    engine_config = tmp_path / "engine.json"
    engine_config.write_text(json.dumps(dict(
        root=str(tmp_path), domain="test",
        transcript_adapter="relative/parser.py",
    )))
    with pytest.raises(ValueError, match="absolute local parser module"):
        config_at(engine_config)
    assert load_transcript_adapter({}) is None
    engine_config.write_text(json.dumps(dict(
        root=str(tmp_path), domain="test",
        transcript_adapter=str(tmp_path / "parser.py"),
    )))
    with pytest.raises(ValueError, match="missing"):
        load_transcript_adapter(config_at(engine_config))
    adapter = tmp_path / "parser.py"
    adapter.write_text("x = 1\n")
    with pytest.raises(ValueError, match="must export"):
        load_transcript_adapter(config_at(engine_config))
    import mindie_knowledge.loop.transcript as real

    adapter.write_text(
        f"from mindie_knowledge.loop.transcript import "
        f"FileIdentity, identify, read_material\n"
    )
    module = load_transcript_adapter(config_at(engine_config))
    assert module.identify is real.identify


def test_missing_adapter_is_honest_summary_only(tmp_path):
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
        "p=json.load(sys.stdin)\n"
        "assert p['coverage']['summary_only'] and 'no transcript adapter' "
        "in p['coverage']['notes'][0]\n"
        "print(json.dumps({'entries':[{'entry_id':None,'title':'T',"
        "'summary':'s','content':p['increment'][:100],'conditions':{}}]}))\n"
    )
    engine = Engine(
        store, agent_command=[sys.executable, str(runner)],
        settings_path=tmp_path / "community.json",
        admission=Admission(adapter),  # no transcript_adapter configured
    )
    result = engine.capture(session_id="manual-A", turn_id="t1",
                            transcript_path=str(tmp_path / "rollout.jsonl"),
                            summary="bounded summary only")
    engine._process(result["id"])
    row = store.capture_row(result["id"])
    assert row["status"] == "organized", row["detail"]  # summary path, no guess
    assert store.coverage_gaps() == []  # no transcript region was consumed
    store.close()


def test_compact_confirmed_removes_sent_payload_and_keeps_receipts(tmp_path):
    settings = write_settings(tmp_path / "community.json", enabled=True,
                              roots=[tmp_path])
    store = Store(tmp_path / "store", "test")
    doc, batch_id = _confirmed_batch(store, settings)
    capture = store.add_capture(root_session="rh", session="s", turn="t",
                                transcript=None, summary="raw capture summary",
                                generation=settings.generation)
    store.mark_capture(capture["id"], "organized")
    assert (store.root / "outbox" / "staging" / batch_id).is_dir()

    removed = store.compact_confirmed(batch_id)
    assert removed["entries"] == 1 and removed["staging"] == 1
    assert removed["captures"] == 1
    row = store._row(doc["entry_id"])
    assert row["draft_revision"] is None
    assert row["batched_revision"]  # hash receipt kept
    header = json.loads(row["doc"])
    assert header["title"] == "Sent case" and header["content"] == ""
    assert store._revision_doc(doc["entry_id"], doc["revision"]) is None
    assert store.capture_row(capture["id"])["summary"] == ""
    assert not (store.root / "outbox" / "staging" / batch_id).exists()
    # Unresolved/failed unsent work is never compacted.
    assert store.compact_confirmed("batch-unknown") is None
    store.close()


def test_restore_draft_enables_safe_later_continuation(tmp_path):
    settings = write_settings(tmp_path / "community.json", enabled=True,
                              roots=[tmp_path])
    store = Store(tmp_path / "store", "test")
    doc, batch_id = _confirmed_batch(store, settings)
    store.compact_confirmed(batch_id)
    with pytest.raises(ValueError, match="no local draft"):
        store.append_observation(doc["entry_id"], "new paragraph", marker="ab"*32,
                                 producer=PRODUCER,
                                 generation=settings.generation)
    remote = documents.make_entry(
        entry_id=doc["entry_id"], domain="test", kind="experience",
        title="Sent case", summary="s", content="the sent body",
    )
    store.restore_draft(doc["entry_id"], remote, generation=settings.generation)
    updated, appended = store.append_observation(
        doc["entry_id"], "new paragraph", marker="ab"*32, producer=PRODUCER,
        generation=settings.generation,
    )
    assert appended and "the sent body" in updated["content"]
    assert "new paragraph" in updated["content"]  # appended, never replaced
    with pytest.raises(ValueError, match="already has a local draft"):
        store.restore_draft(doc["entry_id"], remote)
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
    )))
    config = config_at(engine_config)

    inspected = contribution_recovery(config, "contribution-inspect", batch_id)
    assert inspected["outbox"]["status"] == "submitted"
    assert inspected["outbox"]["pr_url"] == "https://x/pr/1"

    compacted = contribution_recovery(config, "contribution-compact", batch_id)
    assert compacted["compacted"]["entries"] == 1
    absent = contribution_recovery(config, "contribution-inspect", "batch-nope")
    assert absent["outbox"] is None and absent["ledger"] == []
    with pytest.raises(ValueError, match="unknown contribution batch"):
        contribution_recovery(config, "contribution-compact", "batch-nope")
    # Retry is explicit-only: a confirmed batch is never "retried".
    with pytest.raises(ValueError, match="confirmed failed or unresolved"):
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
