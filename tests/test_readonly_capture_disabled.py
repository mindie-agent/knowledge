"""Orthogonal contract tests for read-only / capture-disabled config, cold service
startup without Gitleaks or transcript_adapter, Stop handoff boundaries, and strict
redactor enforcement on material ingestion and distillation."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest

from conftest import admission_token, make_admission, write_settings
from graded_fixture import OBSERVED, unit
from mindie_knowledge.loop import cli, dfx, process, service_host
from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store, session_key
from mindie_knowledge.loop.transcript_redaction import ScannerUnavailable, install_scanner
from mindie_knowledge.loop.transport import RequestRejected, Service, rpc
from mindie_knowledge.publication_contract import GRADED_SCHEMA, make_contract, render_contract
import transcript_double


DOMAIN = "vllm-ascend"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _publish_graded_feed(tmp_path: Path, *units) -> Path:
    repo = tmp_path / "public-feed-repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "feed")
    _git(repo, "config", "user.email", "feed@example.invalid")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "core.eol", "lf")
    (repo / "publication-contract.json").write_text(
        render_contract(make_contract(DOMAIN, "a" * 40, schema=GRADED_SCHEMA)),
        encoding="utf-8",
        newline="\n",
    )
    for item in units:
        target = repo / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item.render(), encoding="utf-8", newline="\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "publish graded corpus")
    return repo


def _distill_inputs() -> list[dict[str, object]]:
    return [
        {
            "kind": "experiment_ledger",
            "outcome": "verified",
            "source_locator": "mindie-agent/knowledge@" + "1" * 40 + ":ledger.json",
            "content": "ACLGraph static capture avoided repeated host launch overhead across 8 runs.",
            "evidence_refs": ["mindie-agent/knowledge@" + "1" * 40 + ":ledger.json#run-1"],
            "repetitions": 8,
        }
    ]


def test_validate_config_readonly_and_capture_disabled_matrix(tmp_path):
    base = {"root": str(tmp_path / "state"), "domain": DOMAIN}
    # Read-only cold bootstrap: capture_mode omitted or disabled, no gitleaks or adapter on disk.
    assert cli.validate_config(dict(base)) == base
    assert cli.validate_config({**base, "redactor_executable": None, "transcript_adapter": None})
    disabled_cfg = {**base, "capture_mode": "disabled", "redactor_executable": None, "transcript_adapter": None}
    assert cli.validate_config(disabled_cfg) == disabled_cfg

    # Consent disabled / read-only settings also allow omitted redactor & adapter.
    readonly_settings = tmp_path / "readonly-community.json"
    write_settings(readonly_settings, consent_choice="read-only", enabled=False, roots=[tmp_path])
    assert cli.validate_config({**base, "community_config": str(readonly_settings)})

    # Consent enabled (allows_capture() == True):
    active_settings = tmp_path / "active-community.json"
    write_settings(active_settings, consent_choice="contribute", enabled=True, roots=[tmp_path])
    # validate_config stays purely structural on config dict (never coupled to live consent),
    # while validate_capture_config strictly enforces capture_mode == "public-transcript",
    # non-None absolute redactor_executable, and non-None absolute .py transcript_adapter.
    disabled_with_consent = {**disabled_cfg, "community_config": str(active_settings)}
    assert cli.validate_config(disabled_with_consent) == disabled_with_consent
    assert cli.validate_config({**base, "community_config": str(active_settings)})
    with pytest.raises(ValueError, match="only public-transcript"):
        cli.validate_capture_config(disabled_with_consent)
    with pytest.raises(ValueError, match="requires an absolute redactor_executable"):
        cli.validate_capture_config({**base, "community_config": str(active_settings)})
    with pytest.raises(ValueError, match="requires a transcript adapter"):
        cli.validate_capture_config({
            **base,
            "community_config": str(active_settings),
            "redactor_executable": str(tmp_path / "gitleaks"),
        })

    # Non-None malformed paths are always rejected, even when capture_mode == "disabled".
    for bad_scanner in ("relative/gitleaks", "", 123):
        with pytest.raises(ValueError, match="requires an absolute redactor_executable"):
            cli.validate_config({**disabled_cfg, "redactor_executable": bad_scanner})
    for bad_adapter in ("relative/parser.py", str(tmp_path / "parser.txt"), "", 123):
        with pytest.raises(ValueError, match="absolute local parser module path"):
            cli.validate_config({**disabled_cfg, "transcript_adapter": bad_adapter})

    # Unsupported capture_mode values preserve the "only public-transcript" message contract.
    for bad_mode in ("organize", None, 42):
        with pytest.raises(ValueError, match="only public-transcript"):
            cli.validate_config({**base, "capture_mode": bad_mode})
    with closing(Store(tmp_path / "engine-check", DOMAIN)) as store:
        assert Engine(store, capture_mode="disabled").capture_mode == "disabled"
        with pytest.raises(ValueError, match="only public-transcript"):
            Engine(store, capture_mode="organize")


def test_readonly_service_sync_query_explain_feedback_without_gitleaks(tmp_path, monkeypatch):
    backend = service_host.platform_backend()
    if backend is not None:
        monkeypatch.setattr(
            backend,
            "request_start",
            lambda cfg_path, _cfg: process.spawn_service(
                [sys.executable, "-m", "mindie_knowledge.loop.cli", "serve",
                 "--config", str(Path(cfg_path).resolve())],
                from_detached_starter=True,
            ),
        )

    published = unit("unit-readonly-aclgraph")
    feed_repo = _publish_graded_feed(tmp_path, published)
    sharing = tmp_path / "community.json"
    write_settings(sharing, consent_choice="read-only", enabled=False, roots=[])
    admission_path = tmp_path / "admission.sqlite3"
    Admission(admission_path).activate("readonly-session", project_root=str(tmp_path))

    config_path = tmp_path / "readonly.engine.json"
    config_data = {
        "root": str(tmp_path / "knowledge-root"),
        "domain": DOMAIN,
        "capture_mode": "disabled",
        "redactor_executable": None,
        "transcript_adapter": None,
        "community_config": str(sharing),
        "admission_path": str(admission_path),
        "feeds": [
            {
                "domain": DOMAIN,
                "repository": "mindie-agent/knowledge-vllm-ascend",
                "url": str(feed_repo),
                "ref": "main",
            }
        ],
    }
    config_path.write_text(json.dumps(config_data), encoding="utf-8", newline="\n")

    connection = cli.ensure_service(config_path)
    try:
        status = rpc(connection, "status", timeout=5.0)
        assert status["capture_pipeline"] == "disabled"
        assert status["worker_alive"] is True

        synced = rpc(connection, "sync", {}, timeout=30.0)
        assert len(synced) == 1 and synced[0]["status"] in {"synced", "current"}

        queried = rpc(
            connection,
            "query",
            {
                "query": "unit-readonly-aclgraph",
                "conditions": dict(OBSERVED),
                "_session_id": "readonly-session",
                "_session_verified": True,
            },
            timeout=10.0,
        )
        assert [row["id"] for row in queried["results"]] == ["unit-readonly-aclgraph"]
        unit_ref = queried["results"][0]["ref"]

        explained = rpc(
            connection,
            "explain",
            {
                "ref": unit_ref,
                "conditions": dict(OBSERVED),
                "_session_id": "readonly-session",
                "_session_verified": True,
            },
            timeout=10.0,
        )
        assert explained["id"] == "unit-readonly-aclgraph"

        feedback_ref = f"mindie://{DOMAIN}/units/unit-readonly-aclgraph@{published.revision}"
        voted = rpc(
            connection,
            "feedback",
            {
                "ref": feedback_ref,
                "rating": "up",
                "reason": "verified out-of-the-box without gitleaks",
                "_session_id": "readonly-session",
                "_session_verified": True,
            },
            timeout=10.0,
        )
        assert voted["publishable"] is False
    finally:
        assert rpc(connection, "stop_if_idle", timeout=5.0)["idle"] is True

    # Also verify read-only startup when capture_mode is default ("public-transcript"),
    # capture consent is disabled, and transcript_adapter points to a not-yet-installed .py path.
    absent_adapter_cfg = tmp_path / "absent-adapter.engine.json"
    absent_adapter_cfg.write_text(
        json.dumps({
            "root": str(tmp_path / "knowledge-root-2"),
            "domain": DOMAIN,
            "community_config": str(sharing),
            "admission_path": str(admission_path),
            "transcript_adapter": str(tmp_path / "not-yet-installed-adapter.py"),
            "redactor_executable": None,
        }),
        encoding="utf-8",
        newline="\n",
    )
    conn2 = cli.ensure_service(absent_adapter_cfg)
    try:
        assert rpc(conn2, "status", timeout=5.0)["worker_alive"] is True
    finally:
        assert rpc(conn2, "stop_if_idle", timeout=5.0)["idle"] is True


def test_stop_inert_when_consent_disabled_and_unavailable_when_consent_enabled_without_capture_config(
    tmp_path, monkeypatch,
):
    reports = []

    def capture_failure(operation, *, stage, category, exception=None, reportable=True):
        reports.append((operation, stage, category))
        return {"incident_id": "0" * 32, "logging_failed": False}

    monkeypatch.setattr(dfx, "failure", capture_failure)

    project = tmp_path / "project"
    project.mkdir()
    event = {
        "hook_event_name": "Stop",
        "identity_kind": "turn",
        "session_id": "manual-A",
        "turn_id": "turn-1",
        "mindie_activation": "synthetic-token",
        "harness": "codex",
        "transcript_path": str(project / "rollout.jsonl"),
    }

    # 1. Consent disabled on capture_mode="disabled" / missing-redactor config -> inert, 0 faults, 0 SQLite files.
    disabled_sharing = tmp_path / "disabled-sharing.json"
    write_settings(disabled_sharing, consent_choice="read-only", enabled=False, roots=[project])
    inert_root = tmp_path / "inert-state"
    inert_admission = tmp_path / "inert-admission.sqlite3"
    inert_cfg = tmp_path / "inert.engine.json"
    inert_cfg.write_text(
        json.dumps({
            "root": str(inert_root),
            "domain": DOMAIN,
            "capture_mode": "disabled",
            "community_config": str(disabled_sharing),
            "admission_path": str(inert_admission),
            "redactor_executable": None,
            "transcript_adapter": None,
        }),
        encoding="utf-8",
        newline="\n",
    )
    inert_res = cli.capture_hook(inert_cfg, event)
    assert inert_res["stage"] == "inert"
    assert inert_res["reason"] == "sharing-disabled"
    assert reports == []
    assert not inert_root.exists()
    assert not inert_admission.exists()

    # 2. Consent enabled on capture_mode="disabled" config -> stage="unavailable", reason="configuration".
    active_sharing = tmp_path / "active-sharing.json"
    write_settings(active_sharing, consent_choice="contribute", enabled=True, roots=[project])
    adm_path = make_admission(tmp_path, project_root=project)
    active_event = {**event, "mindie_activation": admission_token(adm_path)}

    disabled_active_cfg = tmp_path / "disabled-active.engine.json"
    disabled_active_cfg.write_text(
        json.dumps({
            "root": str(tmp_path / "disabled-active-state"),
            "domain": DOMAIN,
            "capture_mode": "disabled",
            "community_config": str(active_sharing),
            "admission_path": str(adm_path),
            "redactor_executable": None,
            "transcript_adapter": None,
        }),
        encoding="utf-8",
        newline="\n",
    )
    unavail_disabled = cli.capture_hook(disabled_active_cfg, active_event)
    assert unavail_disabled["stage"] == "unavailable"
    assert unavail_disabled["reason"] == "configuration"
    assert unavail_disabled["cause"] == "internal"
    assert reports == [("knowledge.capture", "handoff", "configuration_unavailable")]
    assert not (tmp_path / "disabled-active-state").exists()

    # 3. Consent enabled on public-transcript config missing redactor_executable -> unavailable / configuration.
    reports.clear()
    missing_redactor_cfg = tmp_path / "missing-redactor.engine.json"
    missing_redactor_cfg.write_text(
        json.dumps({
            "root": str(tmp_path / "missing-redactor-state"),
            "domain": DOMAIN,
            "capture_mode": "public-transcript",
            "community_config": str(active_sharing),
            "admission_path": str(adm_path),
            "redactor_executable": None,
            "transcript_adapter": transcript_double.__file__,
        }),
        encoding="utf-8",
        newline="\n",
    )
    unavail_missing = cli.capture_hook(missing_redactor_cfg, active_event)
    assert unavail_missing["stage"] == "unavailable"
    assert unavail_missing["reason"] == "configuration"
    assert unavail_missing["cause"] == "internal"
    assert reports == [("knowledge.capture", "handoff", "configuration_unavailable")]


def test_material_ingestion_and_distill_fail_without_installed_redactor(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    sharing = tmp_path / "sharing.json"
    write_settings(sharing, consent_choice="local-only", enabled=True, roots=[project])
    adm_path = make_admission(tmp_path, project_root=project)
    transcript = project / "rollout.jsonl"
    transcript.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "manual-A"}}) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with closing(Store(tmp_path / "store-capture", DOMAIN)) as store:
        # 1. _process_capture fails when capture_mode == "disabled".
        eng_disabled = Engine(
            store,
            settings_path=sharing,
            admission=Admission(adm_path),
            capture_mode="disabled",
            redactor_executable=None,
            transcript_adapter=None,
        )
        cap1 = eng_disabled.capture(session_id="manual-A", turn_id="t-disabled", transcript_path=str(transcript))
        eng_disabled._process(cap1["id"])
        row1 = store.capture_row(cap1["id"])
        assert row1["status"] == "failed"
        assert "transcript capture is disabled" in row1["detail"]

        # 2. _process_capture fails when redactor_executable is None or relative.
        for bad_exec in (None, "relative/gitleaks"):
            eng_no_redactor = Engine(
                store,
                settings_path=sharing,
                admission=Admission(adm_path),
                capture_mode="public-transcript",
                redactor_executable=bad_exec,
                transcript_adapter=transcript_double,
            )
            cap2 = eng_no_redactor.capture(
                session_id="manual-A",
                turn_id=f"t-{bad_exec}",
                transcript_path=str(transcript),
            )
            eng_no_redactor._process(cap2["id"])
            row2 = store.capture_row(cap2["id"])
            assert row2["status"] == "failed"
            assert "requires an installed redactor" in row2["detail"]

        # 3. Lazy transcript_adapter_path loading: missing file fails cleanly; once installed, loads cleanly.
        lazy_adapter = tmp_path / "lazy_adapter.py"
        eng_lazy = Engine(
            store,
            settings_path=sharing,
            admission=Admission(adm_path),
            capture_mode="public-transcript",
            redactor_executable=install_scanner(),
            transcript_adapter=None,
            transcript_adapter_path=str(lazy_adapter),
        )
        cap_missing = eng_lazy.capture(session_id="manual-A", turn_id="t-lazy-missing", transcript_path=str(transcript))
        eng_lazy._process(cap_missing["id"])
        assert store.capture_row(cap_missing["id"])["status"] == "failed"
        assert "transcript_adapter module is missing" in store.capture_row(cap_missing["id"])["detail"]

        shutil.copy(transcript_double.__file__, lazy_adapter)
        cap_ready = eng_lazy.capture(session_id="manual-A", turn_id="t-lazy-ready", transcript_path=str(transcript))
        eng_lazy._process(cap_ready["id"])
        assert eng_lazy.transcript is not None

    # 4. knowledge_distill strictly fails without an installed redactor (None, relative, or missing binary).
    proposal = {
        "schema_version": "mindie-distill-decision/1",
        "session_digest": session_key("manual-A"),
        "evidence_inputs": ["experiment_ledger"],
        "action": "noop",
        "drop_reason": "already_covered",
        "units": [],
    }
    with closing(Store(tmp_path / "store-distill", DOMAIN)) as store:
        eng_none = Engine(store, settings_path=sharing, admission=Admission(adm_path), redactor_executable=None)
        with pytest.raises(ValueError, match="requires an installed scanner"):
            eng_none.distill_selected(
                _distill_inputs(),
                session_digest=session_key("manual-A"),
                task_state="verified_milestone",
                proposal=proposal,
                cancel=threading.Event(),
            )

        # Also verify RPC rejection via Service.call("distill", ...) when redactor_executable is None.
        svc = Service(eng_none, admission=Admission(adm_path))
        try:
            with pytest.raises(ValueError, match="requires an installed scanner"):
                svc.call(
                    "distill",
                    {
                        "inputs": _distill_inputs(),
                        "task_state": "verified_milestone",
                        "proposal": proposal,
                        "_session_id": "manual-A",
                        "_activation": admission_token(adm_path),
                    },
                )
        finally:
            svc.http.server_close()

        eng_missing_bin = Engine(
            store,
            settings_path=sharing,
            admission=Admission(adm_path),
            redactor_executable=str(tmp_path / "nonexistent-gitleaks"),
        )
        with pytest.raises(ScannerUnavailable, match="scanner unavailable"):
            eng_missing_bin.distill_selected(
                _distill_inputs(),
                session_digest=session_key("manual-A"),
                task_state="verified_milestone",
                proposal=proposal,
                cancel=threading.Event(),
            )
        assert not store.graded.available()


def test_distill_empty_inputs_with_topic_redacts_before_persisting_related_snapshot(
    tmp_path, monkeypatch,
):
    from mindie_knowledge.graded import worker as graded_worker

    sharing = tmp_path / "sharing-rc1.json"
    write_settings(sharing, consent_choice="local-only", enabled=True, roots=[tmp_path])
    adm_path = make_admission(tmp_path, project_root=tmp_path)
    secret_topic = f"aclgraph capture: {tmp_path}"

    with closing(Store(tmp_path / "store-rc1", DOMAIN)) as store:
        # 1a. redactor_executable=None with inputs=[] + topic -> fails BEFORE any graded:related:* write.
        eng_none = Engine(store, settings_path=sharing, admission=Admission(adm_path), redactor_executable=None)
        svc_none = Service(eng_none, admission=Admission(adm_path))
        try:
            with pytest.raises(ValueError, match="requires an installed scanner"):
                svc_none.call(
                    "distill",
                    {
                        "inputs": [],
                        "task_state": "in_flight",
                        "topic": secret_topic,
                        "_session_id": "manual-A",
                        "_activation": admission_token(adm_path),
                    },
                )
        finally:
            svc_none.http.server_close()
        assert store.db.execute("SELECT count(*) FROM state WHERE key LIKE 'graded:related:%'").fetchone()[0] == 0

        # 1b. Nonexistent redactor_executable path with inputs=[] + topic -> fails with 0 graded:related:* rows.
        eng_missing = Engine(
            store,
            settings_path=sharing,
            admission=Admission(adm_path),
            redactor_executable=str(tmp_path / "nonexistent-gitleaks"),
        )
        with pytest.raises(ScannerUnavailable, match="scanner unavailable"):
            eng_missing.distill_selected(
                [],
                session_digest=session_key("manual-A"),
                task_state="in_flight",
                topic=secret_topic,
                cancel=threading.Event(),
            )
        assert store.db.execute("SELECT count(*) FROM state WHERE key LIKE 'graded:related:%'").fetchone()[0] == 0

        # 2. Positive case with install_scanner(): persists ONLY sanitized _history_scope and reuses cache hit.
        eng_ok = Engine(
            store,
            settings_path=sharing,
            admission=Admission(adm_path),
            redactor_executable=install_scanner(),
        )
        svc_ok = Service(eng_ok, admission=Admission(adm_path))
        seen_digests: list[str] = []
        orig_prepare = graded_worker.prepare_request

        def spy_prepare(*args, **kwargs):
            prepared_req = orig_prepare(*args, **kwargs)
            seen_digests.append(prepared_req["request_digest"])
            return prepared_req

        monkeypatch.setattr(graded_worker, "prepare_request", spy_prepare)
        try:
            first = svc_ok.call(
                "distill",
                {
                    "inputs": [],
                    "task_state": "in_flight",
                    "topic": secret_topic,
                    "_session_id": "manual-A",
                    "_activation": admission_token(adm_path),
                },
            )
            assert first["status"] == "noop"

            rows = store.db.execute("SELECT key, value FROM state WHERE key LIKE 'graded:related:%'").fetchall()
            assert len(rows) == 1
            raw_persisted = rows[0][1]
            assert str(tmp_path) not in raw_persisted
            assert tmp_path.as_posix() not in raw_persisted
            parsed_snapshot = json.loads(raw_persisted)
            assert set(parsed_snapshot) <= {"related_units", "rejected_history", "_history_scope"}
            sanitized_topic = parsed_snapshot["_history_scope"]["topic"]
            assert sanitized_topic != secret_topic
            assert sanitized_topic.startswith("aclgraph capture: ")
            assert ("<redacted:" in sanitized_topic) or ("[REDACTED" in sanitized_topic)

            meta_rows = store.db.execute(
                "SELECT key, value FROM state WHERE key LIKE 'graded:related-meta:%'"
            ).fetchall()
            assert len(meta_rows) == 1
            assert str(tmp_path) not in meta_rows[0][1]
            assert tmp_path.as_posix() not in meta_rows[0][1]
            parsed_meta = json.loads(meta_rows[0][1])
            assert set(parsed_meta) <= {"history_scope_digest", "redaction_rules"}

            # Second call must hit the persisted snapshot without re-querying _matching_related_units
            # and must produce the exact same request_digest.
            monkeypatch.setattr(
                graded_worker,
                "_matching_related_units",
                lambda *a, **k: pytest.fail("cache hit must not re-query _matching_related_units"),
            )
            second = svc_ok.call(
                "distill",
                {
                    "inputs": [],
                    "task_state": "in_flight",
                    "topic": secret_topic,
                    "_session_id": "manual-A",
                    "_activation": admission_token(adm_path),
                },
            )
            assert second == first
            assert len(seen_digests) == 2 and seen_digests[0] == seen_digests[1]
            assert store.db.execute("SELECT count(*) FROM state WHERE key LIKE 'graded:related:%'").fetchone()[0] == 1
        finally:
            svc_ok.http.server_close()


def test_domain_catalog_prepare_upgrades_readonly_routes_after_consent_switched_to_local_only(tmp_path):
    from mindie_knowledge import consent_store
    from mindie_knowledge.loop import settings as settings_mod
    from mindie_knowledge.loop.domain_catalog import prepare

    shared = tmp_path / "community.json"
    initial_settings = write_settings(shared, consent_choice="read-only", enabled=False, roots=[tmp_path])
    consent_path = Path(initial_settings.raw["consent_config"])

    default_cfg = tmp_path / "default.engine.json"
    readonly_engine = {
        "root": str(tmp_path / "data"),
        "domain": DOMAIN,
        "admission_path": str(tmp_path / "default-admission.sqlite3"),
        "community_config": str(shared),
        "redactor_executable": None,
        "transcript_adapter": None,
    }
    default_cfg.write_text(json.dumps(readonly_engine), encoding="utf-8", newline="\n")
    adapter = {"engine_config": str(default_cfg), "community_config": str(shared)}

    first = prepare(
        adapter,
        readonly_engine,
        config_directory=str(tmp_path / "runtime-1"),
        authority_directory=str(tmp_path / "authorities"),
        consent_config=str(consent_path),
    )
    assert len(first["domain_configs"]) == 9
    adapter["domain_configs"] = first["domain_configs"]

    # User later switches consent to local-only and enables capture on an existing domain's settings.
    consent_store.record_choice(consent_path, "local-only")
    operators_old = json.loads(Path(first["domain_configs"]["operators"]).read_text(encoding="utf-8"))
    settings_mod.write(
        operators_old["community_config"],
        enabled=True,
        repository="mindie-agent/knowledge-operators",
        project_roots=[tmp_path],
        consent_config=str(consent_path),
    )
    assert settings_mod.load(operators_old["community_config"]).allows_capture() is True

    # Upgrading domain routes must still read old redactor_executable=None configs via config_at() without error.
    upgraded_engine = {
        **readonly_engine,
        "redactor_executable": str(tmp_path / "gitleaks"),
        "transcript_adapter": transcript_double.__file__,
    }
    default_cfg.write_text(json.dumps(upgraded_engine), encoding="utf-8", newline="\n")
    second = prepare(
        adapter,
        upgraded_engine,
        config_directory=str(tmp_path / "runtime-2"),
        authority_directory=str(tmp_path / "authorities"),
        consent_config=str(consent_path),
    )
    operators_new = json.loads(Path(second["domain_configs"]["operators"]).read_text(encoding="utf-8"))
    assert operators_new["redactor_executable"] == str(tmp_path / "gitleaks")
    assert operators_new["transcript_adapter"] == transcript_double.__file__


def test_session_verified_query_explain_feedback_without_admission_path(tmp_path, monkeypatch):
    backend = service_host.platform_backend()
    if backend is not None:
        monkeypatch.setattr(
            backend,
            "request_start",
            lambda cfg_path, _cfg: process.spawn_service(
                [sys.executable, "-m", "mindie_knowledge.loop.cli", "serve",
                 "--config", str(Path(cfg_path).resolve())],
                from_detached_starter=True,
            ),
        )

    published = unit("unit-selftest-no-admission")
    feed_repo = _publish_graded_feed(tmp_path, published)
    sharing = tmp_path / "selftest-community.json"
    write_settings(sharing, consent_choice="read-only", enabled=False, roots=[])
    config_path = tmp_path / "selftest.engine.json"
    config_data = {
        "root": str(tmp_path / "selftest-state"),
        "domain": DOMAIN,
        "capture_mode": "disabled",
        "community_config": str(sharing),
        "feeds": [
            {
                "domain": DOMAIN,
                "repository": "mindie-agent/knowledge-vllm-ascend",
                "url": str(feed_repo),
                "ref": "main",
            }
        ],
    }
    config_path.write_text(json.dumps(config_data), encoding="utf-8", newline="\n")

    connection = cli.ensure_service(config_path)
    try:
        synced = rpc(connection, "sync", {}, timeout=30.0)
        assert len(synced) == 1 and synced[0]["status"] in {"synced", "current"}

        queried = rpc(
            connection,
            "query",
            {
                "query": "graph",
                "conditions": dict(OBSERVED),
                "_session_id": "selftest-session",
                "_session_verified": True,
            },
            timeout=10.0,
        )
        assert [row["id"] for row in queried["results"]] == ["unit-selftest-no-admission"]
        unit_ref = queried["results"][0]["ref"]

        explained = rpc(
            connection,
            "explain",
            {
                "ref": unit_ref,
                "conditions": dict(OBSERVED),
                "_session_id": "selftest-session",
                "_session_verified": True,
            },
            timeout=10.0,
        )
        assert explained["id"] == "unit-selftest-no-admission"

        voted = rpc(
            connection,
            "feedback",
            {
                "ref": f"mindie://{DOMAIN}/units/unit-selftest-no-admission@{published.revision}",
                "rating": "up",
                "reason": "selftest candidate mode without admission_path",
                "_session_id": "selftest-session",
                "_session_verified": True,
            },
            timeout=10.0,
        )
        assert voted["publishable"] is False

        # _activation token or capture/distill RPCs still strictly require configured admission.
        with pytest.raises(RequestRejected, match="no adapter admission is configured"):
            rpc(
                connection,
                "query",
                {
                    "query": "unit-selftest-no-admission",
                    "_session_id": "selftest-session",
                    "_activation": "unconfigured-token",
                },
                timeout=10.0,
            )
        with pytest.raises(RequestRejected, match="no adapter admission is configured"):
            rpc(
                connection,
                "distill",
                {
                    "inputs": [],
                    "task_state": "in_flight",
                    "_session_id": "selftest-session",
                    "_session_verified": True,
                },
                timeout=10.0,
            )

        assert list(tmp_path.rglob("*admission*.sqlite3")) == []
    finally:
        assert rpc(connection, "stop_if_idle", timeout=5.0)["idle"] is True
