"""``local-only`` provenance is additive and permanently blocks export backfill."""

from __future__ import annotations

import json
import os
from contextlib import closing
from pathlib import Path

import pytest

import graded_fixture
from conftest import make_admission, write_settings
from mindie_knowledge import consent_store
from mindie_knowledge.graded.export import build_batch as build_graded_batch
from mindie_knowledge.graded.feedback import record_vote
from mindie_knowledge.graded.repository import GradedRepository
from mindie_knowledge.loop import settings as settings_mod
from mindie_knowledge.loop import transcript_capture
from mindie_knowledge.loop.activation import Admission, activation_epoch
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.export import build_batch
from mindie_knowledge.loop.feed import feed_ident
from mindie_knowledge.loop.store import Store, session_key
from mindie_knowledge.loop.transcript_redaction import install_scanner


_SCANNER_HOME = Path.home()


def _scanner():
    matches = list((_SCANNER_HOME / ".cache/mindie-agent/gitleaks/8.30.1").glob("*/gitleaks"))
    if not matches:
        pytest.skip("installed pinned scanner not present; no download in component tests")
    return str(matches[0])


def _engine(tmp_path, consent_choice):
    project = tmp_path / "project"
    project.mkdir()
    path = tmp_path / "community.json"
    settings = write_settings(path, consent_choice=consent_choice, roots=[project])
    admission_path = make_admission(tmp_path, project_root=project, session="local-only-A")
    admission = Admission(admission_path)
    store = Store(tmp_path / "store", "test")
    engine = Engine(store, settings_path=path, admission=admission,
                    redactor_executable=_scanner())
    return engine, store, settings, project, admission


def _authorization(admission, settings, project, session="local-only-A", *,
                   origin_tier=None):
    lease = admission.active_lease(session)
    return dict(
        session=session, generation=settings.generation,
        scope=str(project.resolve()), activation_epoch=activation_epoch(lease["token"]),
        origin_tier=(settings.effective_tier() if origin_tier is None else origin_tier),
        consent_revision=(settings.consent or {}).get("choice_revision"),
    )


def test_local_only_capture_is_stamped_and_never_publishable(tmp_path):
    engine, store, settings, project, admission = _engine(tmp_path, "local-only")
    try:
        captured = engine.capture(
            session_id="local-only-A", turn_id="turn-1",
            transcript_path=str(tmp_path / "transcript.jsonl"), harness="codex",
        )
        assert captured["status"] in {"queued", "pending", "deferred"}
        assert store.origin_tier_of(captured["id"], scope="capture") == "local-only"
        assert store.publication_origin_eligible(captured["id"], scope="capture") is False
        consent_store.record_choice(Path(settings.consent["path"]), "contribute")
        upgraded = settings_mod.load(settings.path)
        assert upgraded.allows_publication() is True
        # Provenance is permanent: the capture is still ineligible after upgrade.
        assert store.publication_origin_eligible(captured["id"], scope="capture") is False
    finally:
        store.close()


def test_knowledge_distill_succeeds_locally_without_gh_and_never_backfills(tmp_path, monkeypatch):
    marker = tmp_path / "gh-was-called"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n: > '" + str(marker).replace("'", "'\"'\"'") + "'\nexit 99\n",
        encoding="utf-8", newline="\n",
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))

    engine, store, settings, project, admission = _engine(tmp_path, "local-only")
    try:
        session = "local-only-A"
        lease = admission.active_lease(session)
        authorization = dict(
            session=session, generation=settings.generation,
            scope=str(project.resolve()), activation_epoch=activation_epoch(lease["token"]),
            origin_tier=settings.effective_tier(),
            consent_revision=(settings.consent or {}).get("choice_revision"),
        )
        request = graded_fixture.request()
        decision = graded_fixture.decision(request)
        decision["session_digest"] = session_key(session)
        result = engine.distill_selected(
            request["sanitized_inputs"], session_digest=session_key(session),
            task_state="verified_milestone", authorization=authorization,
            proposal=decision,
        )
        assert result["status"] == "applied"
        assert result["admitted_unit_ids"] == ["unit-fixture-probe"]
        assert result["publication"] == "local_only"
        assert not marker.exists(), "local-only distillation must never invoke gh"
        assert store.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
        descriptors = store.graded._catalog()["local"]
        assert [item.get("origin_tier") for item in descriptors] == ["local-only"]
        assert store.graded.publication_eligible("unit-fixture-probe") is False
        assert build_graded_batch(store, settings=settings) is None
        # The locally distilled unit stays queryable.
        assert store.explain(
            "mindie://test/units/unit-fixture-probe@1"
        )["frontmatter"]["id"] == "unit-fixture-probe"

        consent_store.record_choice(Path(settings.consent["path"]), "contribute")
        upgraded = settings_mod.load(settings.path)
        assert upgraded.allows_publication() is True
        assert build_graded_batch(store, settings=upgraded) is None
        assert store.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
        assert not marker.exists()
    finally:
        store.close()


def test_contribute_material_is_exported_and_local_only_sibling_is_not(tmp_path):
    path = tmp_path / "community.json"
    settings = write_settings(path, consent_choice="local-only", roots=[tmp_path])
    with closing(Store(tmp_path / "store", "test")) as store:
        local = store.create_draft(
            kind="experience", title="Local-only observation",
            summary="Captured while local only.", content="Local-only body.",
            generation=settings.generation, owner="a" * 64,
        )
        store.stamp_origin_tier(
            local["entry_id"], origin_tier="local-only",
            generation=settings.generation,
            consent_revision=(settings.consent or {}).get("choice_revision"),
        )
        assert store.publication_origin_eligible(local["entry_id"]) is False
        assert build_batch(store, settings=settings) is None

        consent_store.record_choice(Path(settings.consent["path"]), "contribute")
        upgraded = settings_mod.load(path)
        public = store.create_draft(
            kind="experience", title="Contributed observation",
            summary="Captured after upgrade.", content="Contributed body.",
            generation=upgraded.generation, owner="b" * 64,
        )
        built = build_batch(store, settings=upgraded)
        assert built is not None
        _batch_id, _revision, batch, entry_ids, _votes = built
        assert entry_ids == [public["entry_id"]]
        assert all(local["entry_id"] not in ref for ref in batch["entry_refs"])
        assert any("Contributed body." in (item.get("content") or "")
                   for item in batch["files"])
        assert not any("Local-only body." in (item.get("content") or "")
                       for item in batch["files"])


def test_queued_local_only_capture_stays_local_only_after_upgrade(tmp_path, monkeypatch):
    engine, store, settings, project, admission = _engine(tmp_path, "local-only")
    try:
        captured = engine.capture(
            session_id="local-only-A", turn_id="turn-queued",
            transcript_path=str(tmp_path / "transcript.jsonl"), harness="codex",
        )
        assert captured["status"] in {"queued", "pending", "deferred"}
        assert store.origin_tier_of(captured["id"], scope="capture") == "local-only"

        consent_store.record_choice(Path(settings.consent["path"]), "contribute")
        upgraded = settings_mod.load(settings.path)
        assert upgraded.effective_tier() == "contribute"
        assert transcript_capture.capture_origin_tier(
            store, captured["id"], upgraded
        ) == "local-only"

        committed = {}

        def fake_increment(row, live, lease, region):
            region["inc"] = {"start": 0, "end": 5, "digest": "0" * 64,
                             "coverage": False, "discarded_records": []}
            region["reserve"] = lambda *args, **kwargs: "region-1"
            return "hello"

        def fake_prepare(**kwargs):
            return {"blocks": [], "redaction_rules": [], "redaction_state": {},
                    "batch_id": "batch-1"}

        def fake_commit(**kwargs):
            committed.update(kwargs["authorization"])
            return {"revision": "0" * 64}

        monkeypatch.setattr(engine, "_transcript_increment", fake_increment)
        monkeypatch.setattr(transcript_capture, "prepare_increment", fake_prepare)
        monkeypatch.setattr(store, "commit_material_increment", fake_commit)
        monkeypatch.setattr(store, "finish_region", lambda *args, **kwargs: None)
        monkeypatch.setattr(store, "record_capture_material", lambda *args, **kwargs: None)

        engine._process(captured["id"])
        assert committed["origin_tier"] == "local-only", (
            "processing a queued local-only capture after an upgrade must not promote it"
        )
    finally:
        store.close()


def test_real_queued_local_only_capture_stays_local_only_after_upgrade(tmp_path):
    from datetime import datetime, timezone
    from lane_support import load_parser

    project = tmp_path / "project"
    project.mkdir()
    path = tmp_path / "community.json"
    settings = write_settings(path, consent_choice="local-only", roots=[project])
    admission = Admission(make_admission(
        tmp_path, project_root=project, session="local-only-A",
    ))
    store = Store(tmp_path / "store", "test")
    engine = Engine(
        store, settings_path=path, admission=admission,
        transcript_adapter=load_parser("codex"), capture_mode="public-transcript",
        redactor_executable=_scanner(),
    )
    try:
        transcript = tmp_path / "native.jsonl"
        transcript.write_text(
            json.dumps({"type": "session_meta",
                        "payload": {"id": "local-only-A"}}) + "\n",
            encoding="utf-8", newline="\n",
        )
        record = {
            "type": "response_item",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text",
                                     "text": "local-only queued capture marker"}]},
        }
        with transcript.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record) + "\n")

        captured = engine.capture(
            session_id="local-only-A", turn_id="turn-real",
            transcript_path=str(transcript), harness="codex",
        )
        assert captured["status"] in {"queued", "pending", "deferred"}
        assert store.origin_tier_of(captured["id"], scope="capture") == "local-only"

        consent_store.record_choice(Path(settings.consent["path"]), "contribute")
        assert engine._settings().effective_tier() == "contribute"
        engine._process(captured["id"])

        docs = store.drafts_changed()
        assert len(docs) == 1
        assert "local-only queued capture marker" in docs[0]["content"]
        assert store.origin_tier_of(docs[0]["entry_id"]) == "local-only", (
            "real capture processing must honor the queued local-only marker"
        )
    finally:
        store.close()


def test_distillation_started_as_contribute_is_local_only_after_downgrade(tmp_path):
    engine, store, settings, project, admission = _engine(tmp_path, "contribute")
    try:
        session = "local-only-A"
        authorization = _authorization(
            admission, settings, project, session, origin_tier="contribute",
        )
        # The paid/admitted job started under contribute; by completion the
        # live authority is local-only. The result must never become public.
        consent_store.record_choice(Path(settings.consent["path"]), "local-only")
        request = graded_fixture.request()
        decision = graded_fixture.decision(request)
        decision["session_digest"] = session_key(session)
        result = engine.distill_selected(
            request["sanitized_inputs"], session_digest=session_key(session),
            task_state="verified_milestone", authorization=authorization,
            proposal=decision,
        )
        assert result["status"] == "applied"
        assert store.graded.local_origin_tier("unit-fixture-probe") == "local-only"
        assert store.graded.publication_eligible("unit-fixture-probe") is False
    finally:
        store.close()


def test_local_only_related_unit_forces_local_only_result(tmp_path):
    engine, store, settings, project, admission = _engine(tmp_path, "contribute")
    try:
        related = graded_fixture.unit("unit-fixture-related")
        store.graded.install(
            [related], admitted_ids=[related.id], origin_tier="local-only",
        )
        session = "local-only-A"
        authorization = _authorization(
            admission, settings, project, session, origin_tier="contribute",
        )
        request = graded_fixture.request()
        decision = graded_fixture.decision(request)
        decision["session_digest"] = session_key(session)
        result = engine.distill_selected(
            request["sanitized_inputs"], session_digest=session_key(session),
            task_state="verified_milestone", authorization=authorization,
            related_refs=["unit-fixture-related"], proposal=decision,
        )
        assert result["status"] == "applied"
        assert store.graded.local_origin_tier("unit-fixture-probe") == "local-only", (
            "a local-only related unit taints the distilled result"
        )
    finally:
        store.close()


def test_local_only_input_material_forces_local_only_result(tmp_path):
    engine, store, settings, project, admission = _engine(tmp_path, "contribute")
    try:
        session = "local-only-A"
        authorization = _authorization(
            admission, settings, project, session, origin_tier="contribute",
        )
        inputs = graded_fixture.request()["sanitized_inputs"]
        inputs[0]["origin_tier"] = "local-only"
        request = graded_fixture.request()
        decision = graded_fixture.decision(request)
        decision["session_digest"] = session_key(session)
        result = engine.distill_selected(
            inputs, session_digest=session_key(session),
            task_state="verified_milestone", authorization=authorization,
            proposal=decision,
        )
        assert result["status"] == "applied"
        assert store.graded.local_origin_tier("unit-fixture-probe") == "local-only", (
            "local-only input material taints the distilled result"
        )
    finally:
        store.close()


def test_export_loads_catalog_once_for_all_vote_rows(tmp_path, monkeypatch):
    path = tmp_path / "community.json"
    settings = write_settings(path, consent_choice="contribute", roots=[tmp_path])
    with closing(Store(tmp_path / "store", "test")) as store:
        unit = graded_fixture.unit()
        store.graded.install(
            [unit], generation=settings.generation, admitted_ids=[unit.id],
            origin_tier="local-only",
        )
        observed = store.explain(f"mindie://test/units/{unit.id}@1")
        for index in range(3):
            record_vote(
                store, root_hash=str(index) * 64, ref=observed["feedback_ref"],
                rating="up", reason="", publishable=True,
                generation=settings.generation,
            )
        identity = feed_ident(settings.repository, settings.branch)
        catalog = store.graded._catalog()
        catalog["feeds"][identity] = {
            "units": [dict(catalog["local"][0])], "files": {}, "source_revision": None,
        }
        store.graded._save(catalog)

        calls = []
        original = GradedRepository._catalog

        def counting(self):
            calls.append(1)
            return original(self)

        monkeypatch.setattr(GradedRepository, "_catalog", counting)
        assert build_graded_batch(store, settings=settings) is None
        assert len(calls) <= 4, (
            "vote eligibility must use one precomputed local-only set, not "
            f"reparse the catalog per vote row ({len(calls)} loads)"
        )
