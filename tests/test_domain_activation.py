"""Explicit native domain binding reuses target consent and Admission storage."""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from conftest import write_settings
from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.domain_routes import (
    activate_selected_domain, deactivate_registered_domains, register_domain_config, select_engine_config,
)


def engine_config(tmp_path, domain, *, consent="contribute", scope=None):
    scope = scope or tmp_path
    settings = tmp_path / (domain + "-community.json")
    write_settings(settings, consent_choice=consent, roots=[scope])
    admission = tmp_path / (domain + "-admission.sqlite3")
    path = tmp_path / (domain + "-engine.json")
    path.write_text(json.dumps(dict(root=str(tmp_path / "state"), domain=domain,
        admission_path=str(admission), community_config=str(settings),
        transcript_adapter=str(Path(__file__).parent / "transcript_double.py"),
        redactor_executable=sys.executable)))
    return path, admission


def test_register_cli_preserves_default_and_creates_no_target_lease(tmp_path):
    default, _ = engine_config(tmp_path, "vllm-ascend")
    target, admission = engine_config(tmp_path, "operators")
    adapter = tmp_path / "native.json"
    original = dict(engine_config=str(default), consent_config="preserved-native-choice", extension={"keep": 42})
    adapter.write_text(json.dumps(original))
    result = subprocess.run([sys.executable, "-m", "mindie_knowledge.loop.domain_routes", "register",
        "--adapter-config", str(adapter), "--domain", "operators", "--engine-config", str(target)],
        capture_output=True, text=True, timeout=5, cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 0, result.stderr
    saved = json.loads(adapter.read_text())
    assert {key: saved[key] for key in original} == original
    assert select_engine_config(saved["engine_config"], domain="operators", domain_configs=saved["domain_configs"]) == target
    assert json.loads(result.stdout)["activation"] == "not_requested"
    assert not admission.exists()


def test_explicit_native_activation_mints_target_lease_without_copying_default(tmp_path):
    default, default_admission = engine_config(tmp_path, "vllm-ascend")
    target, target_admission = engine_config(tmp_path, "operators")
    first = Admission(default_admission).activate("native-task", project_root=str(tmp_path), root_session="native-root")
    arguments = dict(domain="operators", domain_configs={"operators": str(target)},
                     session="native-task", project_root=str(tmp_path), root_session="native-root")
    selected = activate_selected_domain(default, **arguments)
    assert selected["domain"] == "operators" and selected["engine_config"] == str(target)
    assert selected["generation"]
    lease = selected["lease"]
    assert lease["session"] == first["session"] and lease["root_session"] == "native-root"
    assert lease["token"] != first["token"]
    assert Admission(default_admission).capture_lease("native-task", first["token"]) == first
    assert Admission(target_admission).capture_lease("native-task", lease["token"]) == lease
    assert activate_selected_domain(default, **arguments)["lease"] == lease
    Admission(target_admission).deactivate("native-task")
    renewed = activate_selected_domain(default, **arguments)["lease"]
    assert renewed["token"] != lease["token"]
    assert Admission(default_admission).active_lease("native-task") == first


def test_target_consent_and_scope_must_authorize_before_activation(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    default, default_admission = engine_config(tmp_path, "vllm-ascend")
    target, target_admission = engine_config(tmp_path, "operators", consent="read-only", scope=allowed)
    first = Admission(default_admission).activate("native-task", project_root=str(allowed))
    arguments = dict(domain="operators", domain_configs={"operators": str(target)},
                     session="native-task", project_root=str(allowed))
    with pytest.raises(ValueError, match="not authorized"):
        activate_selected_domain(default, **arguments)
    assert not target_admission.exists()
    target, _ = engine_config(tmp_path, "operators", scope=allowed)
    with pytest.raises(ValueError, match="outside"):
        activate_selected_domain(default, **{**arguments, "project_root": str(tmp_path / "outside")})
    assert not target_admission.exists()
    assert Admission(default_admission).active_lease("native-task") == first


def test_failed_registration_retains_exact_adapter_config(tmp_path):
    target, _ = engine_config(tmp_path, "operators")
    adapter = tmp_path / "native.json"
    original = '{"engine_config":"existing-default", "extension":{"keep":true}}\n'
    adapter.write_text(original)
    with pytest.raises(ValueError, match="another domain"):
        register_domain_config(adapter, domain="vllm-ascend", engine_config_path=target)
    assert adapter.read_text() == original


def test_native_deactivation_revokes_registered_domains_after_scope_change_without_affecting_other_session(tmp_path):
    default, first_path = engine_config(tmp_path, "vllm-ascend", consent="read-only")
    target, second_path = engine_config(tmp_path, "operators", consent="read-only", scope=tmp_path / "moved")
    untouched, unused_path = engine_config(tmp_path, "unopened")
    for path in (first_path, second_path):
        admission = Admission(path)
        admission.activate("current", project_root=str(tmp_path))
        admission.activate("other", project_root=str(tmp_path))
    result = deactivate_registered_domains(default, session="current", domain_configs={
        "operators": str(target), "vllm-ascend": str(default), "unopened": str(untouched)})
    assert result["status"] == "inactive" and result["changed"]
    for path in (first_path, second_path):
        assert Admission(path).active_lease("current") is None
        assert Admission(path).active_lease("other")["enabled"]
    assert not unused_path.exists()
    repeated = deactivate_registered_domains(default, session="current", domain_configs={"operators": str(target)})
    assert repeated["status"] == "inactive" and not repeated["changed"]


def test_native_deactivation_lock_failure_is_partial_and_other_domain_still_revokes_within_total_budget(tmp_path):
    default, first_path = engine_config(tmp_path, "vllm-ascend")
    target, second_path = engine_config(tmp_path, "operators")
    first, second = Admission(first_path), Admission(second_path)
    first.activate("current", project_root=str(tmp_path))
    second.activate("current", project_root=str(tmp_path))
    with first._database() as locked:
        locked.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        result = deactivate_registered_domains(default, session="current", timeout=0.2,
                                               domain_configs={"operators": str(target)})
        elapsed = time.monotonic() - started
        locked.rollback()
    assert result["status"] == "partial" and elapsed < 0.8
    outcomes = {item["domain"]: item for item in result["domains"]}
    assert outcomes["vllm-ascend"]["status"] == "unavailable"
    assert outcomes["operators"]["status"] == "inactive"
    assert first.active_lease("current")["enabled"]
    assert second.active_lease("current") is None


def test_admission_deactivation_in_process_lock_respects_caller_timeout(tmp_path):
    _, path = engine_config(tmp_path, "operators")
    admission = Admission(path)
    admission.activate("current", project_root=str(tmp_path))
    entered, release = threading.Event(), threading.Event()

    def hold_existing_lock():
        with admission._lock:
            entered.set()
            release.wait(timeout=2)

    thread = threading.Thread(target=hold_existing_lock)
    thread.start()
    try:
        assert entered.wait(timeout=1)
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="in-process lock"):
            admission.deactivate("current", timeout=0.05)
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        thread.join(timeout=1)
    assert admission.active_lease("current")["enabled"]
