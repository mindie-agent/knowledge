"""Sync state machine: switch, failure keeps old version, resume, prune, lock."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from distribution.helpers import (
    GIT_SHA,
    GIT_SHA_2,
    GIT_SHA_3,
    DOC_A,
    FakeClient,
    embedding_info,
    make_release_dir,
    metrics_reader_from,
)

from mindie_knowledge.distribution.errors import SwitchInProgress
from mindie_knowledge.distribution.manifest import version_id_from_sha
from mindie_knowledge.distribution.sync import (
    DistributionState,
    SwitchLock,
    check_and_sync,
    current_shared,
)


def _sync(state_root, release_dir, client, **kwargs):
    return check_and_sync(
        state_root,
        str(release_dir),
        embedding_info=embedding_info(),
        client=client,
        **kwargs,
    )


def test_first_switch_activates_version(tmp_path):
    release = make_release_dir(tmp_path / "rel")
    client = FakeClient()
    result = _sync(tmp_path / "state", release, client, smoke_query="alpha")
    assert result.status == "switched", result.reason
    version_id = version_id_from_sha(GIT_SHA)
    assert result.root_uri == f"viking://resources/shared/{version_id}"
    assert client.import_kwargs == {"on_conflict": "fail", "vector_mode": "require"}
    assert result.details["import_embedding_calls"] == {}
    assert result.details["smoke_query_hits"] > 0

    current = current_shared(tmp_path / "state")
    assert current is not None
    assert {"source_git_sha", "root_uri", "manifest_path"} <= set(current)
    assert current["metadata_status"] == "unavailable_legacy"
    assert Path(current["prepared_root"]).is_dir()
    assert current["source_git_sha"] == GIT_SHA
    assert current["root_uri"] == result.root_uri
    assert Path(current["manifest_path"]).is_file()
    payload = json.loads((tmp_path / "state" / "distribution" / "current.json").read_text())
    assert payload["embedding"]["dimension"] == 384


def test_no_change_is_quiet(tmp_path):
    release = make_release_dir(tmp_path / "rel")
    client = FakeClient()
    state = tmp_path / "state"
    assert _sync(state, release, client).status == "switched"
    again = FakeClient()
    result = _sync(state, release, again)
    assert result.status == "unchanged"
    assert not again.calls  # never touches the instance on no-change


def test_offline_source_keeps_old_version(tmp_path):
    release = make_release_dir(tmp_path / "rel")
    client = FakeClient()
    state = tmp_path / "state"
    assert _sync(state, release, client).status == "switched"
    result = check_and_sync(
        state, str(tmp_path / "unreachable"), embedding_info=embedding_info(), client=client
    )
    assert result.status == "offline"
    assert "not reachable" in result.reason
    assert current_shared(state)["source_git_sha"] == GIT_SHA


def test_corrupt_pack_keeps_old_version(tmp_path):
    state = tmp_path / "state"
    client = FakeClient()
    assert _sync(state, make_release_dir(tmp_path / "rel1"), client).status == "switched"
    release2 = make_release_dir(tmp_path / "rel2", sha=GIT_SHA_2)
    pack = next(release2.glob("*.ovpack"))
    raw = bytearray(pack.read_bytes())
    raw[-40:] = b"\x00" * 40  # tamper after release.json was written
    pack.write_bytes(raw)
    result = _sync(state, tmp_path / "rel2", client)
    assert result.status == "corrupt", result.reason
    assert current_shared(state)["source_git_sha"] == GIT_SHA
    assert not any((tmp_path / "state" / "distribution" / "staging").iterdir())


def test_wrong_model_is_incompatible(tmp_path):
    release = make_release_dir(tmp_path / "rel", model="other-model")
    result = _sync(tmp_path / "state", release, FakeClient())
    assert result.status == "incompatible"
    assert "model" in result.reason
    assert current_shared(tmp_path / "state") is None


def test_live_embedding_contract_is_enforced(tmp_path):
    release = make_release_dir(tmp_path / "rel")
    result = check_and_sync(
        tmp_path / "state",
        str(release),
        embedding_info=embedding_info(dimension=768),
        client=FakeClient(),
    )
    assert result.status == "incompatible"
    assert "768" in result.reason


def test_import_failure_keeps_old_version(tmp_path):
    state = tmp_path / "state"
    good = FakeClient()
    assert _sync(state, make_release_dir(tmp_path / "rel1"), good).status == "switched"
    failing = FakeClient(fail_import=True)
    result = _sync(state, make_release_dir(tmp_path / "rel2", sha=GIT_SHA_2), failing)
    assert result.status == "error"
    assert "previous version remains active" in result.reason
    assert current_shared(state)["source_git_sha"] == GIT_SHA


def test_no_instance_is_actionable(tmp_path):
    release = make_release_dir(tmp_path / "rel")
    result = check_and_sync(tmp_path / "state", str(release), embedding_info=embedding_info())
    assert result.status == "error"
    assert "no live OpenViking instance" in result.reason
    assert current_shared(tmp_path / "state") is None


def test_interrupted_switch_resumes_without_reimport(tmp_path):
    """Crash after import but before the pointer switch: next run resumes."""
    state = tmp_path / "state"
    release = make_release_dir(tmp_path / "rel")
    client = FakeClient()

    real_import = client.import_ovpack

    def crash_after_import(*args, **kwargs):
        uri = real_import(*args, **kwargs)
        raise RuntimeError("simulated power loss before pointer switch")

    client.import_ovpack = crash_after_import  # type: ignore[assignment]
    first = _sync(state, release, client)
    assert first.status == "error"
    assert current_shared(state) is None

    client.import_ovpack = real_import  # type: ignore[assignment]
    imports_before = len([c for c in client.calls if c[0] == "import_ovpack"])
    second = _sync(state, release, client)
    assert second.status == "switched", second.reason
    assert second.details.get("import_resumed") is True
    assert len([c for c in client.calls if c[0] == "import_ovpack"]) == imports_before
    assert current_shared(state)["source_git_sha"] == GIT_SHA


def test_reembedding_during_import_is_rejected(tmp_path):
    release = make_release_dir(tmp_path / "rel")
    counter = {"texts": 0, "generation_rejected": 0}
    client = FakeClient()
    real_import = client.import_ovpack

    def reembedding_import(*args, **kwargs):
        counter["texts"] += 5  # a silent re-embed would show up here
        return real_import(*args, **kwargs)

    client.import_ovpack = reembedding_import  # type: ignore[assignment]
    result = _sync(
        tmp_path / "state", release, client, metrics_reader=metrics_reader_from(counter)
    )
    assert result.status == "error"
    assert "must not recompute" in result.reason
    assert current_shared(tmp_path / "state") is None


def test_query_phase_embedding_calls_are_recorded(tmp_path):
    release = make_release_dir(tmp_path / "rel")
    counter = {"texts": 0, "generation_rejected": 0}
    client = FakeClient()
    real_find = client.find

    def querying_find(*args, **kwargs):
        counter["texts"] += 1  # query vectors are computed locally; that is expected
        return real_find(*args, **kwargs)

    client.find = querying_find  # type: ignore[assignment]
    result = _sync(
        tmp_path / "state",
        release,
        client,
        metrics_reader=metrics_reader_from(counter),
        smoke_query="alpha",
    )
    assert result.status == "switched", result.reason
    assert result.details["import_embedding_calls"].get("texts", 0) == 0
    assert result.details["query_embedding_calls"]["texts"] == 1


def test_prune_keeps_current_and_one_inactive(tmp_path):
    state = tmp_path / "state"
    client = FakeClient()
    for sha, marker in ((GIT_SHA, "rel1"), (GIT_SHA_2, "rel2"), (GIT_SHA_3, "rel3")):
        release = make_release_dir(tmp_path / marker, sha=sha)
        assert _sync(state, release, client).status == "switched"
    versions = sorted(p.name for p in (state / "distribution" / "versions").iterdir())
    assert versions == sorted([version_id_from_sha(GIT_SHA_2), version_id_from_sha(GIT_SHA_3)])
    removed = [uri for name, uri in client.calls if name == "rm"]
    assert f"viking://resources/shared/{version_id_from_sha(GIT_SHA)}" in removed
    assert current_shared(state)["source_git_sha"] == GIT_SHA_3
    # Old version content is gone from the instance; the new one serves.
    assert f"viking://resources/shared/{version_id_from_sha(GIT_SHA)}" not in client.trees
    assert client.trees[f"viking://resources/shared/{version_id_from_sha(GIT_SHA_3)}"]


def test_private_layers_are_never_touched(tmp_path):
    state = tmp_path / "state"
    client = FakeClient()
    client.trees["viking://resources/project"] = {"viking://resources/project/p.md": "# P\n\nbody\n"}
    client.trees["viking://resources/candidate"] = {
        "viking://resources/candidate/c.md": "# C\n\nbody\n"
    }
    for sha, marker in ((GIT_SHA, "rel1"), (GIT_SHA_2, "rel2")):
        assert _sync(state, make_release_dir(tmp_path / marker, sha=sha), client).status == "switched"
    assert client.trees["viking://resources/project"]
    assert client.trees["viking://resources/candidate"]
    for name, uri in client.calls:
        if name == "rm":
            assert "/project" not in uri and "/candidate" not in uri


def test_modified_and_deleted_content_follow_the_new_version(tmp_path):
    state = tmp_path / "state"
    client = FakeClient()
    assert _sync(state, make_release_dir(tmp_path / "rel1"), client).status == "switched"
    old_root = current_shared(state)["root_uri"]
    assert DOC_A in client.trees[old_root].values()

    files = {"alpha.md": "# Alpha note\n\nUpdated body.\n"}  # beta.md deleted upstream
    release2 = make_release_dir(tmp_path / "rel2", sha=GIT_SHA_2, files=files)
    assert _sync(state, release2, client).status == "switched"
    new_root = current_shared(state)["root_uri"]
    assert new_root != old_root
    new_docs = client.trees[new_root]
    assert list(new_docs.values()) == ["# Alpha note\n\nUpdated body.\n"]
    # Searches target only the active root; nothing leaks back from the old one.
    hits = client.find("beta", target_uri=new_root)["resources"]
    assert all("beta.md" not in hit["uri"] for hit in hits)


def test_busy_lock_blocks_second_switcher(tmp_path):
    state = tmp_path / "state"
    release = make_release_dir(tmp_path / "rel")
    holder = SwitchLock(DistributionState(state).lock_path)
    holder.acquire()
    try:
        result = _sync(state, release, FakeClient())
        assert result.status == "busy"
        assert "in progress" in result.reason
        assert current_shared(state) is None
    finally:
        holder.release()
    # After the holder finishes, the next check proceeds normally.
    assert _sync(state, release, FakeClient()).status == "switched"


def test_aged_live_holder_keeps_lock(tmp_path):
    """Review repro: a long valid import must never lose the lock to age."""

    path = tmp_path / "sync.lock"
    first = SwitchLock(path)
    second = SwitchLock(path)
    first.acquire()
    old = time.time() - 7200
    os.utime(path, (old, old))  # two hours old, but the owner is alive and holds it
    with pytest.raises(SwitchInProgress):
        second.acquire()
    assert first.acquired and not second.acquired
    first.release()
    second.acquire()  # only after a real release
    second.release()


def test_partial_payload_window_stays_busy(tmp_path):
    """A half-written/unreadable payload while another process holds the lock
    is proof of activity, never of death."""

    path = tmp_path / "sync.lock"
    holder = SwitchLock(path)
    holder.acquire()
    try:
        try:
            fd = os.open(path, os.O_WRONLY)
            try:
                os.ftruncate(fd, 0)
                os.write(fd, b"{partial")  # simulate the create->write window
            finally:
                os.close(fd)
        except OSError:
            pass  # Windows enforces the byte lock against tampering — also fine
        with pytest.raises(SwitchInProgress):
            SwitchLock(path).acquire()
    finally:
        holder.release()


def test_old_owner_release_cannot_drop_replacement(tmp_path):
    """After A releases and B acquires, a stale A.release() must be a no-op."""

    path = tmp_path / "sync.lock"
    first, second, third = SwitchLock(path), SwitchLock(path), SwitchLock(path)
    first.acquire()
    first.release()
    second.acquire()
    first.release()  # stale extra release from the old owner
    with pytest.raises(SwitchInProgress):
        third.acquire()
    assert second.acquired and path.exists()
    second.release()
    third.acquire()
    third.release()


def test_real_subprocess_contention_and_crash_reclaim(tmp_path):
    """Real cross-process proof: contention while held, reclaim after a crash."""

    path = tmp_path / "sync.lock"
    repo_root = Path(__file__).resolve().parent.parent
    helper = (
        "import os, sys\n"
        "from mindie_knowledge.distribution.sync import SwitchLock\n"
        f"lock = SwitchLock({str(path)!r})\n"
        "lock.acquire()\n"
        "print('acquired', flush=True)\n"
        "sys.stdin.read(1)\n"
        "os._exit(99)\n"
    )
    env = dict(os.environ, PYTHONPATH=str(repo_root))
    proc = subprocess.Popen(
        [sys.executable, "-c", helper],
        stdout=subprocess.PIPE,
        stdin=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "acquired"
        with pytest.raises(SwitchInProgress):
            SwitchLock(path).acquire()
        # Crash the actual lock holder, including behind a Windows venv
        # redirector. Killing only the launcher does not prove holder exit.
        proc.communicate("x", timeout=10)
        assert proc.returncode == 99  # no release() or payload cleanup ran
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    # The OS released the crashed holder's lock: reclaim immediately.
    reclaimed = SwitchLock(path)
    reclaimed.acquire()
    reclaimed.release()


def test_concurrent_checks_have_one_switcher(tmp_path):
    state = tmp_path / "state"
    release = make_release_dir(tmp_path / "rel")

    class SlowClient(FakeClient):
        def import_ovpack(self, *args, **kwargs):
            time.sleep(0.3)  # widen the window so the loser meets the held lock
            return super().import_ovpack(*args, **kwargs)

    results = []

    def run():
        results.append(_sync(state, release, SlowClient()))

    threads = [threading.Thread(target=run), threading.Thread(target=run)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    statuses = [result.status for result in results]
    # Exactly one switcher imports; the other sees the lock or the finished state.
    assert statuses.count("switched") >= 1
    assert all(status in {"switched", "busy", "unchanged"} for status in statuses)
    imports = sum(
        1 for result in results if result.status == "switched" and not result.details.get("import_resumed")
    )
    assert imports == 1
    assert current_shared(state)["source_git_sha"] == GIT_SHA


def test_switch_lock_reentrant_safety(tmp_path):
    lock = SwitchLock(tmp_path / "sync.lock")
    lock.acquire()
    with pytest.raises(Exception, match="in progress"):
        SwitchLock(tmp_path / "sync.lock").acquire()
    lock.release()
    lock.release()  # double release is harmless
    SwitchLock(tmp_path / "sync.lock").acquire()


def test_corrupt_current_pointer_is_not_fatal(tmp_path):
    state = tmp_path / "state"
    dist = DistributionState(state)
    dist.root.mkdir(parents=True, exist_ok=True)
    dist.current_path.write_text("{corrupt", encoding="utf-8")
    assert current_shared(state) is None
    release = make_release_dir(tmp_path / "rel")
    result = _sync(state, release, FakeClient())
    assert result.status == "switched", result.reason
    assert current_shared(state)["source_git_sha"] == GIT_SHA


@pytest.mark.parametrize("damage", ["content", "missing_file", "vector", "missing_root"])
def test_active_damage_is_repaired_into_an_independent_root(tmp_path, damage):
    state = tmp_path / "state"
    release = make_release_dir(tmp_path / "release")
    client = FakeClient()
    assert _sync(state, release, client).status == "switched"
    old_root = current_shared(state)["root_uri"]
    if damage == "content":
        client.trees[old_root]["alpha.md"] = "# Damaged\n\nDifferent content.\n"
    elif damage == "missing_file":
        del client.trees[old_root]["alpha.md"]
    elif damage == "vector":
        client.corrupt_vectors.add(old_root)
    else:
        del client.trees[old_root]
    # Presence-only consistency is deliberately still true in this fake.
    assert client.check_consistency(old_root)["ok"]
    result = _sync(state, release, client, verify=True)
    assert result.status == "switched", result.reason
    assert result.details["repaired"] and result.details["verified"]
    new_root = current_shared(state)["root_uri"]
    assert new_root != old_root and "/repairs/" in new_root
    assert client.trees[new_root]["alpha.md"] == DOC_A
    assert ("rm", old_root) not in client.calls
    again = _sync(state, release, client, verify=True)
    assert again.status == "unchanged" and again.details["verified"]
    assert current_shared(state)["root_uri"] == new_root


def test_failed_active_repair_keeps_pointer_and_original_content(tmp_path):
    state = tmp_path / "state"
    release = make_release_dir(tmp_path / "release")
    client = FakeClient()
    assert _sync(state, release, client).status == "switched"
    old = current_shared(state)
    client.corrupt_vectors.add(old["root_uri"])
    client.fail_import = True
    result = _sync(state, release, client, verify=True)
    assert result.status == "error"
    assert current_shared(state) == old
    assert client.trees[old["root_uri"]]["alpha.md"] == DOC_A
    assert ("rm", old["root_uri"]) not in client.calls


@pytest.mark.parametrize("damage", ["manifest", "pack"])
def test_active_local_release_metadata_and_pack_are_restored(tmp_path, damage):
    state = tmp_path / "state"
    release = make_release_dir(tmp_path / "release")
    client = FakeClient()
    assert _sync(state, release, client).status == "switched"
    manifest_path = Path(current_shared(state)["manifest_path"])
    path = manifest_path if damage == "manifest" else next(manifest_path.parent.glob("*.ovpack"))
    path.write_bytes(b"damaged")
    result = _sync(state, release, client, verify=True)
    assert result.status == "unchanged" and result.details["verified"]
    assert result.details["local_release_repaired"]
    assert path.read_bytes() != b"damaged"


def test_offline_audit_repairs_from_retained_verified_pack(tmp_path):
    state = tmp_path / "state"
    client = FakeClient()
    assert _sync(state, make_release_dir(tmp_path / "release"), client).status == "switched"
    old_root = current_shared(state)["root_uri"]
    client.corrupt_vectors.add(old_root)
    result = _sync(state, tmp_path / "offline", client, verify=True)
    assert result.status == "switched", result.reason
    assert result.details["source_unavailable"]
    assert result.details["repaired"] and result.details["verified"]
    assert client.trees[current_shared(state)["root_uri"]]["alpha.md"] == DOC_A


@pytest.mark.parametrize("missing_root", [False, True])
def test_corrupt_pointer_recovers_activated_release_while_offline(tmp_path, missing_root):
    state, client = tmp_path / "state", FakeClient()
    assert _sync(state, make_release_dir(tmp_path / "release"), client).status == "switched"
    old = current_shared(state)
    DistributionState(state).current_path.write_text("{broken")
    if missing_root:
        del client.trees[old["root_uri"]]
    result = _sync(state, tmp_path / "offline", client, verify=True)
    assert result.ok, result.reason
    assert result.details["current_recovered"] and result.details["verified"]
    assert current_shared(state)["source_git_sha"] == GIT_SHA
    assert client.trees[current_shared(state)["root_uri"]]["alpha.md"] == DOC_A


def test_offline_pointer_recovery_skips_a_corrupted_newest_pack(tmp_path):
    state, client = tmp_path / "state", FakeClient()
    for sha in (GIT_SHA, GIT_SHA_2):
        assert _sync(state, make_release_dir(tmp_path / sha, sha=sha), client).status == "switched"
    latest = Path(current_shared(state)["manifest_path"]).parent
    next(latest.glob("*.ovpack")).write_bytes(b"broken")
    DistributionState(state).current_path.write_text("{broken")
    result = _sync(state, tmp_path / "offline", client, verify=True)
    assert result.ok and result.details["current_recovered"], result.reason
    assert current_shared(state)["source_git_sha"] == GIT_SHA


def test_legacy_activation_can_recover_from_source_download_cache(tmp_path):
    from mindie_knowledge.distribution.errors import SourceUnavailable
    from mindie_knowledge.distribution.release import LocalReleaseSource

    state, client = tmp_path / "state", FakeClient()
    release = make_release_dir(tmp_path / "release")
    assert _sync(state, release, client).status == "switched"
    activation_dir = Path(current_shared(state)["manifest_path"]).parent
    # Pre-maintenance releases kept activated.json + release.json but no pack.
    next(activation_dir.glob("*.ovpack")).unlink()
    record = json.loads((activation_dir / "activated.json").read_text())
    record.pop("root_uri")
    (activation_dir / "activated.json").write_text(json.dumps(record))
    DistributionState(state).current_path.write_text("{broken")

    class OfflineSource:
        def fetch(self):
            raise SourceUnavailable("offline")

        def cached(self, sha):
            assert sha == GIT_SHA
            return LocalReleaseSource(release).fetch()

    result = check_and_sync(state, OfflineSource(), embedding_info=embedding_info(), client=client, verify=True)
    assert result.ok and result.details["current_recovered"], result.reason
    assert list(activation_dir.glob("*.ovpack"))


def test_same_version_audit_checks_pinned_model_cache(tmp_path):
    import hashlib

    release = make_release_dir(tmp_path / "release")
    manifest_path = release / "release.json"
    data = json.loads(manifest_path.read_text())
    data["embedding"]["model_files"] = [{"path": "model.bin", "size": 4,
                                         "sha256": hashlib.sha256(b"good").hexdigest()}]
    manifest_path.write_text(json.dumps(data))
    cache = tmp_path / "cache"
    cache.mkdir()
    model = cache / "model.bin"
    model.write_bytes(b"good")
    state, client = tmp_path / "state", FakeClient()
    assert _sync(state, release, client, model_cache=cache).status == "switched"
    old = current_shared(state)
    model.write_bytes(b"bad!")
    result = _sync(state, release, client, model_cache=cache, verify=True)
    assert result.status == "incompatible" and "model.bin" in result.reason
    assert current_shared(state) == old


def test_verified_release_repairs_model_before_connecting(tmp_path):
    import hashlib

    release = make_release_dir(tmp_path / "release")
    manifest_path = release / "release.json"
    data = json.loads(manifest_path.read_text())
    data["embedding"]["model_files"] = [{"path": "model.bin", "size": 4,
                                         "sha256": hashlib.sha256(b"good").hexdigest()}]
    manifest_path.write_text(json.dumps(data))
    cache = tmp_path / "cache"
    cache.mkdir()
    model = cache / "model.bin"
    events = []
    client = FakeClient()

    def prepare(manifest):
        events.append("prepare")
        assert manifest.source_git_sha == GIT_SHA
        model.write_bytes(b"good")

    def connect():
        events.append("connect")
        assert model.read_bytes() == b"good"
        return client

    state = tmp_path / "state"
    kwargs = {"embedding_info": embedding_info(), "model_cache": cache,
              "prepare_model": prepare, "client_factory": connect}
    assert check_and_sync(state, str(release), **kwargs).status == "switched"
    assert events == ["prepare", "connect"]
    events.clear()
    assert check_and_sync(state, str(release), **kwargs).status == "unchanged"
    assert events == []
    model.write_bytes(b"bad!")
    assert check_and_sync(state, str(release), verify=True, **kwargs).status == "unchanged"
    assert events == ["prepare", "connect"]
    assert model.read_bytes() == b"good"


def test_corrupt_pack_cannot_trigger_model_preparation(tmp_path):
    release = make_release_dir(tmp_path / "release")
    next(release.glob("*.ovpack")).write_bytes(b"corrupt")

    def unexpected(*_args):
        raise AssertionError("corrupt release must not prepare a model or connect")

    result = check_and_sync(tmp_path / "state", str(release), embedding_info=embedding_info(),
                            prepare_model=unexpected, client_factory=unexpected)
    assert result.status == "corrupt", result.reason


def test_failed_model_repair_preserves_active_release_and_never_connects(tmp_path):
    state, client = tmp_path / "state", FakeClient()
    assert _sync(state, make_release_dir(tmp_path / "first"), client).status == "switched"
    previous = current_shared(state)
    release = make_release_dir(tmp_path / "second", sha=GIT_SHA_2)

    def failed(_manifest):
        raise OSError("model source unavailable")

    def unexpected():
        raise AssertionError("failed repair must not connect")

    result = check_and_sync(state, str(release), embedding_info=embedding_info(),
                            prepare_model=failed, client_factory=unexpected)
    assert result.status == "error" and "model source unavailable" in result.reason
    assert current_shared(state) == previous


def test_slow_discovery_cannot_replace_a_concurrently_activated_release(tmp_path):
    from mindie_knowledge.distribution.release import LocalReleaseSource

    state, client = tmp_path / "state", FakeClient()
    older = make_release_dir(tmp_path / "older")
    newer = make_release_dir(tmp_path / "newer", sha=GIT_SHA_2)

    class SlowSource:
        def fetch(self):
            snapshot = LocalReleaseSource(older).fetch()
            assert _sync(state, newer, client).status == "switched"
            return snapshot

    result = check_and_sync(state, SlowSource(), embedding_info=embedding_info(), client=client)
    assert result.status == "busy" and "changed during discovery" in result.reason
    assert current_shared(state)["source_git_sha"] == GIT_SHA_2
    assert len([call for call in client.calls if call[0] == "import_ovpack"]) == 1
