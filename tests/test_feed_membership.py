"""Multiple sources keep independent membership inside one semantic domain."""
from contextlib import closing
import hashlib
import json
from types import SimpleNamespace

import pytest

from graded_fixture import OBSERVED, unit
from mindie_knowledge.graded.index import read_units
from mindie_knowledge.graded.units import GradedUnit, is_graded_unit_path
from mindie_knowledge.loop.cli import _feeds
from mindie_knowledge.loop.feed import feed_ident
from mindie_knowledge.loop.publication_target import publication_target
from mindie_knowledge.loop.store import Store
from mindie_knowledge.materials import MaterialStore
from mindie_knowledge.materials.references import ReadReferenceError


def package(root, domain, identity):
    task = hashlib.sha256(identity.encode()).hexdigest()
    with closing(MaterialStore(root / identity, domain)) as author:
        author.append_batch(task, [dict(block_id=hashlib.sha256((identity + "block").encode()).hexdigest(),
            text=f"Graph material from {identity}.", title=identity, summary=f"Graph {identity}", source_range={})],
            f"Graph material {identity}.", title=identity, status="complete", promote=True)
        return author.export_task(task)


def test_legacy_sources_withdraw_and_switch_formats_independently(store, tmp_path):
    first, second = package(tmp_path, store.domain, "first"), package(tmp_path, store.domain, "second")
    store.install_feed([first], feed_ident="feed-a", source_revision="a" * 40)
    store.install_feed([second], feed_ident="feed-b", source_revision="b" * 40)
    old_files = list((store.root / "materials" / "tasks" / first["task_id"]).rglob("*.md"))
    assert old_files
    assert {row["entry_id"] for row in store.db.execute("SELECT entry_id FROM entries WHERE feed_active=1")} == {
        first["task_id"], second["task_id"]}
    store.graded.install_feed([unit()], feed_ident="feed-a", source_revision="c" * 40)
    assert store._row(first["task_id"])["feed_active"] == 0
    assert store._row(second["task_id"])["feed_active"] == 1
    assert {row["source"] for row in store.query("graph", conditions=OBSERVED)["results"]} == {"graded", "legacy_material"}
    store.install_feed([], feed_ident="feed-b", source_revision="d" * 40)
    assert store._row(second["task_id"])["feed_active"] == 0
    assert store.explain(store.graded.ref("unit-fixture-probe"))["revision"] == 1
    assert all(path.exists() for path in old_files)


def test_format_switch_preserves_exact_task_bytes_after_restart_and_cleanup(tmp_path):
    domain, root = "operators", tmp_path / "consumer"
    material = package(tmp_path, domain, "retained")
    with closing(Store(root, domain)) as store:
        store.install_feed([material], feed_ident="feed-a", source_revision="a" * 40)
        old_ref = store.query("graph")["results"][0]["ref"]
        original = {path: path.read_bytes() for path in
                    (store.root / "materials" / "tasks" / material["task_id"]).rglob("*.md")}
        store.graded.install_feed([unit()], feed_ident="feed-a", source_revision="b" * 40)
        assert store.materials.current_revisions(material["task_id"]) == {}
    for _ in range(2):
        with closing(Store(root, domain)) as store:
            store.retry_feed_cleanup()
            assert {path: path.read_bytes() for path in original} == original
            assert {row["source"] for row in store.query("graph", conditions=OBSERVED)["results"]} == {"graded"}
            with pytest.raises(ReadReferenceError):
                store.explain(old_ref)


def test_unrelated_graded_sync_materializes_legacy_membership_before_receipt_changes(store, tmp_path):
    material = package(tmp_path, store.domain, "legacy-upgrade")
    store.install_feed([material], feed_ident="feed-a", source_revision="a" * 40)
    # An older runtime kept just this exact published-commit, without the
    # material-feeds map added by the multi-source implementation.
    with store._write_txn():
        store.db.execute("DELETE FROM feed_state WHERE key='material-feeds'")
    store.graded.install_feed([unit()], feed_ident="feed-b", source_revision="b" * 40)
    assert store.material_feeds()["feed-a"] == dict(
        entries={material["task_id"]: material["revision"]}, source_revision="a" * 40)
    store.install_feed([], feed_ident="feed-a", source_revision="c" * 40)
    assert store._row(material["task_id"])["feed_active"] == 0
    assert {row["source"] for row in store.query("graph", conditions=OBSERVED)["results"]} == {"graded"}


def test_material_replacement_withdraws_same_source_graded_authority_and_local_dependents(store, tmp_path):
    settings = SimpleNamespace(repository="fixture/operators", branch="main")
    identity = feed_ident(settings.repository, settings.branch)
    value = unit("unit-replaced-public")
    store.graded.install_feed([value], feed_ident=identity, source_revision="a" * 40)
    child = unit("unit-dependent-local", parents=[value.id])
    store.graded.install([value, child], generation="g1")
    observed = store.query("graph", conditions=OBSERVED)["results"][0]
    store.record_vote(root_hash="reader", ref=observed["feedback_ref"], rating="up", reason="Observed before switch.", publishable=False)
    blobs = {path: path.read_bytes() for path in (store.root / "graded" / "blobs").glob("*.md")}
    material = package(tmp_path, store.domain, "replacement")
    store.install_feed([material], feed_ident=identity, source_revision="b" * 40)
    assert identity not in store.graded._catalog()["feeds"]
    assert store.graded.pending("g1") == []
    assert publication_target(store, settings)["source_commit"] == "b" * 40
    assert {row["source"] for row in store.query("graph", conditions=OBSERVED)["results"]} == {"legacy_material"}
    assert {path: path.read_bytes() for path in blobs} == blobs
    assert store.db.execute("SELECT count(*) FROM votes").fetchone()[0] == 1
    with pytest.raises(ReadReferenceError):
        store.explain(store.graded.ref(value.id))


def test_duplicate_unit_revision_disagreement_is_rejected_before_feed_replacement(store):
    value = unit()
    store.graded.install_feed([value], feed_ident="feed-a", source_revision="a" * 40)
    observed = store.query("graph", conditions=OBSERVED)["results"][0]
    store.record_vote(root_hash="reader", ref=observed["feedback_ref"], rating="up", reason="Observed revision.", publishable=False)
    duplicate = GradedUnit.from_parts({**value.metadata, "revision": 2}, value.body, value.path)
    with pytest.raises(ValueError, match="stable unit revision"):
        store.graded.install_feed([duplicate], feed_ident="feed-b", source_revision="b" * 40)
    assert "feed-b" not in store.graded._catalog()["feeds"]
    assert store.query("graph", conditions=OBSERVED)["results"][0]["revision"] == 1


def test_declared_units_under_scripts_tests_and_materials_are_not_prefiltered(tmp_path):
    areas = ("scripts", "tests", "materials", "build", "shell", "topic/docs")
    for index, area in enumerate(areas):
        value = unit(f"unit-source-{index}", domain_path=area)
        assert is_graded_unit_path(value.path)
        path = tmp_path / value.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value.render(), encoding="utf-8", newline="\n")
    assert {value.metadata["domain_path"] for value in read_units(tmp_path)} == set(areas)


def test_prefixed_feed_does_not_suppress_publication_root_feed(store, tmp_path):
    settings = tmp_path / "community.json"
    settings.write_text(json.dumps(dict(schema="mindie-community-config/1", enabled=False,
                                       repository="fixture/material", branch="main")))
    config = dict(domain=store.domain, community_config=str(settings),
                  feeds=[dict(domain=store.domain, repository="fixture/material", ref="main", prefix="subset")])
    # Use the real settings input contract; no service or Git access is started.
    from mindie_knowledge.loop.settings import from_engine_config
    assert from_engine_config(config).repository == "fixture/material"
    feeds = _feeds(config, store)
    assert {feed.prefix for feed in feeds} == {"", "subset"}


def test_feedback_from_a_feed_without_the_unit_does_not_poison_other_source(store):
    value = unit(revision=2)
    store.graded.install_feed([value], feed_ident='feed-a', source_revision='a' * 40)
    bad = dict(vote_id='foreign-vote', root_id='foreign-root', unit_id=value.id,
               revision=1, rating='up')
    store.graded.install_feed([], feed_ident='feed-b', source_revision='b' * 40, feedback=[bad])
    result = store.query('graph', conditions=OBSERVED)
    assert result['results'][0]['id'] == value.id
    assert store.graded._catalog()['feeds']['feed-b']['feedback'] == [bad]


def test_graded_cutover_retains_legacy_draft_without_freezing_wrong_format(store):
    from mindie_knowledge.loop.export import build_batch, DistillationRequired
    from test_streamed_publication import append
    task, _ = append(store, 1)
    settings = SimpleNamespace(generation='fixture', repository='fixture/demo', branch='main')
    store.graded.install_feed([unit()], feed_ident=feed_ident(settings.repository, settings.branch),
                              source_revision='a' * 40)
    original = store.materials.export_task(task)
    with pytest.raises(DistillationRequired, match='no legacy batch was frozen'):
        build_batch(store, settings=settings)
    assert store.db.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0
    assert store.materials.export_task(task) == original
