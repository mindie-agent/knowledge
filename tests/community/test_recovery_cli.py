"""Recovery uses the same frozen publication and receipts as the worker."""
from contextlib import closing
import json
import subprocess

import pytest

from mindie_knowledge import community
from mindie_knowledge.loop import cli, settings as settings_mod
from mindie_knowledge.loop.export import build_batch
from mindie_knowledge.loop.store import Store
from mindie_knowledge.materials.publication import load_batch_payload, staging_path

from .conftest import git


@pytest.fixture
def failed_batch(tmp_path, settings):
    config = dict(root=str(tmp_path / "producer"), domain="npu",
                  community_config=settings["config_path"])
    with closing(Store(config["root"], config["domain"])) as store:
        doc = store.create_draft(
            kind="experience", title="Recovery fixture", summary="Unverified trial.",
            content="The original failed trial and its correction remain available.",
            owner="b" * 64, generation=settings["generation"])
        batch_id, *_ = build_batch(store, settings=settings_mod.from_engine_config(config))
        store.mark_batch(batch_id, "failed", attempted=True, detail="fixture failed before write")
        row = store.batch(batch_id)
        batch = load_batch_payload(store, row)
        frozen = {item["path"]: item["content"] for item in batch["files"]}
        staged = staging_path(store.root, batch_id, row["revision"])
    return config, batch_id, doc, frozen, staged


def test_retry_frozen_batch_publishes_exact_bytes_once(failed_batch, remote_url):
    config, batch_id, doc, frozen, staged = failed_batch
    receipt = cli.contribution_recovery(config, "contribution-retry", batch_id)
    assert receipt["status"] == "submitted"
    assert git(["ls-remote", remote_url, f"refs/heads/mindie-contrib/npu/{batch_id}"]).split()[0] == receipt["head_sha"]
    for path, content in frozen.items():
        actual = subprocess.run(
            ["git", "show", receipt["head_sha"] + ":" + path],
            cwd=remote_url, capture_output=True, check=True).stdout
        assert actual == content.encode("utf-8")
    with closing(Store(config["root"], config["domain"])) as store:
        row = store.batch(batch_id)
        assert row["status"] == "submitted" and row["attempted"] is not None
        sent = store.sent_receipt(doc["entry_id"])
        assert sent["head_sha"] == receipt["head_sha"]
        assert {f["path"]: f["sha256"] for f in store.sent_package_files(doc["entry_id"])} == {
            f["path"]: f["sha256"] for f in receipt["files"]}
        assert store.get(store.ref(doc["entry_id"]))["content"] == doc["content"]
    assert not staged.exists()
    before = cli.contribution_recovery(config, "contribution-inspect", batch_id)
    with pytest.raises(ValueError, match="proven failed"):
        cli.contribution_recovery(config, "contribution-retry", batch_id)
    assert cli.contribution_recovery(config, "contribution-inspect", batch_id) == before


@pytest.mark.parametrize("damage", ["unknown", "missing", "tampered"])
def test_retry_never_writes_unknown_or_invalid_frozen_work(failed_batch, monkeypatch, damage):
    config, batch_id, _, frozen, staged = failed_batch
    if damage == "unknown":
        with closing(Store(config["root"], config["domain"])) as store:
            store.mark_batch(batch_id, "unknown", detail="original push result uncertain")
    else:
        file = staged / "files" / next(iter(frozen))
        if damage == "missing":
            file.unlink()
        else:
            file.write_bytes(b"tampered")
    before = cli.contribution_recovery(config, "contribution-inspect", batch_id)
    monkeypatch.setattr(community, "submit_batch",
                        lambda *a, **kw: pytest.fail("invalid work reached publisher"))
    with pytest.raises(ValueError):
        cli.contribution_recovery(config, "contribution-retry", batch_id)
    assert cli.contribution_recovery(config, "contribution-inspect", batch_id) == before


@pytest.mark.parametrize("failure", ["record", "cleanup", "close", "cleanup-receipt"])
def test_completed_write_remains_visible_after_local_failure(
        failed_batch, monkeypatch, capsys, remote_url, failure):
    config, batch_id, _, _, _ = failed_batch
    original_close = Store.close

    def broken(*args, **kwargs):
        raise OSError("synthetic local failure")

    def broken_close(store):
        original_close(store)
        raise OSError("synthetic close failure")

    monkeypatch.setattr(cli, "config_at", lambda path: config)
    with monkeypatch.context() as patch:
        if failure == "record":
            patch.setattr(Store, "mark_batch", broken)
        elif failure == "cleanup":
            patch.setattr(Store, "compact_confirmed", broken)
        elif failure == "close":
            patch.setattr(Store, "close", broken_close)
        else:
            patch.setattr(Store, "compact_confirmed",
                          lambda *a: dict(cleanup_status="failed", cleanup_error="synthetic"))
        code = cli.main(["contribution-retry", "--config", "unused.json", "--batch", batch_id])
    assert code == 2
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "submitted" and receipt["pr_url"]
    if failure == "cleanup-receipt":
        assert receipt["cleanup_failed"]
    else:
        assert receipt["recovery_error"]["category"] == "OSError"
        assert receipt["automatic_retry"] is False
    assert git(["ls-remote", remote_url, f"refs/heads/mindie-contrib/npu/{batch_id}"]).split()[0] == receipt["head_sha"]
    inspected = cli.contribution_recovery(config, "contribution-inspect", batch_id)
    assert inspected["ledger"][0]["status"] == "submitted"
    assert inspected["outbox"]["status"] == ("failed" if failure == "record" else "submitted")
    if failure == "record":
        # Read-only reconciliation repairs the local receipt without replaying.
        monkeypatch.setattr(community, "submit_batch",
                            lambda *a, **kw: pytest.fail("reconciliation replayed a write"))
        recovered = cli.contribution_recovery(config, "contribution-reconcile", batch_id)
        assert recovered["head_sha"] == receipt["head_sha"]


def test_recovery_keeps_primary_failure_when_close_also_fails(failed_batch, monkeypatch):
    config, batch_id, _, _, _ = failed_batch
    primary = OSError("synthetic pre-publication failure")
    original = Store.close

    def submit(*args, **kwargs):
        raise primary

    def close(store):
        original(store)
        raise RuntimeError("synthetic close failure")

    monkeypatch.setattr(community, "submit_batch", submit)
    monkeypatch.setattr(Store, "close", close)
    with pytest.raises(OSError) as caught:
        cli.contribution_recovery(config, "contribution-retry", batch_id)
    assert caught.value is primary
    assert "RuntimeError" in " ".join(primary.__notes__)


def test_retry_preserves_publisher_retry_floor(failed_batch, monkeypatch, capsys):
    config, batch_id, _, _, _ = failed_batch

    def rejected(batch, settings, state_dir, *, authority_guard):
        authority_guard()
        assert batch["explicit_retry"] is True
        assert all(isinstance(item["content"], str) for item in batch["files"])
        return dict(status="failed", detail="synthetic rate limit", retry_at=9_999_999_999)

    monkeypatch.setattr(community, "submit_batch", rejected)
    receipt = cli.contribution_recovery(config, "contribution-retry", batch_id)
    with closing(Store(config["root"], config["domain"])) as store:
        assert store.batch(batch_id)["next_attempt"] == receipt["retry_at"]
    monkeypatch.setattr(cli, "config_at", lambda path: config)
    assert cli.main(["contribution-retry", "--config", "unused.json", "--batch", batch_id]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
