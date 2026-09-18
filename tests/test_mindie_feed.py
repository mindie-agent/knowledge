"""Real Git/export boundary: atomic updates, stale revisions and feedback."""

import json
import subprocess

import pytest

pytest.importorskip("knowledge_intake")
from knowledge_intake.feed_sync import GitFeed

from mindie_knowledge.curation_export import export_notes
from mindie_knowledge.loop.feed import Feed
from mindie_knowledge.loop.store import Store


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
