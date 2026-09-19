"""Real Git/export boundary: atomic updates, stale revisions and feedback."""

import hashlib
import json
import subprocess
import uuid

import pytest

pytest.importorskip("knowledge_intake")
from knowledge_intake.common import digest
from knowledge_intake.feed_sync import SCHEMA, PROFILE, GitFeed

from mindie_knowledge.loop.feed import Feed
from mindie_knowledge.loop.store import Store


def write_lf(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def init_feed_git(repo):
    """Local Git that stores the exact bytes the producer hashed.

    Windows Git defaults to ``core.autocrlf=true``, which would convert
    hashed working-tree CRLF into LF blobs and fail verification.
    """
    repo.mkdir(parents=True, exist_ok=True)
    (repo / ".gitattributes").write_bytes(b"* -text\n")

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], text=True
        ).strip()

    git("init", "-q")
    git("config", "user.name", "test")
    git("config", "user.email", "test@example.com")
    git("config", "core.autocrlf", "false")
    git("config", "core.eol", "lf")
    return git


def export_notes(notes, repo):
    """Minimal test producer for the verified export v1 protocol.

    Writes current.json plus generations/<id>/ with a prepared manifest and
    every Markdown note paired with retrieval metadata, exactly what the
    independent intake reader verifies.
    """

    def encoded(value):
        return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()

    notes = sorted(notes.rglob("*.md"))
    rows, files = [], {}
    for path in notes:
        relative = path.relative_to(path.parents[1]).as_posix()
        raw = path.read_bytes()
        sidecar = path.with_suffix(".meta.json")
        conditions = (
            json.loads(sidecar.read_text(encoding="utf-8"))["conditions"] if sidecar.exists() else {}
        )
        normalized = digest(raw.decode().replace("\r\n", "\n").replace("\r", "\n").encode())
        metadata = {
            "conditions": conditions,
            "retrieval": {"source_sha256": normalized, "aliases": [], "topics": []},
        }
        metadata_raw = encoded(metadata)
        files[relative] = raw
        files[str(path.with_suffix(".meta.json").relative_to(path.parents[1]).as_posix())] = metadata_raw
        rows.append(
            {
                "path": relative,
                "size": len(raw),
                "sha256": digest(raw),
                "source_sha256": normalized,
                "input_sha256": digest(raw),
                "metadata_size": len(metadata_raw),
                "metadata_sha256": digest(metadata_raw),
            }
        )
    includes = sorted({row["path"].split("/", 1)[0] for row in rows})
    generation = uuid.uuid4().hex
    snapshot = digest(
        encoded({"files": rows, "includes": includes, "redaction_profile": PROFILE})
    )
    manifest = {
        "schema": SCHEMA,
        "redaction_profile": PROFILE,
        "includes": includes,
        "snapshot": snapshot,
        "previous_snapshot": None,
        "files": rows,
        "changes": {"added": [], "removed": [], "updated": [], "renamed": []},
    }
    manifest_raw = encoded(manifest)
    target = repo / "generations" / generation
    for name, raw in files.items():
        (target / name).parent.mkdir(parents=True, exist_ok=True)
        (target / name).write_bytes(raw)
    (target / "prepared.json").write_bytes(manifest_raw)
    (repo / "current.json").write_bytes(
        encoded(
            {
                "schema": SCHEMA,
                "generation": generation,
                "manifest_sha256": digest(manifest_raw),
                "snapshot": snapshot,
            }
        )
    )


@pytest.fixture
def fixture(tmp_path):
    notes, repo = tmp_path / "notes", tmp_path / "git"
    (notes / "topics").mkdir(parents=True)
    (notes / "cases").mkdir()
    (notes / "maintenance").mkdir()
    write_lf(
        notes / "topics/gate.md",
        "# Device gate\n\nC8 requires a declared hardware capability.\n",
    )
    write_lf(
        notes / "topics/gate.meta.json",
        json.dumps({"conditions": {"revision": "abc"}}),
    )
    write_lf(
        notes / "cases/debug.md",
        "# Device gate investigation\n\nTrace the hardware capability before diagnosing kernels.\n",
    )
    write_lf(notes / "maintenance/run.md", "# Operational diary\n\nRun completed.\n")
    git = init_feed_git(repo)

    def publish():
        export_notes(notes, repo)
        git("add", "current.json", "generations")
        git("commit", "-qm", "publish")
        return git("rev-parse", "HEAD")

    first = publish()
    store = Store(tmp_path / "store", "vllm-ascend")
    feed = Feed(
        store,
        dict(
            repository="org/knowledge", ref="knowledge/vllm-ascend", domain=store.domain
        ),
    )
    feed.GitFeed = lambda repository, ref, budget: GitFeed(str(repo), "HEAD", budget)
    yield notes, repo, git, publish, first, store, feed
    store.close()


def test_committed_feed_bytes_match_manifest_hashes(fixture):
    """Working-tree writes, git blobs and manifest hashes are the same bytes."""
    _, repo, _, _, first, _, _ = fixture
    pointer_raw = subprocess.check_output(
        ["git", "-C", str(repo), "cat-file", "blob", f"{first}:current.json"]
    )
    pointer = json.loads(pointer_raw)
    assert (repo / "current.json").read_bytes() == pointer_raw
    generation = pointer["generation"]
    manifest_raw = subprocess.check_output(
        [
            "git",
            "-C",
            str(repo),
            "cat-file",
            "blob",
            f"{first}:generations/{generation}/prepared.json",
        ]
    )
    assert digest(manifest_raw) == pointer["manifest_sha256"]
    assert (repo / "generations" / generation / "prepared.json").read_bytes() == manifest_raw
    for row in json.loads(manifest_raw)["files"]:
        blob = subprocess.check_output(
            [
                "git",
                "-C",
                str(repo),
                "cat-file",
                "blob",
                f"{first}:generations/{generation}/{row['path']}",
            ]
        )
        assert len(blob) == row["size"]
        assert digest(blob) == row["sha256"]
        assert (repo / "generations" / generation / row["path"]).read_bytes() == blob
        assert b"\r\n" not in blob


def test_real_feed_update_reuse_removal_and_retained_explanation(fixture):
    notes, _, _, publish, first, store, feed = fixture
    result = feed.sync(force=True)
    assert result["status"] == "synced", result
    assert result["revision"] == first and result["entries"] == 2
    assert result["skipped"] == ["maintenance/run.md"]
    old = {row["kind"]: row for row in store.query("Device gate")["results"]}
    use = store.use(
        ref=old["experience"]["ref"],
        session_id="consumer",
        application="Traced capability",
        evidence="Gate rejected before kernel",
    )
    store.capture("consumer", "turn", "Capability trace found the rejection")
    store.judge(
        use["use_id"], judge_id="independent", verdict="helpful", reason="Found gate"
    )
    assert feed.sync(force=True)["downloaded_files"] == 0
    write_lf(notes / "topics/gate.md", "# Device gate\n\nUpdated C8 capability gate.\n")
    publish()
    assert feed.sync(force=True)["reused_files"] > 0
    new = {row["kind"]: row for row in store.query("Device gate")["results"]}
    assert new["knowledge"]["ref"] != old["knowledge"]["ref"]
    assert new["experience"]["ref"] == old["experience"]["ref"]
    assert new["experience"]["usefulness"]["helpful"] == 1
    assert store.get(old["knowledge"]["ref"])["content"]
    (notes / "topics/gate.md").unlink()
    (notes / "topics/gate.meta.json").unlink()
    publish()
    assert feed.sync(force=True)["entries"] == 1
    assert {row["kind"] for row in store.query("Device gate")["results"]} == {
        "experience"
    }


def test_invalid_applicability_keeps_whole_old_generation(fixture):
    notes, _, _, publish, first, store, feed = fixture
    assert feed.sync(force=True)["status"] == "synced"
    before = store.query("Device gate")["results"]
    write_lf(notes / "topics/gate.meta.json", '{"conditions": {}}')
    publish()
    assert feed.sync(force=True)["retained_revision"] == first
    assert store.query("Device gate")["results"] == before


def test_tampered_committed_bytes_reject_even_if_cache_claims_unchanged(fixture):
    _, repo, git, _, first, store, feed = fixture
    assert feed.sync(force=True)["status"] == "synced"
    pointer = json.loads((repo / "current.json").read_text())
    path = repo / "generations" / pointer["generation"] / "topics/gate.md"
    write_lf(path, "# Tampered gate\n")
    git("add", "generations")
    git("commit", "-qm", "tamper")
    assert feed.sync(force=True)["retained_revision"] == first
    assert "Tampered" not in str(store.query("gate"))


def test_feed_domain_must_match(fixture):
    *_, store, _ = fixture
    with pytest.raises(ValueError, match="domain"):
        Feed(store, dict(repository="org/knowledge", ref="feed", domain="ascendc"))


def test_export_feed_roundtrip_through_the_verified_reader(tmp_path):
    """The production writer emits a feed the independent reader installs:
    publication, withdrawal in a later generation, failure retaining current."""
    from mindie_knowledge.loop.export import export_feed
    from mindie_knowledge.loop.store import session_key

    repo = tmp_path / "feed"
    git = init_feed_git(repo)

    origin = Store(tmp_path / "origin", "vllm-ascend")
    reader = Store(tmp_path / "reader", "vllm-ascend")
    try:
        knowledge = origin.add(
            kind="knowledge",
            title="Device gate reference",
            content="C8 requires a declared hardware capability.",
            source={"url": "https://example.com/docs", "revision": "abc"},
            conditions={"revision": "abc"},
        )
        exp = origin.add(
            kind="experience",
            title="Device gate investigation",
            content="Trace the hardware capability before diagnosing kernels.",
            producers=[session_key("producer")],
        )
        # Empty authorized set is a valid export: it clears downstream feeds.
        result = export_feed(origin, repo)
        assert result["entries"] == 0
        git("add", ".")
        git("commit", "-qm", "generation 0")
        feed = Feed(
            reader,
            dict(repository="org/knowledge", ref="knowledge/vllm-ascend", domain="vllm-ascend"),
        )
        feed.GitFeed = lambda repository, ref, budget: GitFeed(str(repo), "HEAD", budget)
        assert feed.sync(force=True)["status"] == "synced"
        assert reader.query("Device gate")["results"] == []

        origin.publish(knowledge["id"])
        origin.publish(exp["id"])
        result = export_feed(origin, repo)
        assert result["entries"] == 2 and not result["changes"]["removed"]
        git("add", ".")
        git("commit", "-qm", "generation 1")

        assert feed.sync(force=True)["status"] == "synced"
        found = {row["kind"]: row for row in reader.query("Device gate")["results"]}
        assert set(found) == {"knowledge", "experience"}
        # Canonical identity survives the Git round-trip unchanged.
        assert found["experience"]["ref"] == reader.ref(exp["id"])
        assert found["knowledge"]["ref"] == reader.ref(knowledge["id"])
        reader_exp_ref = found["experience"]["ref"]

        # Withdrawal propagates through the next generation.
        origin.withdraw(exp["id"])
        result = export_feed(origin, repo)
        assert result["changes"]["removed"], result["changes"]
        git("add", ".")
        git("commit", "-qm", "generation 2")
        assert feed.sync(force=True)["status"] == "synced"
        found = {row["kind"] for row in reader.query("Device gate")["results"]}
        assert set(found) == {"knowledge"}
        # Content remains explainable by reference on the reader.
        assert reader.get(reader_exp_ref)["content"]

        # A failed export (private value appears) retains the current pointer.
        # publish() itself would reject this entry, so authorize it directly to
        # simulate a later ruleset tightening or a ledger edit.
        pointer_before = (repo / "current.json").read_text()
        bad = origin.add(
            kind="experience",
            title="Leaky note",
            content="Checked host 192.168.13.153 first.",
        )
        origin.db.execute("INSERT OR IGNORE INTO publication VALUES(?)", (bad["id"],))
        origin.db.commit()
        with pytest.raises(ValueError, match="redaction"):
            export_feed(origin, repo)
        assert (repo / "current.json").read_text() == pointer_before
    finally:
        origin.close()
        reader.close()


def test_export_final_withdrawal_clears_downstream(tmp_path):
    """Withdrawing the last entry produces an empty generation that switches
    the downstream feed to no active entries."""
    from mindie_knowledge.loop.export import export_feed
    from mindie_knowledge.loop.store import session_key

    repo = tmp_path / "feed"
    git = init_feed_git(repo)
    origin = Store(tmp_path / "origin", "vllm-ascend")
    reader = Store(tmp_path / "reader", "vllm-ascend")
    try:
        exp = origin.add(
            kind="experience",
            title="Sole exportable note",
            content="Only one entry is ever published here.",
            producers=[session_key("producer")],
        )
        origin.publish(exp["id"])
        export_feed(origin, repo)
        git("add", ".")
        git("commit", "-qm", "gen1")
        feed = Feed(
            reader,
            dict(repository="org/knowledge", ref="r", domain="vllm-ascend"),
        )
        feed.GitFeed = lambda repository, ref, budget: GitFeed(str(repo), "HEAD", budget)
        assert feed.sync(force=True)["status"] == "synced"
        assert reader.query("Sole exportable")["results"]
        origin.withdraw(exp["id"])
        result = export_feed(origin, repo)
        assert result["entries"] == 0 and result["changes"]["removed"]
        git("add", ".")
        git("commit", "-qm", "gen2-empty")
        assert feed.sync(force=True)["status"] == "synced"
        assert reader.query("Sole exportable")["results"] == []
        assert reader.get(exp["id"])["content"]  # history stays explainable
    finally:
        origin.close()
        reader.close()
