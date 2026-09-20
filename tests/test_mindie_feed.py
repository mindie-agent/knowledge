"""Feed sync against real local Git remotes: atomic switch, retirement,
empty generations, invalid layouts and persisted attempt budgets."""

import json
import subprocess

import pytest

from mindie_knowledge.loop.documents import make_entry, render_entry
from mindie_knowledge.loop.feed import Feed
from mindie_knowledge.loop.store import Store

PRODUCER = "a" * 64


def init_repo(path):
    path.mkdir(parents=True)
    (path / ".gitattributes").write_bytes(b"* -text\n")

    def git(*args):
        return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "test")
    git("config", "user.email", "test@example.com")
    git("config", "core.autocrlf", "false")
    git("config", "core.eol", "lf")
    return git


def entry_doc(entry_id, title, *, kind="experience", conditions=None):
    return make_entry(
        entry_id=entry_id, domain="vllm-ascend", kind=kind, title=title,
        summary=f"Summary of {title}.",
        content=f"Detailed body of {title} with failure and fix context.",
        conditions=conditions or {},
    )


def commit_docs(git, repo, docs):
    for folder in ("cases", "topics"):
        (repo / folder).mkdir(exist_ok=True)
    for old in repo.glob("cases/*.md"):
        old.unlink()
    for old in repo.glob("topics/*.md"):
        old.unlink()
    for doc in docs:
        subdir = "topics" if doc["kind"] == "knowledge" else "cases"
        (repo / subdir / f"{doc['entry_id'][:12]}.md").write_text(
            render_entry(doc), encoding="utf-8", newline="\n"
        )
    git("add", "-A")
    git("commit", "-qm", "publish")
    return git("rev-parse", "HEAD")


@pytest.fixture
def env(tmp_path):
    repo = tmp_path / "remote"
    git = init_repo(repo)
    store = Store(tmp_path / "store", "vllm-ascend")
    feed = Feed(store, dict(repository="org/knowledge", ref="main",
                            domain="vllm-ascend", url=str(repo)))
    yield git, repo, store, feed
    store.close()


def test_sync_installs_and_keeps_exact_history(env):
    git, repo, store, feed = env
    first = commit_docs(git, repo, [entry_doc("1" * 64, "Device gate")])
    receipt = feed.sync()
    assert receipt["status"] == "synced" and receipt["commit"] == first
    hits = store.query("Device gate")["results"]
    assert hits and hits[0]["origin"] == "feed"
    v1 = store.get(hits[0]["ref"])["revision"]
    assert feed.sync()["status"] == "unchanged"
    revised = entry_doc("1" * 64, "Device gate revised")
    second = commit_docs(git, repo, [revised])
    assert feed.sync()["commit"] == second
    # A saved reference stays on its observed revision; re-querying shows the new one.
    assert store.get(hits[0]["ref"])["revision"] == v1
    current = store.get(store.query("Device gate")["results"][0]["ref"])
    assert current["revision"] == revised["revision"]
    pinned = store.get(store.ref("1" * 64, v1))
    assert pinned["title"] == "Device gate"  # exact old body retained


def test_upstream_deletion_withdraws_but_pinned_reads_stay_explicit(env):
    git, repo, store, feed = env
    # A local draft of the same entry exists before publication.
    store.create_draft(kind="experience", title="Old driver note",
                       summary="stale local copy", content="stale body",
                       entry_id="2" * 64, owner=PRODUCER, generation="gen-1")
    commit_docs(git, repo, [entry_doc("2" * 64, "Old driver note")])
    feed.sync()
    hits = store.query("Old driver")["results"]
    assert hits and hits[0]["summary"].startswith("Summary of")  # published body wins
    pinned = hits[0]["ref"]
    commit_docs(git, repo, [])  # deleted from the upstream main tree
    assert feed.sync()["entries"] == 0
    assert store.query("Old driver")["results"] == []  # gone from retrieval
    doc = store.get(pinned)  # cached pinned read remains for history
    assert doc["withdrawn"] is True and "withdrawn" in doc["note"]
    assert "Old driver" in doc["title"]
    assert store.get(store.ref("2" * 64))["withdrawn"] is True
    # The stale local draft neither resurrects the entry in search nor
    # becomes a new publication candidate.
    assert store.drafts_changed(generation="gen-1") == []
    # Old feedback on the withdrawn entry may be recorded but stays local.
    store.record_vote(root_hash="9" * 64, ref=pinned, rating="down",
                      reason="superseded", publishable=True, generation="gen-1")
    assert store.unbatched_votes(generation="gen-1") == []


def test_empty_generation_is_valid_and_clears_search(env):
    git, repo, store, feed = env
    commit_docs(git, repo, [entry_doc("3" * 64, "Temporary note")])
    feed.sync()
    assert store.query("Temporary")["results"]
    commit_docs(git, repo, [])  # empty tree: withdrawal of everything
    assert feed.sync()["entries"] == 0
    assert store.query("Temporary")["results"] == []
    assert store.get(store.ref("3" * 64))["content"]  # still explainable


def test_old_corpus_layout_is_not_an_empty_feed(env):
    git, repo, store, feed = env
    (repo / "corpus" / "references").mkdir(parents=True)
    (repo / "corpus" / "references" / "old.md").write_text("# old\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "old layout")
    receipt = feed.sync()
    assert receipt["status"] == "invalid"
    assert store.query("old")["results"] == []
    # An incompatible candidate stops immediately; attempts do not burn.
    assert feed.sync()["status"] == "invalid"
    candidate = store.feed_get(f"feed-candidate:{feed.ident}")
    assert candidate["status"] == "invalid" and candidate["attempts"] == 1


def test_knowledge_needs_no_header_sources_and_versions_can_be_empty(env):
    git, repo, store, feed = env
    # mindie-entry/2 has no public sources header; citations live in the body.
    commit_docs(git, repo, [entry_doc("4" * 64, "Grounded topic", kind="knowledge")])
    assert feed.sync()["status"] == "synced"
    assert store.query("Grounded")["results"][0]["conditions"] == {}


def test_attempts_persist_across_sync_restarts(env, monkeypatch):
    git, repo, store, feed = env
    commit_docs(git, repo, [entry_doc("5" * 64, "Flaky candidate")])
    original = feed._validate_tree
    monkeypatch.setattr(feed, "_validate_tree",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("network down")))
    for expected in (1, 2, 3):
        assert feed.sync(force=True)["status"] == "unavailable"
        assert store.feed_get(f"feed-candidate:{feed.ident}")["attempts"] == expected
    assert feed.sync(force=True)["status"] == "exhausted"
    monkeypatch.setattr(feed, "_validate_tree", original)
    assert feed.sync(force=True)["status"] == "exhausted"  # no automatic retry

def test_unresolved_remote_stops_after_three_attempts(tmp_path):
    store = Store(tmp_path / 'state', 'vllm-ascend')
    try:
        feed = Feed(store, dict(repository='org/knowledge', ref='main', domain='vllm-ascend',
                                url=str(tmp_path / 'missing-remote')))
        for _ in range(3):
            assert feed.sync()['status'] == 'unavailable'
        assert feed.sync()['status'] == 'exhausted'
        assert store.feed_get(f'feed-discovery:{feed.ident}')['failures'] == 3
        assert feed.sync(force=True)['status'] == 'unavailable'
        assert store.feed_get(f'feed-discovery:{feed.ident}')['failures'] == 1
    finally:
        store.close()
