"""Exact current-member reads, using real files, SQLite and loopback RPC."""
import hashlib
from contextlib import closing
from pathlib import Path
import threading

import pytest

from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store
from mindie_knowledge.loop.transport import RequestRejected, Service, rpc
from mindie_knowledge.materials import MaterialStore
from mindie_knowledge.materials.references import (
    MaterialReadError, ReadReferenceError, block_ref, feedback_ref,
    parse_feedback_ref, parse_read_ref, task_ref,
)
from conftest import make_admission, write_settings


TASK = "a" * 64
DOMAIN = "test"


def block(number, text):
    return dict(block_id=hashlib.sha256(str(number).encode()).hexdigest(), text=text,
                source_range=dict(part=number), title=f"Observation {number}", summary=text[:100])


@pytest.fixture
def corpus(tmp_path):
    author = MaterialStore(tmp_path / "author", DOMAIN)
    author.append_batch(TASK, [block(0, "Initial hypothesis.\n"),
                               block(1, "Middle-stage unique failure evidence.\n"),
                               block(2, "Later correction; acceptance remains incomplete.\n")],
                        "The initial hypothesis was corrected.", title="Long investigation",
                        domain=DOMAIN, status="complete", promote=True)
    store = Store(tmp_path / "consumer", DOMAIN)
    store.install_feed([author.export_task(TASK)], feed_ident="f" * 64)
    try:
        yield store, author
    finally:
        store.close()
        author.close()


def selected_ref(store, position=1):
    task = store.materials.read_task(TASK, source="feed")
    item = task["blocks"][position]
    return block_ref(DOMAIN, TASK, item["block_id"], item["sha256"])


def track_body_reads(monkeypatch):
    reads = []
    original = Path.read_bytes

    def read(path):
        if path.parent.name == "blocks":
            reads.append(path.name)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    return reads


def test_middle_block_reads_exactly_one_body_and_navigation_reads_none(corpus, monkeypatch):
    store, _ = corpus
    ref = selected_ref(store)
    changes = store.db.total_changes
    reads = track_body_reads(monkeypatch)
    monkeypatch.setattr(store, "get", lambda *_: pytest.fail("public read assembled an internal document"))
    monkeypatch.setattr(store.materials, "get_document", lambda *_a, **_k: pytest.fail("public read assembled a task"))
    navigation = store.explain(task_ref(DOMAIN, TASK))
    assert reads == []
    assert navigation["kind"] == "task" and navigation["block_count"] == 3
    assert "content" not in navigation
    assert navigation["current_navigation"]["advisory"] is True
    result = store.explain(ref)
    assert result["content"] == "Middle-stage unique failure evidence.\n"
    assert reads == [parse_read_ref(ref)["block_id"] + ".md"]
    assert result["previous_block_ref"] == navigation["first_block_ref"]
    assert result["next_block_ref"] is not None
    assert result["feedback_ref"] == navigation["feedback_ref"]
    assert store.db.total_changes == changes  # no observation bookkeeping in reads


def test_append_changes_navigation_without_invalidating_body_or_observed_vote(corpus, monkeypatch):
    store, author = corpus
    ref = selected_ref(store)
    observed = store.explain(ref)
    author.append_batch(TASK, [block(3, "A subsequent independent failure was observed.\n")],
                        "Four observations; still incomplete.", status="complete", promote=True)
    store.install_feed([author.export_task(TASK)], feed_ident="f" * 64)
    current = store.explain(ref)
    assert current["ref"] == observed["ref"]
    assert current["content"] == observed["content"]
    assert current["current_revision"] != observed["current_revision"]
    assert current["current_navigation"]["summary"] != observed["current_navigation"]["summary"]
    assert current["feedback_ref"] != observed["feedback_ref"]
    reads = track_body_reads(monkeypatch)
    vote = store.record_vote(root_hash="root", ref=observed["feedback_ref"],
                             rating="down", reason="A later counterexample.", publishable=False)
    assert vote["revision"] == observed["current_revision"]
    assert reads == []
    assert store.db.execute("SELECT count(*) FROM revisions WHERE entry_id=?", (TASK,)).fetchone()[0] == 1
    assert store.db.execute("PRAGMA table_info(known_revisions)").fetchall()[0][1] == "entry_id"
    assert {r[1] for r in store.db.execute("PRAGMA table_info(known_revisions)")} == {"entry_id", "revision"}
    manifests = list((store.materials.root / "tasks" / TASK / "manifests").glob("*.md"))
    assert len(manifests) == 1


def test_removed_member_cannot_be_read_from_an_unreferenced_leftover(corpus):
    store, _ = corpus
    ref = selected_ref(store)
    parsed = parse_read_ref(ref)
    path = store.materials.root / "tasks" / TASK / "blocks" / (parsed["block_id"] + ".md")
    previous = path.read_bytes()
    replacement = MaterialStore(store.root / "replacement", DOMAIN)
    try:
        replacement.append_batch(TASK, [block(7, "A maintainer-replaced current observation.\n")],
                                 "Current replacement.", title="Investigation", status="complete", promote=True)
        store.install_feed([replacement.export_task(TASK)], feed_ident="f" * 64)
    finally:
        replacement.close()
    path.write_bytes(previous)  # simulate deferred cleanup, not an admitted member
    with pytest.raises(ReadReferenceError) as caught:
        store.explain(ref)
    assert caught.value.code == "removed_or_superseded"
    assert caught.value.read_ref == task_ref(DOMAIN, TASK)
    assert "content" not in store.explain(caught.value.read_ref)


def test_navigation_only_revision_keeps_block_and_observed_feedback_after_restart(corpus, monkeypatch):
    store, author = corpus
    ref = selected_ref(store)
    observed = store.explain(ref)
    author.update_blocks(TASK, [], "The corrected navigation remains advisory.",
                         title="Corrected investigation", status="complete", promote=True)
    store.install_feed([author.export_task(TASK)], feed_ident="f" * 64)
    current = store.explain(ref)
    assert current["content"] == observed["content"]
    assert current["current_revision"] != observed["current_revision"]
    assert current["current_navigation"]["title"] == "Corrected investigation"
    with closing(Store(store.root.parent, DOMAIN)) as reopened:
        reads = track_body_reads(monkeypatch)
        vote = reopened.record_vote(root_hash="root", ref=observed["feedback_ref"],
                                    rating="down", reason="Correction", publishable=False)
        assert vote["revision"] == observed["current_revision"]
        assert reads == []


def test_withdrawal_is_distinct_from_removed_member(corpus):
    store, _ = corpus
    ref = selected_ref(store)
    store.install_feed([], feed_ident="f" * 64)
    for requested in (ref, task_ref(DOMAIN, TASK)):
        with pytest.raises(ReadReferenceError) as caught:
            store.explain(requested)
        assert caught.value.code == "withdrawn" and caught.value.read_ref is None


def test_current_identity_is_available_after_opening_an_existing_v4_store(corpus):
    store, _ = corpus
    ref = store.explain(selected_ref(store))["feedback_ref"]
    with store.db:
        store.db.execute("DROP TABLE known_revisions")
    with closing(Store(store.root.parent, DOMAIN)) as reopened:
        vote = reopened.record_vote(root_hash="root", ref=ref, rating="up", reason="", publishable=False)
        assert vote["revision"] == parse_feedback_ref(ref)["revision"]


@pytest.mark.parametrize("damage", ["missing", "modified", "symlink"])
def test_required_member_failure_is_not_normal_expiry(corpus, damage, tmp_path):
    store, _ = corpus
    ref = selected_ref(store)
    path = store.materials.root / "tasks" / TASK / "blocks" / (parse_read_ref(ref)["block_id"] + ".md")
    if damage == "missing":
        path.unlink()
    elif damage == "modified":
        path.write_bytes(path.read_bytes() + b"not the admitted bytes")
    else:
        target = tmp_path / "unrelated.txt"
        target.write_text("never read this replacement")
        path.unlink()
        try:
            path.symlink_to(target)
        except OSError:
            pytest.skip("host does not permit creating test symlinks")
    with pytest.raises(MaterialReadError) as caught:
        store.explain(ref)
    assert caught.value.code == "material_corrupt"
    assert not isinstance(caught.value, ReadReferenceError)


@pytest.mark.parametrize("damage", ["missing_manifest", "modified_manifest", "missing_pointer"])
def test_required_navigation_failure_is_material_corruption(corpus, damage):
    store, _ = corpus
    requested = task_ref(DOMAIN, TASK)
    revision = store.explain(requested)["current_revision"]
    manifest = store.materials.root / "tasks" / TASK / "manifests" / (revision + ".md")
    if damage == "missing_manifest":
        manifest.unlink()
    elif damage == "modified_manifest":
        manifest.write_bytes(manifest.read_bytes() + b"changed navigation")
    else:
        (store.materials.root / "current.json").unlink()
    with pytest.raises(MaterialReadError) as caught:
        store.explain(requested)
    assert caught.value.code == "material_corrupt"


def test_wrong_reference_digest_does_not_read_another_body(corpus, monkeypatch):
    store, _ = corpus
    parsed = parse_read_ref(selected_ref(store))
    ref = block_ref(DOMAIN, TASK, parsed["block_id"], "0" * 64)
    reads = track_body_reads(monkeypatch)
    with pytest.raises(ReadReferenceError) as caught:
        store.explain(ref)
    assert caught.value.code == "removed_or_superseded"
    assert reads == []


def test_reference_types_do_not_guess_missing_identity_or_observation(corpus):
    store, _ = corpus
    navigation = store.explain(task_ref(DOMAIN, TASK))
    with pytest.raises(ReadReferenceError, match="feedback only") as caught:
        store.explain(navigation["feedback_ref"])
    assert caught.value.read_ref == navigation["ref"]
    for ref in (TASK, TASK[:16], task_ref(DOMAIN, TASK) + "@" + "b" * 16,
                task_ref("foreign", TASK), selected_ref(store) + "/extra"):
        with pytest.raises(ReadReferenceError):
            store.explain(ref)
    for ref in (navigation["ref"], navigation["first_block_ref"]):
        with pytest.raises(ReadReferenceError, match="feedback_ref"):
            store.record_vote(root_hash="root", ref=ref, rating="up", reason="", publishable=False)
    assert parse_feedback_ref(navigation["feedback_ref"], domain=DOMAIN)["revision"] == navigation["current_revision"]
    with pytest.raises(TypeError, match="offset"):
        store.explain(navigation["ref"], offset=0)
    with pytest.raises(TypeError, match="limit"):
        store.explain(navigation["ref"], limit=1)
    with pytest.raises(ValueError, match="unknown observed revision"):
        store.record_vote(root_hash="root", ref=feedback_ref(DOMAIN, TASK, "0" * 64),
                          rating="up", reason="", publishable=False)


def test_loopback_preserves_unavailable_and_corrupt_failure_meanings(corpus, tmp_path):
    store, _ = corpus
    settings = tmp_path / "community.json"
    write_settings(settings, enabled=False, roots=[tmp_path])
    admission = Admission(make_admission(tmp_path, project_root=tmp_path))
    engine = Engine(store, settings_path=settings, admission=admission)
    service = Service(engine, admission=admission)
    thread = threading.Thread(target=service.http.serve_forever, daemon=True)
    thread.start()
    identity = dict(_session_id="manual-A", _session_verified=True)
    try:
        ref = selected_ref(store)
        result = rpc(service.connection, "explain", dict(identity, ref=ref))
        assert result["content"] == "Middle-stage unique failure evidence.\n"
        with pytest.raises(RequestRejected) as caught:
            rpc(service.connection, "explain", dict(identity, ref=result["feedback_ref"]))
        assert caught.value.error_code == "reference_invalid"
        assert caught.value.read_ref == result["task_ref"]
        with pytest.raises(RequestRejected, match="only ref"):
            rpc(service.connection, "explain", dict(identity, ref=ref, offset=0))
        path = store.materials.root / "tasks" / TASK / "blocks" / (result["block_id"] + ".md")
        path.unlink()
        with pytest.raises(MaterialReadError) as caught:
            rpc(service.connection, "explain", dict(identity, ref=ref))
        assert caught.value.code == "material_corrupt"
        assert not isinstance(caught.value, RequestRejected)
        store.install_feed([], feed_ident="f" * 64)
        with pytest.raises(RequestRejected) as caught:
            rpc(service.connection, "explain", dict(identity, ref=ref))
        assert caught.value.error_code == "withdrawn"
    finally:
        service.http.shutdown()
        service.http.server_close()
        thread.join(timeout=5)
