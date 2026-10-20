"""Feed integration tests for contract/2 graded Markdown and real Git snapshots."""
from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from graded_fixture import OBSERVED, unit
from mindie_knowledge.graded.feed import load_snapshot
from mindie_knowledge.graded.index import RoutingIndex
from mindie_knowledge.graded.units import GradedUnit
from mindie_knowledge.loop.feed import Feed
from mindie_knowledge.loop.store import Store
from mindie_knowledge.publication_contract import (
    GRADED_SCHEMA, SCHEMA, make_contract, render_contract,
)

DOMAIN = "vllm-ascend"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def publish(tmp_path, files=None, *, schema=GRADED_SCHEMA, domain=DOMAIN):
    files = dict(files or {})
    repo = tmp_path / "remote"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "feed")
    git(repo, "config", "user.email", "feed@example.invalid")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "config", "core.eol", "lf")
    files["publication-contract.json"] = render_contract(make_contract(domain, "a" * 40, schema=schema))
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "publication")
    return repo, git(repo, "rev-parse", "HEAD")


def commit(repo, message="change"):
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def revised(value, body):
    return GradedUnit.from_parts(
        {**value.metadata, "revision": value.revision + 1},
        body, f"{value.metadata['domain_path']}/{value.id}.md",
    )


def write_units(repo, *units):
    for value in units:
        path = repo / value.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value.render(), encoding="utf-8", newline="\n")


def unit_files(*units):
    return {value.path: value.render() for value in units}


def snapshot(repo, sha, tmp_path):
    return load_snapshot(repo, sha, staging=tmp_path / "staging")


def test_snapshot_parses_units_exact_bytes_and_skips_metadata(tmp_path):
    parent = unit("unit-parent")
    child = unit("unit-child", parents=["unit-parent"])
    files = {**unit_files(parent, child), "README.md": "# feed metadata\n",
             "README.en.md": "# feed metadata in English\n",
             "AGENTS.md": "Publication policy.\n", "docs/guide.md": "# guide\n"}
    repo, sha = publish(tmp_path, files)
    snap = snapshot(repo, sha, tmp_path)
    assert {value.id for value in snap.units} == {"unit-parent", "unit-child"}
    for value in snap.units:
        raw = (repo / value.path).read_bytes()
        assert snap.files[value.path] == hashlib.sha256(raw).hexdigest()
    assert snap.manifest is None and snap.topology is None


def test_snapshot_rejects_legacy_tasks_tree(tmp_path):
    repo, sha = publish(tmp_path, {"tasks/" + "a" * 64 + "/index.md": "old package\n"})
    with pytest.raises(ValueError, match="legacy tasks/"):
        snapshot(repo, sha, tmp_path)


@pytest.mark.parametrize("damage", ["duplicate", "missing-parent", "path", "executable", "private-path"])
def test_snapshot_rejects_malformed_units(tmp_path, damage):
    parent = unit("unit-parent")
    files = unit_files(parent)
    if damage == "duplicate":
        files.update(unit_files(unit("unit-parent", domain_path="serving/other")))
    elif damage == "missing-parent":
        files.update(unit_files(unit("unit-orphan", parents=["unit-absent"])))
    elif damage == "path":
        files["elsewhere/unit-parent.md"] = files.pop(parent.path)
    elif damage == "private-path":
        files.update(unit_files(unit("unit-parent", domain_path="private/hidden")))
    repo, sha = publish(tmp_path, files)
    if damage == "executable":
        git(repo, "add", "--chmod=+x", parent.path)
        git(repo, "commit", "-qm", "executable")
        sha = git(repo, "rev-parse", "HEAD")
    with pytest.raises(ValueError):
        snapshot(repo, sha, tmp_path)


def test_snapshot_reuses_staged_blobs_and_reads_only_changes(tmp_path, monkeypatch):
    import mindie_knowledge.graded.feed as graded_feed
    parent = unit("unit-parent")
    child = unit("unit-child", parents=["unit-parent"])
    repo, first = publish(tmp_path, unit_files(parent, child))
    staged = tmp_path / "staging"
    assert len(load_snapshot(repo, first, staging=staged).units) == 2

    real_read, reads = graded_feed.CatFileBatch.read, []

    def counting(self, rev, **kwargs):
        reads.append(rev)
        return real_read(self, rev, **kwargs)

    monkeypatch.setattr(graded_feed.CatFileBatch, "read", counting)
    assert len(load_snapshot(repo, first, staging=staged).units) == 2
    assert reads == []  # exact verified staging bytes are reused

    updated = revised(child, "Updated child rule; no hardware claim.\n")
    write_units(repo, updated)
    second = commit(repo, "update child")
    snap = load_snapshot(repo, second, staging=staged)
    assert {value.id for value in snap.units} == {"unit-parent", "unit-child"}
    assert len(reads) == 1  # only the changed blob


def test_missing_listed_blob_is_unavailable_not_a_deletion(tmp_path, monkeypatch):
    import mindie_knowledge.graded.feed as graded_feed
    value = unit("unit-parent")
    repo, sha = publish(tmp_path, unit_files(value))
    monkeypatch.setattr(graded_feed.CatFileBatch, "read", lambda *args, **kwargs: None)
    with pytest.raises(OSError, match="unavailable"):
        snapshot(repo, sha, tmp_path)


def test_snapshot_updates_moves_and_removes_units(tmp_path):
    parent = unit("unit-parent")
    child = unit("unit-child", parents=["unit-parent"])
    repo, first = publish(tmp_path, unit_files(parent, child))
    assert {value.id for value in snapshot(repo, first, tmp_path).units} == {"unit-parent", "unit-child"}

    moved = GradedUnit.from_parts({**parent.metadata, "domain_path": "serving/moved"},
                                  parent.body, "serving/moved/unit-parent.md")
    (repo / parent.path).unlink()
    write_units(repo, moved)
    second = commit(repo, "move parent")
    snap = snapshot(repo, second, tmp_path)
    assert {value.id: value.path for value in snap.units}["unit-parent"] == "serving/moved/unit-parent.md"

    (repo / child.path).unlink()
    third = commit(repo, "remove child")
    snap = snapshot(repo, third, tmp_path)
    assert {value.id for value in snap.units} == {"unit-parent"}
    assert snap.files == {moved.path: hashlib.sha256(moved.render().encode("utf-8")).hexdigest()}


def test_snapshot_validates_supported_derived_data_against_units(tmp_path):
    value = unit("unit-parent")
    index = RoutingIndex([value])
    manifest = index.manifest()
    manifest["id_path"] = {row["id"]: row["path"] for row in manifest["units"]}
    manifest["closure"] = {ident: list(parents) for ident, parents in index.closure.items()}
    files = {**unit_files(value), "index/routing-manifest.json": json.dumps(manifest, sort_keys=True) + "\n"}
    repo, sha = publish(tmp_path, files)
    parsed = snapshot(repo, sha, tmp_path)
    assert parsed.manifest is not None
    assert parsed.files["index/routing-manifest.json"] == hashlib.sha256(files["index/routing-manifest.json"].encode()).hexdigest()

    manifest["unit_count"] = 2
    (repo / "index/routing-manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    bad = commit(repo, "lying manifest")
    with pytest.raises(ValueError, match="unit_count"):
        snapshot(repo, bad, tmp_path)


def test_snapshot_uses_the_graded_feedback_authority(tmp_path):
    from mindie_knowledge.graded.feedback import SCHEMA_FEEDBACK
    value = unit("unit-parent")
    vote = {"vote_id": "vote-v2", "root_id": "root-v2", "unit_id": value.id,
            "revision": value.revision,
            "rating": "up", "reason": "Synthetic protocol observation."}
    files = {**unit_files(value),
             "feedback/votes.json": json.dumps({"schema": SCHEMA_FEEDBACK, "votes": [vote]}) + "\n"}
    repo, sha = publish(tmp_path, files)
    snap = snapshot(repo, sha, tmp_path)
    assert snap.feedback_files == 1 and snap.feedback == (vote,)

    files["feedback/votes.json"] = json.dumps(
        {"schema": SCHEMA_FEEDBACK, "votes": [dict(vote, revision=0)]}) + "\n"
    repo2, sha2 = publish(tmp_path / "bad", files)
    with pytest.raises(ValueError, match="revision"):
        snapshot(repo2, sha2, tmp_path / "bad")


def _store_feed(tmp_path, remote):
    store = Store(tmp_path / "store", DOMAIN)
    feed = Feed(store, dict(repository="org/knowledge", ref="main", domain=DOMAIN, url=str(remote)))
    return store, feed


def test_inconsistent_feedback_is_rejected_before_readable_feed_switch(tmp_path):
    from mindie_knowledge.graded.feedback import SCHEMA_FEEDBACK
    from mindie_knowledge.publication_check import validate
    value = unit("unit-vote-target", revision=2)
    repo, first = publish(tmp_path, unit_files(value))
    store, feed = _store_feed(tmp_path, repo)
    try:
        assert feed.sync(force=True)["status"] == "synced"
        observed = store.query("graph", conditions=OBSERVED)["results"][0]
        vote = dict(vote_id="wrong-revision", root_id="reader", unit_id=value.id, revision=0,
                    rating="up", reason="Fixture observation.")
        path = repo / "feedback/votes.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"schema": SCHEMA_FEEDBACK, "votes": [vote]}),
                        encoding="utf-8", newline="\n")
        bad = commit(repo, "invalid feedback identity")
        with pytest.raises(ValueError, match="positive integer"):
            validate(repo, bad, DOMAIN)
        receipt = feed.sync(force=True)
        assert receipt["status"] == "invalid" and receipt["retained_commit"] == first
        assert store.explain(observed["ref"])["revision"] == 2
        assert store.query("graph", conditions=OBSERVED)["results"][0]["feedback"]["total"] == 0
    finally:
        store.close()


def test_feed_sync_v2_uses_the_single_graded_installer_or_reports_dependency(tmp_path):
    parent = unit("unit-parent")
    child = unit("unit-child", parents=["unit-parent"])
    remote, sha = publish(tmp_path, unit_files(parent, child))
    store, feed = _store_feed(tmp_path, remote)
    try:
        receipt = feed.sync()
        if not hasattr(store.graded, "install_feed"):
            assert receipt["status"] == "unavailable"
            assert "GradedRepository.install_feed" in receipt["detail"]
            return
        assert receipt["status"] == "synced", receipt.get("detail")
        assert receipt["entries"] == 2
        assert receipt["contract_schema"] == GRADED_SCHEMA
        assert store.graded.available()
        hits = store.graded.query("graph capture", include_unknown=True)["results"]
        assert {item["id"] for item in hits} == {"unit-parent", "unit-child"}
    finally:
        store.close()


def test_feed_sync_v2_invalid_candidate_keeps_previous_corpus(tmp_path):
    parent = unit("unit-parent")
    remote, first = publish(tmp_path, unit_files(parent))
    store, feed = _store_feed(tmp_path, remote)
    if not hasattr(store.graded, "install_feed"):
        store.close()
        pytest.skip("coordinator GradedRepository.install_feed is still pending")
    try:
        assert feed.sync()["status"] == "synced"
        assert feed.sync()["status"] == "unchanged"
        (remote / parent.path).write_text(
            parent.render().replace("revision: 1", "revision: zero"),
            encoding="utf-8",
            newline="\n",
        )
        second = commit(remote, "tampered unit")
        receipt = feed.sync(force=True)
        assert receipt["status"] == "invalid"
        assert receipt["retained_commit"] == first and receipt["commit"] == second
        assert store.graded.query("graph capture", include_unknown=True)["results"]

        (remote / "tasks").mkdir()
        (remote / "tasks" / ("c" * 64)).mkdir()
        (remote / "tasks" / ("c" * 64) / "index.md").write_text("old package\n", encoding="utf-8")
        third = commit(remote, "legacy layout")
        receipt = feed.sync(force=True)
        assert receipt["status"] == "invalid" and receipt["commit"] == third
        assert receipt["retained_commit"] == first
    finally:
        store.close()


def test_feed_sync_reads_only_changed_blobs_and_revalidates_full_graph(tmp_path, monkeypatch):
    import mindie_knowledge.graded.feed as graded_feed
    parent = unit("unit-parent")
    child = unit("unit-child", parents=[parent.id])
    remote, _ = publish(tmp_path, unit_files(parent, child))
    contract_blob = git(remote, "rev-parse", "HEAD:publication-contract.json")
    store, feed = _store_feed(tmp_path, remote)
    real_read, reads = graded_feed.CatFileBatch.read, []

    def counting(self, revision, **kwargs):
        reads.append(revision)
        return real_read(self, revision, **kwargs)

    monkeypatch.setattr(graded_feed.CatFileBatch, "read", counting)
    try:
        assert feed.sync(force=True)["status"] == "synced"
        assert reads.count(contract_blob) == 1
        assert len(reads) == 3  # current contract plus the two semantic blobs
        reads.clear()
        updated = revised(child, "Updated child rule; no hardware claim.\n")
        write_units(remote, updated)
        second = commit(remote, "update one child")
        # A new Feed object and the ordinary post-sync scratch cleanup must
        # still preserve the exact reusable parent blob.
        feed = Feed(store, dict(repository="org/knowledge", ref="main", domain=DOMAIN, url=str(remote)))
        receipt = feed.sync(force=True)
        assert receipt["status"] == "synced" and receipt["entries"] == 2
        assert reads == [contract_blob, git(remote, "rev-parse", f"{second}:{updated.path}")]
        assert store.graded._load().units[child.id].content_sha256 == updated.content_sha256

        # A cached child cannot keep a deleted parent in the candidate graph.
        reads.clear()
        (remote / parent.path).unlink()
        third = commit(remote, "remove required parent")
        refused = feed.sync(force=True)
        assert reads == [contract_blob]
        assert refused["status"] == "invalid" and refused["retained_commit"] == second
        assert refused["commit"] == third
        assert set(store.graded._load().units) == {parent.id, child.id}
    finally:
        store.close()


def test_feed_sync_v2_wrong_contract_never_silently_uses_v1(tmp_path):
    parent = unit("unit-parent")
    remote, _sha = publish(tmp_path, unit_files(parent), schema=SCHEMA)
    store, feed = _store_feed(tmp_path, remote)
    try:
        receipt = feed.sync()
        assert receipt["status"] == "invalid"
        assert "unsupported content layout" in receipt["detail"]
    finally:
        store.close()


def test_feed_sync_v2_tasks_tree_is_invalid_not_empty(tmp_path):
    remote, _sha = publish(tmp_path, {"tasks/" + "a" * 64 + "/index.md": "old package\n"})
    store, feed = _store_feed(tmp_path, remote)
    try:
        receipt = feed.sync()
        assert receipt["status"] == "invalid"
        assert "legacy tasks/" in receipt["detail"]
        assert store.query("old package")["results"] == []
    finally:
        store.close()


def test_feed_sync_v2_update_move_and_removal(tmp_path):
    parent = unit("unit-parent")
    child = unit("unit-child", parents=["unit-parent"])
    remote, _first = publish(tmp_path, unit_files(parent, child))
    store, feed = _store_feed(tmp_path, remote)
    if not hasattr(store.graded, "install_feed"):
        store.close()
        pytest.skip("coordinator GradedRepository.install_feed is still pending")
    try:
        assert feed.sync()["entries"] == 2

        updated = revised(child, "Updated child rule; no hardware claim.\n")
        (remote / child.path).write_text(updated.render(), encoding="utf-8", newline="\n")
        commit(remote, "update child")
        assert feed.sync()["entries"] == 2
        assert {item["id"] for item in store.graded.query(
            "Updated", include_unknown=True)["results"]} == {"unit-child"}

        moved = GradedUnit.from_parts({**parent.metadata, "domain_path": "serving/moved"},
                                      parent.body, "serving/moved/unit-parent.md")
        (remote / parent.path).unlink()
        write_units(remote, moved)
        commit(remote, "move parent")
        assert feed.sync()["entries"] == 2
        assert {item["id"] for item in store.graded.query(
            "graph capture", include_unknown=True)["results"]} == {"unit-parent", "unit-child"}

        (remote / updated.path).unlink()
        (remote / moved.path).unlink()
        commit(remote, "remove all units")
        assert feed.sync()["entries"] == 0
        assert store.graded.query("graph capture", include_unknown=True)["results"] == []
    finally:
        store.close()
