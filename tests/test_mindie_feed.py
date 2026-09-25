"""Feed sync against real local Git remotes: atomic switch, retirement,
empty generations, invalid layouts and persisted attempt budgets."""

import json
import subprocess
import time

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


def test_transient_candidate_failure_backs_off_and_recovers(env, monkeypatch):
    git, repo, store, feed = env
    commit_docs(git, repo, [entry_doc("5" * 64, "Flaky candidate")])
    clock = [10000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    original = feed._validate_listing
    monkeypatch.setattr(feed, "_validate_listing",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("network down")))
    # Attempts are persisted across restarts, and each failure backs the same
    # candidate off exponentially instead of exhausting permanently.
    assert feed.sync()["status"] == "unavailable"
    candidate = store.feed_get(f"feed-candidate:{feed.ident}")
    assert candidate["attempts"] == 1 and candidate["next_check"] > clock[0]
    clock[0] += 61
    assert feed.sync()["status"] == "unavailable"
    candidate = store.feed_get(f"feed-candidate:{feed.ident}")
    assert candidate["attempts"] == 2
    assert candidate["next_check"] >= clock[0] + 119  # backoff grows
    # While the backoff is due nothing revalidates; this is a deferral with a
    # persisted next check, never a permanent exhaustion.
    assert feed.sync()["status"] == "deferred"
    assert store.feed_get(f"feed-candidate:{feed.ident}")["attempts"] == 2
    assert feed.sync()["status"] == "deferred"
    # An explicit resume rechecks immediately, even mid-backoff.
    assert feed.sync(force=True)["status"] == "unavailable"
    assert store.feed_get(f"feed-candidate:{feed.ident}")["attempts"] == 3
    # Once due, an ordinary sync retries the same candidate and recovers.
    monkeypatch.setattr(feed, "_validate_listing", original)
    clock[0] += 241
    assert feed.sync()["status"] == "synced"
    candidate = store.feed_get(f"feed-candidate:{feed.ident}")
    assert candidate["status"] == "ok" and candidate["attempts"] == 0
    assert store.query("Flaky candidate")["results"]


def test_invalid_candidate_stays_quarantined_under_resume(env):
    git, repo, store, feed = env
    (repo / "corpus").mkdir()
    (repo / "corpus" / "old.md").write_text("# old\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "unsupported layout")
    assert feed.sync()["status"] == "invalid"
    # Explicit resume never revalidates an invalid immutable candidate and
    # never burns further attempts on it.
    assert feed.sync(force=True)["status"] == "invalid"
    candidate = store.feed_get(f"feed-candidate:{feed.ident}")
    assert candidate["status"] == "invalid" and candidate["attempts"] == 1


def test_unresolved_remote_backs_off_one_hour_then_rediscovers(tmp_path):
    store = Store(tmp_path / 'state', 'vllm-ascend')
    try:
        feed = Feed(store, dict(repository='org/knowledge', ref='main', domain='vllm-ascend',
                                url=str(tmp_path / 'missing-remote')))
        for _ in range(3):
            assert feed.sync()['status'] == 'unavailable'
        discovery = store.feed_get(f'feed-discovery:{feed.ident}')
        assert discovery['failures'] == 3
        assert discovery['next_check'] > time.time() + 3500  # one-hour cadence
        # Deferred ordinary calls stay off the network while backoff is due.
        assert feed.sync()['status'] == 'deferred'
        assert feed.sync()['status'] == 'deferred'
        assert store.feed_get(f'feed-discovery:{feed.ident}')['failures'] == 3
        # Explicit resume skips the backoff and makes one bounded pass now.
        assert feed.sync(force=True)['status'] == 'unavailable'
        assert store.feed_get(f'feed-discovery:{feed.ident}')['failures'] == 4
        # Once the backoff is due, an ordinary call discovers again.
        store.feed_set(f'feed-discovery:{feed.ident}',
                       dict(store.feed_get(f'feed-discovery:{feed.ident}'),
                            next_check=time.time() - 1))
        assert feed.sync()['status'] == 'unavailable'
        assert store.feed_get(f'feed-discovery:{feed.ident}')['failures'] == 5
    finally:
        store.close()


def test_legacy_exhausted_discovery_without_next_check_is_due_immediately(env):
    git, repo, store, feed = env
    commit = commit_docs(git, repo, [entry_doc("6" * 64, "Stranded latch")])
    # State persisted by the old permanent latch has no next_check.
    store.feed_set(f"feed-discovery:{feed.ident}", {"failures": 3})
    assert feed.sync()["status"] == "synced"
    assert feed.sync()["commit"] == commit
    discovery = store.feed_get(f"feed-discovery:{feed.ident}")
    assert discovery["failures"] == 0 and "next_check" not in discovery


def test_offline_recovery_through_real_git_remote(env, tmp_path):
    git, repo, store, feed = env
    commit = commit_docs(git, repo, [entry_doc("7" * 64, "Returns after outage")])
    hidden = tmp_path / "hidden-remote"
    repo.rename(hidden)  # offline: the configured URL no longer resolves
    try:
        for _ in range(3):
            assert feed.sync()["status"] == "unavailable"
        assert feed.sync()["status"] == "deferred"
        # Connectivity restored; the backoff is still due, so ordinary callers
        # do not hammer the network, but nothing is permanently latched.
        assert feed.sync()["status"] == "deferred"
    finally:
        hidden.rename(repo)
    store.feed_set(f"feed-discovery:{feed.ident}",
                   dict(store.feed_get(f"feed-discovery:{feed.ident}"),
                        next_check=time.time() - 1))
    receipt = feed.sync()  # ordinary invocation discovers again once due
    assert receipt["status"] == "synced" and receipt["commit"] == commit
    assert store.feed_get(f"feed-discovery:{feed.ident}")["failures"] == 0
    assert store.query("Returns after outage")["results"]


def test_same_commit_refresh_drops_stale_unavailable_recovery_fields(env):
    git, repo, store, feed = env
    commit = commit_docs(git, repo, [entry_doc("8" * 64, "Recovered same commit")])
    first = feed.sync()
    assert first["status"] == "synced" and first["commit"] == commit
    entries = first["entries"]
    store.feed_set(
        f"feed:{feed.ident}",
        dict(
            first,
            status="unavailable",
            detail="HTTP 404: remote not found",
            retained_commit=commit,
            checked=time.time(),
        ),
    )
    receipt = feed.sync()
    assert receipt["status"] == "unchanged"
    assert receipt["commit"] == commit
    assert receipt["entries"] == entries
    assert "detail" not in receipt
    assert "retained_commit" not in receipt
    hits = store.query("Recovered same commit")["results"]
    assert hits and "Recovered same commit" in store.get(hits[0]["ref"])["title"]


def test_interrupted_candidate_resumes_from_staged_progress(env, monkeypatch):
    """A mid-candidate failure keeps verified per-blob progress: the next
    attempt continues after the last staged blob, never from item zero."""
    git, repo, store, feed = env
    docs = [entry_doc(f"{i:04x}" + "0" * 60, f"Staged case {i}") for i in range(6)]
    commit = commit_docs(git, repo, docs)
    from mindie_knowledge import gitread

    reads = []
    real_read = gitread.CatFileBatch.read
    fail = {"armed": True}

    def counting_read(self, rev, **kwargs):
        if fail["armed"] and len(reads) == 3:
            fail["armed"] = False
            reads.append(rev)
            raise OSError("network down mid-candidate")
        reads.append(rev)
        return real_read(self, rev, **kwargs)

    monkeypatch.setattr(gitread.CatFileBatch, "read", counting_read)
    assert feed.sync()["status"] == "unavailable"
    assert store.feed_staging_count(feed.ident, commit) == 3
    first_attempt = len(reads)
    receipt = feed.sync(force=True)  # explicit resume skips the backoff
    assert receipt["status"] == "synced" and receipt["entries"] == 6
    # The second attempt read only the three still-unverified blobs: verified
    # progress was kept, no restart at item zero.
    assert first_attempt == 4  # three staged, the fourth hit the fault
    assert len(reads) - first_attempt == 3
    assert store.feed_staging_count(feed.ident, commit) == 0
    assert store.query("Staged case 4")["results"]


def test_feed_grows_past_1024_entries(env):
    """No whole-feed count or byte total rejects a growing knowledge base."""
    git, repo, store, feed = env
    # Distinct leading hex so the entry-id-derived filenames do not collide.
    docs = [entry_doc(f"{i:04x}" + "0" * 60, f"Accumulated case {i}") for i in range(1026)]
    commit_docs(git, repo, docs)
    receipt = feed.sync()
    assert receipt["status"] == "synced", receipt.get("detail")
    assert receipt["entries"] == 1026
    assert store.query("Accumulated case 1000")["results"]
    assert store.get(store.ref(f"{1025:04x}" + "0" * 60))["title"] == "Accumulated case 1025"
    # And the same corpus revalidates as unchanged without restaging.
    assert feed.sync()["status"] == "unchanged"
