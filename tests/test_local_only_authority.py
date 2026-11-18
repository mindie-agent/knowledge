"""Capture vs publication authority: ``local-only`` never opens outbound work."""

from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path

import pytest

from conftest import write_settings
from mindie_knowledge import consent_store
from mindie_knowledge.loop import settings as settings_mod
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.export import build_batch
from mindie_knowledge.loop.store import Store


def _consent_path(settings):
    return settings.consent["path"]


def _tick(engine, monkeypatch):
    engine.stop.clear()
    monkeypatch.setattr(engine.stop, "wait", lambda _: engine.stop.set())
    try:
        engine._outbox_loop()
    finally:
        engine.stop.clear()


def test_local_only_allows_capture_but_never_publication(tmp_path):
    settings = write_settings(
        tmp_path / "community.json", consent_choice="local-only",
        repository=None, roots=[tmp_path],
    )
    assert settings.allows_capture() is True
    assert settings.allows_publication() is False
    assert settings.effective_tier() == "local-only"
    assert settings.publication_block_kind() == "revoked"
    assert settings.capture_block_kind() is None
    status = settings.public_status()
    assert status["effective_tier"] == "local-only"
    assert status["allows_capture"] is True
    assert status["allows_publication"] is False
    assert status["consent"]["choice"] == "local-only"


def test_contribute_allows_both_without_requiring_fork_or_account(tmp_path):
    settings = write_settings(
        tmp_path / "community.json", consent_choice="contribute", roots=[tmp_path]
    )
    assert settings.allows_capture() is True
    assert settings.allows_publication() is True
    assert settings.publication_block_kind() is None
    assert settings.effective_tier() == "contribute"


def test_local_only_flag_blocks_publication_even_with_stale_target(tmp_path):
    settings = write_settings(
        tmp_path / "community.json", consent_choice="contribute", roots=[tmp_path],
        local_only=True, fork="me/knowledge-vllm-ascend", account="exampleuser",
    )
    assert settings.allows_capture() is True
    assert settings.allows_publication() is False
    assert settings.publication_block_kind() == "revoked"
    assert settings.effective_tier() == "local-only"


@pytest.mark.parametrize("choice", ["read-only", "later", "disabled"])
def test_non_capture_choices_revoke_both_gates(tmp_path, choice):
    settings = write_settings(
        tmp_path / "community.json", consent_choice=choice, roots=[tmp_path]
    )
    assert settings.allows_capture() is False
    assert settings.allows_publication() is False
    assert settings.capture_block_kind() == "revoked"
    assert settings.publication_block_kind() == "revoked"
    assert settings.effective_tier() == "read-only"


def test_publication_configuration_faults_are_not_revocations(tmp_path):
    malformed_fork = write_settings(
        tmp_path / "fork.json", consent_choice="contribute", roots=[tmp_path],
        fork="not-a-repository",
    )
    assert malformed_fork.allows_capture() is True
    assert malformed_fork.allows_publication() is False
    assert malformed_fork.publication_block_kind() == "fault"

    missing_repository = write_settings(
        tmp_path / "repo.json", consent_choice="contribute", repository=None,
        roots=[tmp_path],
    )
    assert missing_repository.allows_capture() is True
    assert missing_repository.allows_publication() is False
    assert missing_repository.publication_block_kind() == "fault"

    corrupt = tmp_path / "corrupt.json"
    write_settings(corrupt, consent_choice="contribute", roots=[tmp_path])
    corrupt.write_text("{broken", encoding="utf-8")
    loaded = settings_mod.load(corrupt)
    assert loaded.publication_block_kind() == "fault"
    assert loaded.capture_block_kind() == "fault"


def test_tier_transitions_switch_gates_without_rewriting_settings(tmp_path):
    path = tmp_path / "community.json"
    settings = write_settings(path, consent_choice="local-only", roots=[tmp_path])
    revision = settings.generation
    consent = _consent_path(settings)

    consent_store.record_choice(consent, "contribute")
    contribute = settings_mod.load(path)
    assert contribute.generation == revision
    assert contribute.effective_tier() == "contribute"
    assert contribute.allows_publication() is True

    consent_store.record_choice(consent, "read-only")
    read_only = settings_mod.load(path)
    assert read_only.effective_tier() == "read-only"
    assert read_only.allows_capture() is False
    assert read_only.allows_publication() is False

    consent_store.record_choice(consent, "local-only")
    local_only = settings_mod.load(path)
    assert local_only.effective_tier() == "local-only"
    assert local_only.allows_capture() is True
    assert local_only.allows_publication() is False
    assert local_only.generation == revision


def test_local_only_cancels_unsent_batches_without_invoking_gh(tmp_path, monkeypatch):
    path = tmp_path / "community.json"
    settings = write_settings(path, consent_choice="contribute", roots=[tmp_path])
    marker = tmp_path / "gh-was-called"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n: > " + shlex_quote(str(marker)) + "\nexit 99\n",
        encoding="utf-8", newline="\n",
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))

    with closing(Store(tmp_path / "store", "test")) as store:
        doc = store.create_draft(
            kind="experience", title="Synthetic public observation",
            summary="Unverified test material.", content="Pending contribution body.",
            generation=settings.generation, owner="a" * 64,
        )
        batch_id, *_ = build_batch(store, settings=settings)
        consent_store.record_choice(
            Path(settings.consent["path"]), "local-only"
        )
        engine = Engine(store, settings_path=path)
        engine.community = {
            "submit_batch": lambda *a, **k: pytest.fail("local-only must not submit"),
            "reconcile_batch": lambda *a, **k: pytest.fail("local-only must not reconcile"),
        }
        _tick(engine, monkeypatch)
        assert not marker.exists()
        assert store.batch(batch_id)["status"] == "disabled"
        assert "publication disabled" in store.batch(batch_id)["detail"]
        local = engine._settings()
        assert local.effective_tier() == "local-only"
        assert store.get(store.ref(doc["entry_id"]))["content"] == "Pending contribution body."


def test_outbox_loop_creates_no_rows_under_local_only(tmp_path, monkeypatch):
    path = tmp_path / "community.json"
    settings = write_settings(path, consent_choice="local-only", roots=[tmp_path])
    with closing(Store(tmp_path / "store", "test")) as store:
        engine = Engine(store, settings_path=path)
        engine.community = {
            "submit_batch": lambda *a, **k: pytest.fail("local-only must not submit"),
            "reconcile_batch": lambda *a, **k: pytest.fail("local-only must not reconcile"),
        }
        _tick(engine, monkeypatch)
        assert store.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
        assert store.db.execute("SELECT count(*) FROM grants").fetchone()[0] == 0
        assert engine._settings().generation == settings.generation


def test_local_only_to_read_only_cancels_capture_and_publication(tmp_path, monkeypatch):
    path = tmp_path / "community.json"
    settings = write_settings(path, consent_choice="local-only", roots=[tmp_path])
    consent = Path(settings.consent["path"])
    with closing(Store(tmp_path / "store", "test")) as store:
        engine = Engine(store, settings_path=path)
        engine.community = {
            "submit_batch": lambda *a, **k: pytest.fail("local-only must not submit"),
            "reconcile_batch": lambda *a, **k: pytest.fail("local-only must not reconcile"),
        }
        # A real local-only tick records ("local-only", generation), not the
        # ("revoked", None) tuple used for read-only/disabled.
        _tick(engine, monkeypatch)
        assert engine._observed_authority[0] == "local-only"

        capture = engine.operations.begin("capture", "c", settings.generation)
        publish = engine.operations.begin("publish", "p", settings.generation)
        try:
            consent_store.record_choice(consent, "read-only")
            assert engine._settings().effective_tier() == "read-only"
            _tick(engine, monkeypatch)
            assert capture.cancel.is_set(), (
                "local-only -> read-only must cancel capture work"
            )
            assert publish.cancel.is_set(), (
                "local-only -> read-only must cancel publication work"
            )
        finally:
            engine.operations.end(capture)
            engine.operations.end(publish)


def test_unknown_external_write_is_preserved_when_publication_is_local_only(tmp_path):
    from mindie_knowledge.community.ledger import Ledger
    from mindie_knowledge.community.reconcile import inspect_batch, reconcile_batch

    state = tmp_path / "state"
    state.mkdir()
    batch_id, revision, head = "batch-unknown", "r" * 64, "a" * 40
    ledger = Ledger(state)
    try:
        ledger.record_intent(
            batch_id=batch_id, revision=revision, domain="test",
            repository="mindie-agent/knowledge-vllm-ascend", branch="main",
        )
        ledger.record_step(batch_id, revision, "git:pushed", head)
        ledger.finish_publication(
            batch_id, revision, status="unknown",
            detail="external write outcome unknown", head_sha=head,
        )
    finally:
        ledger.close()

    settings = write_settings(
        tmp_path / "community.json", consent_choice="local-only", roots=[tmp_path],
    )
    for reconcile in (inspect_batch, reconcile_batch):
        result = reconcile(batch_id, settings.as_dict(), state)
        assert result["status"] == "unknown", reconcile.__name__
        assert result["revision"] == revision
        assert "remains unknown" in result["detail"]

    reopened = Ledger(state)
    try:
        row = reopened.latest_for_batch(batch_id)
        assert row["status"] == "unknown", "a blocked read must not erase uncertainty"
        assert row["detail"] == "external write outcome unknown"
        assert row["head_sha"] == head
    finally:
        reopened.close()


def shlex_quote(value: str) -> str:
    """A POSIX single-quote helper that keeps the tripwire newline-free."""
    return "'" + value.replace("'", "'\"'\"'") + "'"
