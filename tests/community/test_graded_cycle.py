"""Synthetic C5/C6 lifecycle through real Git and the FileTransport API boundary.

The native subprocess is a protocol fixture, not a paid model or NPU result.
No real corpus or GitHub repository is touched.
"""
from contextlib import closing
import json
from pathlib import Path
import sys

import pytest

from .conftest import REPO, git
from graded_fixture import unit
from mindie_knowledge.community import submit_batch, reconcile_batch
from mindie_knowledge.community.common import Deadline, UnknownOutcome
from mindie_knowledge.community.transport import FileTransport
from mindie_knowledge.graded.distill import apply_decision, prepare_request
from mindie_knowledge.graded.units import GradedUnit
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.feed import Feed
from mindie_knowledge.loop.store import Store
from mindie_knowledge.materials.references import ReadReferenceError
from mindie_knowledge.publication_contract import GRADED_SCHEMA, make_contract, render_contract


def scanner():
    matches = list((Path.home() / ".cache/mindie-agent/gitleaks/8.30.1").glob("*/gitleaks"))
    if not matches:
        pytest.skip("pinned local scanner unavailable; tests never download it")
    return str(matches[0])


def seed_graded(tmp_path, remote_url):
    work = tmp_path / "maintainer"
    git(["clone", remote_url, str(work)])
    (work / "publication-contract.json").write_text(render_contract(make_contract("npu", "a" * 40, schema=GRADED_SCHEMA)))
    commit(work, "Select graded contract")
    return work


def commit(work, message):
    git(["add", "-A"], cwd=work)
    git(["-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", message], cwd=work)
    git(["push", "origin", "main"], cwd=work)


def feed(store, remote_url):
    return Feed(store, {"domain": "npu", "repository": REPO, "url": remote_url})


def merge(transport, number):
    pr = transport.get_pull_request(REPO, number, deadline=Deadline(20, 10))
    return transport.merge_pull_request(REPO, number, sha=pr["head"]["sha"], method="squash", deadline=Deadline(20, 10))


def latest(store):
    with store.lock:
        rows = store.db.execute("SELECT * FROM outbox ORDER BY updated DESC").fetchall()
    assert rows
    return dict(rows[0])


def test_c6_outbox_git_feed_feedback_update_and_withdrawal(tmp_path, settings, state_dir, transport, remote_url, monkeypatch):
    maintainer = seed_graded(tmp_path, remote_url)
    calls = tmp_path / "native-calls.txt"
    monkeypatch.setenv("MINDIE_GRADED_FIXTURE_CALLS", str(calls))
    selected = [{"kind": "authorized_transcript", "outcome": "observed", "source_locator": "fixture@abcdef1:probe.txt",
                 "content": "Fixture graph probe; no hardware claim.", "evidence_refs": ["fixture-observation"], "repetitions": 1}]
    with closing(Store(tmp_path / "producer", "npu")) as producer, closing(Store(tmp_path / "consumer", "npu")) as consumer:
        engine = Engine(producer, settings_path=settings["config_path"], state_dir=state_dir,
                        redactor_executable=scanner(),
                        summary_command=[sys.executable, str(Path(__file__).resolve().parents[1] / "fixtures/graded_worker.py")])
        producer_feed, consumer_feed = feed(producer, remote_url), feed(consumer, remote_url)
        assert producer_feed.sync(force=True)["status"] == "synced"
        assert consumer_feed.sync(force=True)["entries"] == 0
        authorization = {"generation": "g1", "session": "fixture-session", "scope": str(tmp_path)}
        applied = engine.distill_selected(selected, session_digest="c" * 64, task_state="completed", authorization=authorization)
        assert applied["status"] == "applied" and applied["publication"] == "eligible_for_existing_outbox"
        engine.publisher.flush()
        first = latest(producer)
        assert first["status"] == "submitted", first["detail"]
        assert calls.read_text() == "1"
        assert consumer.query("graph", conditions={"soc": "Ascend910B"})["results"] == []
        merge(transport, 1)
        assert consumer_feed.sync(force=True)["entries"] == 1
        assert producer_feed.sync(force=True)["entries"] == 1
        found = consumer.query("graph", conditions={"soc": "Ascend910B"})["results"][0]
        old_ref = found["feedback_ref"]
        assert consumer.explain(found["ref"])["revision"] == 1
        assert producer.graded.pending("g1") == []
        vote = consumer.record_vote(root_hash="fixture-root", ref=old_ref, rating="up", reason="Fixture reuse worked.",
                                    publishable=True, generation="g2")
        assert vote["revision"] == 1
        consumer_config = tmp_path / "consumer-settings.json"
        consumer_settings = {**settings, "generation": "g2"}
        consumer_settings.pop("config_path")
        consumer_config.write_text(json.dumps(consumer_settings))
        consumer_engine = Engine(consumer, settings_path=consumer_config, state_dir=tmp_path / "consumer-publisher")
        consumer_engine.publisher.flush()
        voted = latest(consumer)
        assert voted["status"] == "submitted", voted["detail"]
        consumer_transport = FileTransport(tmp_path / "consumer-publisher/dev-github.json", settings["dev_remotes"])
        merge(consumer_transport, 1)
        consumer_feed.sync(force=True)
        producer_feed.sync(force=True)
        assert producer.query("graph", conditions={"soc": "Ascend910B"})["results"][0]["feedback"]["up"] == 1

        # A reviewed correction of the same evidence goes through C6 admission
        # and the identical outbox/publisher. The original paid call is untouched.
        req = prepare_request(selected, session_digest="d" * 64, task_state="completed",
                              redactor_executable=scanner(), redaction_key=producer.redaction_key())
        current = producer.graded._load().units[found["id"]]
        changed = GradedUnit.from_parts(current.metadata, current.body + "Check the corrected graph precondition.\n", current.path)
        from graded_fixture import decision
        correction = apply_decision(producer, req, decision(req, changed, "merge_into_existing"), generation="g1")
        assert correction["admitted_unit_ids"] == [current.id]
        engine.publisher.flush()
        updated = latest(producer)
        assert updated["status"] == "submitted", updated["detail"]
        merge(transport, 2)
        consumer_feed.sync(force=True)
        new = consumer.query("graph", conditions={"soc": "Ascend910B"})["results"][0]
        assert new["revision"] == 2 and new["feedback_ref"] != old_ref
        assert new["feedback"]["up"] == 0
        with pytest.raises(ReadReferenceError, match="replaced"):
            consumer.explain(old_ref)
        old_vote = consumer.record_vote(root_hash="fixture-root", ref=old_ref, rating="down", reason="Observed old revision failed.",
                                        publishable=True, generation="g2")
        assert old_vote["revision"] == 1 and old_vote["content_digest"] == found["content_digest"]

        git(["pull", "--ff-only", "origin", "main"], cwd=maintainer)
        (maintainer / current.path).unlink()
        # Regenerate the ordinary derived files for the reviewed empty corpus.
        from mindie_knowledge.graded.maintenance import build_routing_index
        assert build_routing_index(maintainer)["ok"] is True
        commit(maintainer, "Withdraw synthetic unit")
        assert consumer_feed.sync(force=True)["entries"] == 0
        assert producer_feed.sync(force=True)["entries"] == 0
        assert consumer.query("graph", conditions={"soc": "Ascend910B"})["results"] == []
        assert consumer.unbatched_votes(generation="g2") == []
        with pytest.raises(ReadReferenceError, match="absent"):
            consumer.explain(old_ref)
        assert consumer.db.execute("SELECT count(*) FROM votes WHERE entry_id=?", (current.id,)).fetchone()[0] == 1
        assert calls.read_text() == "1"


def test_graded_publisher_unknown_creation_reconciles_without_repush(tmp_path, settings, state_dir, remote_url):
    seed_graded(tmp_path, remote_url)
    from mindie_knowledge.loop.export import build_batch
    from mindie_knowledge.loop.settings import load

    class LostCreateReply(FileTransport):
        creates = 0
        def create_pull_request(self, *args, **kwargs):
            self.creates += 1
            super().create_pull_request(*args, **kwargs)
            raise UnknownOutcome("fixture reply lost after durable PR creation")

    boundary = LostCreateReply(state_dir / "dev-github.json", settings["dev_remotes"])
    with closing(Store(tmp_path / "source", "npu")) as store:
        feed(store, remote_url).sync(force=True)
        store.graded.install([unit()], generation="g1")
        staged = build_batch(store, settings=load(settings["config_path"]))
        receipt = submit_batch(staged[2], settings, state_dir, transport=boundary)
        assert receipt["status"] == "unknown" and boundary.creates == 1
        branch_heads = git(["show-ref"], cwd=remote_url)
        reconciled = reconcile_batch(staged[0], settings, state_dir, transport=boundary)
        assert reconciled["status"] == "submitted", reconciled
        assert boundary.creates == 1 and git(["show-ref"], cwd=remote_url) == branch_heads
        assert any(item["path"].endswith("unit-fixture-probe.md") for item in reconciled["files"])



def test_graded_path_move_preserves_reference_and_remote_correction_blocks_stale_write(
        tmp_path, settings, state_dir, transport, remote_url):
    maintainer = seed_graded(tmp_path, remote_url)
    from mindie_knowledge.graded.maintenance import build_routing_index
    with closing(Store(tmp_path / "source", "npu")) as store:
        source = feed(store, remote_url)
        source.sync(force=True)
        original = unit()
        store.graded.install([original], generation="g1")
        engine = Engine(store, settings_path=settings["config_path"], state_dir=state_dir)
        engine.publisher.flush()
        assert latest(store)["status"] == "submitted"
        merge(transport, 1)
        source.sync(force=True)
        observed = store.query("graph", conditions={"soc": "Ascend910B", "cann": "8.1",
                                                   "framework": {"torch_npu": "2.5.1"}, "workload_scope": "decode"})["results"][0]
        moved = GradedUnit.from_parts({**original.metadata, "domain_path": "serving/moved"}, original.body,
                                      "serving/moved/" + original.id + ".md")
        store.graded.install([moved], generation="g1")
        engine.publisher.flush()
        move_receipt = latest(store)
        assert move_receipt["status"] == "submitted", move_receipt["detail"]
        merge(transport, 2)
        source.sync(force=True)
        assert store.explain(observed["feedback_ref"])["frontmatter"]["domain_path"] == "serving/moved"
        assert store.explain(observed["feedback_ref"])["revision"] == 1
        assert original.path not in git(["ls-tree", "-r", "--name-only", "main"], cwd=remote_url).splitlines()

        # The producer deliberately stays on its older observed snapshot.
        git(["pull", "--ff-only", "origin", "main"], cwd=maintainer)
        corrected = GradedUnit.from_parts({**moved.metadata, "revision": 2}, moved.body + "Public correction.\n", moved.path)
        (maintainer / moved.path).write_text(corrected.render(), encoding="utf-8", newline="\n")
        assert build_routing_index(maintainer)["ok"]
        commit(maintainer, "Maintainer correction")
        stale = GradedUnit.from_parts({**moved.metadata, "revision": 3}, moved.body + "Stale local rewrite.\n", moved.path)
        store.graded.install([stale], generation="g1")
        heads = git(["show-ref"], cwd=remote_url)
        engine.publisher.flush()
        refused = latest(store)
        assert refused["status"] == "needs_review" and "declared base" in refused["detail"]
        assert git(["show-ref"], cwd=remote_url) == heads
        source.sync(force=True)
        assert store.explain(store.graded.ref(moved.id))["content"] == corrected.body


def test_reviewed_factor_survives_git_feed_and_group_withdrawal(tmp_path, remote_url):
    from mindie_knowledge.graded.index import RoutingIndex
    from mindie_knowledge.graded.maintenance import build_routing_index
    from mindie_knowledge.graded.reviewed_change import SCHEMA, apply_change
    from mindie_knowledge.publication_check import validate

    maintainer = seed_graded(tmp_path, remote_url)
    conditions = {"repository": "fixture/engine", "revision": "a" * 40}
    original = [unit("unit-" + side, conditions=conditions, title=side + " queue",
                     body="Shared premise. Inspect " + side + " queue.")
                for side in ("left", "right")]
    for value in original:
        path = maintainer / value.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value.render(), encoding="utf-8", newline="\n")
    assert build_routing_index(maintainer)["ok"]
    commit(maintainer, "Publish synthetic rules before reviewed factoring")

    def authored(changes):
        return dict(schema=SCHEMA, review_reference="fixture:authored-factor-review",
                    base_generation=RoutingIndex.from_root(maintainer).generation,
                    changes=changes)

    def replacement(old, new):
        return dict(id=new.id, expected_content_sha256=old.content_sha256 if old else None,
                    path=new.path, content=new.render())

    with closing(Store(tmp_path / "consumer", "npu")) as consumer:
        source = feed(consumer, remote_url)
        assert source.sync(force=True)["entries"] == 2
        observed = consumer.query("left queue", conditions=conditions, limit=1)["results"][0]
        common = unit("unit-common", conditions=conditions, domain_path="_common",
                      title="Shared queue premise", body="Shared premise.")
        revised = [GradedUnit.from_parts({**old.metadata, "revision": 2, "inherits_from": [common.id]},
                                        "Inspect " + side + " queue.", old.path)
                   for old, side in zip(original, ("left", "right"))]
        plan = authored([replacement(None, common),
                         *[replacement(old, new) for old, new in zip(original, revised)]])
        assert apply_change(maintainer, plan)["status"] == "applied"
        commit(maintainer, "Publish explicitly reviewed synthetic factor")
        head = git(["rev-parse", "HEAD"], cwd=maintainer).strip()
        assert validate(maintainer, head, "npu", state=str(tmp_path / "check.json"))["entries"] == 3
        assert source.sync(force=True)["entries"] == 3
        factored = consumer.query("left queue", conditions=conditions, limit=1)["results"][0]
        assert factored["id"] == original[0].id and factored["revision"] == 2
        assert consumer.explain(factored["ref"])["content"] == revised[0].body
        assert factored["inherited_rules"][0]["content"] == common.body
        assert consumer.query("left queue", conditions={**conditions, "revision": "b" * 40})["results"] == []
        with pytest.raises(ReadReferenceError, match="replaced"):
            consumer.explain(observed["ref"])
        parent_ref = factored["inherited_rules"][0]["ref"]
        assert consumer.explain(parent_ref)["content"] == common.body

        def delete(value):
            return dict(id=value.id, expected_content_sha256=value.content_sha256, content=None)

        with pytest.raises(ValueError, match="missing inherited unit"):
            apply_change(maintainer, authored([delete(common)]))
        assert git(["status", "--porcelain"], cwd=maintainer) == ""
        assert apply_change(maintainer, authored([delete(value) for value in [*revised, common]]))["status"] == "applied"
        commit(maintainer, "Withdraw complete reviewed synthetic inheritance group")
        assert source.sync(force=True)["entries"] == 0
        assert consumer.query("left queue", conditions=conditions)["results"] == []
        for reference in (factored["ref"], parent_ref):
            with pytest.raises(ReadReferenceError, match="absent"):
                consumer.explain(reference)
