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
            json.loads(sidecar.read_text())["conditions"] if sidecar.exists() else {}
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
    (notes / "topics/gate.md").write_text(
        "# Device gate\n\nC8 requires a declared hardware capability.\n"
    )
    (notes / "topics/gate.meta.json").write_text(
        json.dumps({"conditions": {"revision": "abc"}})
    )
    (notes / "cases/debug.md").write_text(
        "# Device gate investigation\n\nTrace the hardware capability before diagnosing kernels.\n"
    )
    (notes / "maintenance/run.md").write_text("# Operational diary\n\nRun completed.\n")
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], text=True
        ).strip()

    git("init", "-q")
    git("config", "user.name", "test")
    git("config", "user.email", "test@example.com")

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
    (notes / "topics/gate.md").write_text(
        "# Device gate\n\nUpdated C8 capability gate.\n"
    )
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
    (notes / "topics/gate.meta.json").write_text('{"conditions": {}}')
    publish()
    assert feed.sync(force=True)["retained_revision"] == first
    assert store.query("Device gate")["results"] == before


def test_tampered_committed_bytes_reject_even_if_cache_claims_unchanged(fixture):
    _, repo, git, _, first, store, feed = fixture
    assert feed.sync(force=True)["status"] == "synced"
    pointer = json.loads((repo / "current.json").read_text())
    path = repo / "generations" / pointer["generation"] / "topics/gate.md"
    path.write_text("# Tampered gate\n")
    git("add", "generations")
    git("commit", "-qm", "tamper")
    assert feed.sync(force=True)["retained_revision"] == first
    assert "Tampered" not in str(store.query("gate"))


def test_feed_domain_must_match(fixture):
    *_, store, _ = fixture
    with pytest.raises(ValueError, match="domain"):
        Feed(store, dict(repository="org/knowledge", ref="feed", domain="ascendc"))
