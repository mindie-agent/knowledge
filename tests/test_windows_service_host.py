import json
import os
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from mindie_knowledge.loop import service_context as context
from mindie_knowledge.loop import windows_service as host


@pytest.fixture(autouse=True)
def windows_context_semantics(monkeypatch):
    """Exercise the Windows transfer contract even on non-Windows CI."""
    from mindie_knowledge.loop import service_host
    monkeypatch.setattr(context, "_windows", lambda: True)
    monkeypatch.setattr(service_host, "platform_backend", lambda: host)


def test_context_projects_only_declared_present_values_and_consumes_once(tmp_path, monkeypatch):
    monkeypatch.setenv("SELECTED_KEY", "synthetic-secret")
    monkeypatch.setenv("OTHER_SECRET", "must-not-transfer")
    monkeypatch.setattr(context, "_crypt", lambda raw, entropy, **kw: raw)
    binding = dict(sid="S-1-5-21-123", attempt="a" * 32, fingerprint="b" * 64)
    config = dict(service_launch_context=dict(
        paths={"NATIVE_HOME": str(tmp_path)}, environment=["SELECTED_KEY", "MISSING_OPTIONAL"]))
    raw = context.capture(config, binding)
    assert b"must-not-transfer" not in raw
    path = tmp_path / "context"
    path.write_bytes(raw)
    assert context.consume(path, config, binding) == dict(
        NATIVE_HOME=str(tmp_path), SELECTED_KEY="synthetic-secret")
    assert not path.exists()
    with pytest.raises(context.ContextError):
        context.consume(path, config, binding)


@pytest.mark.parametrize("key", [
    "PATH", "Path", "PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "DYLD_LIBRARY_PATH",
    "NODE_OPTIONS", "GIT_CONFIG_COUNT", "CODEX_THREAD_ID", "MINDIE_MAINTENANCE_OWNER",
])
def test_context_rejects_loader_and_session_overrides(key):
    with pytest.raises(context.ContextError):
        context.validate(dict(service_launch_context={"environment": [key]}))


def test_context_optional_variables_and_private_name_are_compatible():
    assert context.validate({}) == dict(paths={}, environment=[])
    assert context.validate(dict(service_launch_context={"environment": ["_KEY", "HTTPS_PROXY", "https_proxy"]}))


def test_context_rejects_relative_paths_and_case_shadow(tmp_path):
    for value in (
        dict(paths={"HOME_PATH": "relative"}),
        dict(paths={"HOME_PATH": str(tmp_path)}, environment=["home_path"]),
    ):
        with pytest.raises(context.ContextError):
            context.validate(dict(service_launch_context=value))


def test_context_binding_failure_never_applies_or_deletes_values(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "_crypt", lambda raw, entropy, **kw: raw)
    config = dict(service_launch_context={})
    path = tmp_path / "context"
    path.write_bytes(context.capture(config, {"attempt": "original"}))
    with pytest.raises(context.ContextError, match="binding"):
        context.consume(path, config, {"attempt": "different"})
    assert path.exists()


def test_context_cleanup_failure_prevents_admission(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "_crypt", lambda raw, entropy, **kw: raw)
    path = tmp_path / "context"
    path.write_bytes(context.capture({}, {}))
    monkeypatch.setattr(Path, "unlink", lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(context.ContextError, match="cleanup_failed"):
        context.consume(path, {}, {})


def test_context_caller_absence_clears_only_declared_host_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "_crypt", lambda raw, entropy, **kw: raw)
    config = dict(service_launch_context=dict(
        paths={"SELECTED_HOME": str(tmp_path)},
        environment=["SELECTED_KEY", "HTTPS_PROXY", "PRESENT_KEY"]))
    monkeypatch.delenv("SELECTED_KEY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.setenv("PRESENT_KEY", "current-synthetic-value")
    path = tmp_path / "context"
    path.write_bytes(context.capture(config, {}))
    monkeypatch.setenv("SELECTED_KEY", "stale-synthetic-key")
    monkeypatch.setenv("https_proxy", "stale-synthetic-proxy")
    monkeypatch.setenv("PRESENT_KEY", "stale-synthetic-value")
    monkeypatch.setenv("UNRELATED_SETTING", "preserved")
    context.apply(config, context.consume(path, config, {}))
    assert not any(key.casefold() in {"selected_key", "https_proxy"} for key in os.environ)
    assert os.environ["PRESENT_KEY"] == "current-synthetic-value"
    assert os.environ["SELECTED_HOME"] == str(tmp_path)
    assert os.environ["UNRELATED_SETTING"] == "preserved"


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI contract")
def test_dpapi_roundtrip_binds_entropy_and_is_not_plaintext():
    raw = b'{"key":"synthetic-secret"}'
    encrypted = context._crypt(raw, b"correct-owner-config-attempt")
    assert b"synthetic-secret" not in encrypted
    assert context._crypt(encrypted, b"correct-owner-config-attempt", decrypt=True) == raw
    with pytest.raises(context.ContextError):
        context._crypt(encrypted, b"other-attempt", decrypt=True)


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    from mindie_knowledge.loop.lifecycle import config_fingerprint
    import transcript_double
    config = dict(root=str(tmp_path / "data"), domain="test",
                  redactor_executable=str(tmp_path / "scanner"),
                  transcript_adapter=transcript_double.__file__)
    config_path = tmp_path / "engine.json"
    config_path.write_text(json.dumps(config))
    sid = "S-1-5-21-123"
    state = dict(task=None, calls=[], interrupt=None)
    def private(path):
        path.mkdir(parents=True, exist_ok=True)
        return sid
    monkeypatch.setattr(host, "_private", private)
    monkeypatch.setattr(host, "_verify_private", lambda path: sid)
    monkeypatch.setattr(context, "_crypt", lambda raw, entropy, **kw: raw)
    def record(path, cfg, root, owner):
        import uuid
        attempt = uuid.uuid4().hex
        return dict(schema=host.MARKER, sid=sid, label="synthetic-test",
                    attempt=attempt, config=str(path), fingerprint=config_fingerprint(path, cfg),
                    cwd=str(tmp_path),
                    python=str(tmp_path / "pythonw.exe"),
                    args=["-I", "-m", "mindie_knowledge.loop.windows_service",
                          "--manifest", str(root / "launch.json"), "--attempt", attempt])
    monkeypatch.setattr(host, "_record", record)
    monkeypatch.setattr(host, "_task", lambda label: state["task"])
    def execute(script, *, stage):
        state["calls"].append(stage)
        if stage == "register":
            root = host._paths(config)
            state["task"] = dict(xml=(root / "task.xml").read_text(),
                                 sid=sid, state="Ready", last_run="before", last_result=0)
        if state["interrupt"] == stage:
            raise KeyboardInterrupt()
        if stage == "start":
            state["task"].update(state="Running", last_run="after")
        if stage == "remove":
            state["task"] = None
        return ""
    monkeypatch.setattr(host, "_ps", execute)
    return config_path, config, state


def test_scheduler_start_does_not_claim_readiness(scheduler):
    path, config, state = scheduler
    request = host.request_start(path, config)
    assert request.externally_owned and request.poll() is None
    assert state["calls"] == ["register", "start"]
    assert not (Path(config["root"]) / "test/connection.json").exists()
    assert host.request_start(path, config).record == request.record
    assert state["calls"] == ["register", "start"]


def test_interrupted_registration_reconciles_exact_task_without_replaying_register(scheduler):
    path, config, state = scheduler
    state["interrupt"] = "register"
    with pytest.raises(KeyboardInterrupt):
        host.request_start(path, config)
    state["interrupt"] = None
    host.request_start(path, config)
    assert state["calls"] == ["register", "start"]


def test_interrupted_start_without_execution_proof_stays_unknown(scheduler):
    path, config, state = scheduler
    state["interrupt"] = "start"
    with pytest.raises(KeyboardInterrupt):
        host.request_start(path, config)
    state["interrupt"] = None
    with pytest.raises(host.ServiceHostError, match="start_outcome_unknown") as caught:
        host.request_start(path, config)
    assert caught.value.uncertain
    assert state["calls"] == ["register", "start"]
    record = host._read(host._paths(config) / "launch.json")
    assert (host._paths(config) / (record["attempt"] + ".context")).exists()


def test_accepted_start_receipt_failure_preserves_pending_context_and_never_replays(scheduler, monkeypatch):
    from mindie_knowledge.loop.diagnostics import _startup
    path, config, state = scheduler
    write = host._write
    def fail_started(target, value):
        if isinstance(value, dict) and value.get("phase") == "started":
            raise OSError("synthetic receipt storage failure")
        return write(target, value)
    monkeypatch.setattr(host, "_write", fail_started)
    with pytest.raises(host.ServiceHostError, match="start_receipt_write_failed") as caught:
        host.request_start(path, config)
    assert caught.value.uncertain
    root = host._paths(config)
    record = host._read(root / "launch.json")
    assert (root / (record["attempt"] + ".context")).exists()
    assert host._read(root / "operation.json")["phase"] == "starting"
    assert _startup(path, config)["host"] == dict(
        code="start_receipt_write_failed", stage="start", uncertain=True)
    host.request_start(path, config)
    assert state["calls"] == ["register", "start"]


def test_unrecognized_operation_never_authorizes_transfer_cleanup(scheduler):
    path, config, state = scheduler
    request = host.request_start(path, config)
    context_path = request.root / (request.record["attempt"] + ".context")
    host._write(request.root / "operation.json",
                dict(attempt=request.record["attempt"], phase="unrecognized"))
    host._cleanup_unstarted_context(request.root, request.record, context_path)
    assert context_path.exists()


def test_confirmed_start_error_allows_later_new_attempt(scheduler, monkeypatch):
    path, config, state = scheduler
    execute = host._ps
    def denied(script, *, stage):
        if stage == "start":
            state["calls"].append(stage)
            raise host.ServiceHostError("manager_not_spawned", stage=stage)
        return execute(script, stage=stage)
    monkeypatch.setattr(host, "_ps", denied)
    with pytest.raises(host.ServiceHostError):
        host.request_start(path, config)
    first = host._read(host._paths(config) / "launch.json")
    monkeypatch.setattr(host, "_ps", execute)
    second = host.request_start(path, config)
    assert second.record["attempt"] != first["attempt"]
    assert state["calls"] == ["register", "start", "register", "start"]


def test_manager_failure_after_spawn_is_not_proven_failed_by_idle_task(scheduler, monkeypatch):
    path, config, state = scheduler
    execute = host._ps
    def uncertain(script, *, stage):
        if stage == "start":
            state["calls"].append(stage)
            raise host.ServiceHostError("manager_operation_failed", stage=stage, uncertain=True)
        return execute(script, stage=stage)
    monkeypatch.setattr(host, "_ps", uncertain)
    with pytest.raises(host.ServiceHostError):
        host.request_start(path, config)
    monkeypatch.setattr(host, "_ps", execute)
    with pytest.raises(host.ServiceHostError, match="start_outcome_unknown"):
        host.request_start(path, config)
    assert state["calls"] == ["register", "start"]


def test_foreign_task_prevents_mutation(scheduler):
    path, config, state = scheduler
    host.request_start(path, config)
    state["task"]["sid"] = "S-1-5-21-999"
    with pytest.raises(host.ServiceHostError, match="foreign_task"):
        host.request_start(path, config)
    assert state["calls"] == ["register", "start"]


def test_additional_task_actions_or_principals_are_foreign(scheduler):
    path, config, state = scheduler
    request = host.request_start(path, config)
    original = state["task"]["xml"]
    for name, extra in (("Actions", "ComHandler"), ("Principals", "Principal")):
        root = ET.fromstring(original)
        ET.SubElement(root.find("{" + host.NS + "}" + name), "{" + host.NS + "}" + extra)
        state["task"]["xml"] = ET.tostring(root, encoding="unicode")
        with pytest.raises(host.ServiceHostError, match="foreign_task"):
            host._verify_task(state["task"], request.record, request.root / "launch.json")


def test_task_has_no_triggers_and_no_secrets(scheduler, monkeypatch):
    path, config, state = scheduler
    monkeypatch.setenv("TEST_KEY", "synthetic-secret")
    config["service_launch_context"] = {"environment": ["TEST_KEY"]}
    path.write_text(json.dumps(config))
    host.request_start(path, config)
    xml = state["task"]["xml"]
    assert "synthetic-secret" not in xml and "TEST_KEY" not in xml
    root = ET.fromstring(xml)
    assert list(root.find("{" + host.NS + "}Triggers")) == []
    assert root.find(".//{" + host.NS + "}WorkingDirectory") is None


def test_cwd_is_bound_to_the_one_use_transfer(scheduler):
    path, config, state = scheduler
    request = host.request_start(path, config)
    original = host._binding(request.record)
    changed = host._binding(dict(request.record, cwd=str(path.parent / "other")))
    assert original != changed
    with pytest.raises(context.ContextError, match="binding"):
        context.consume(request.root / (request.record["attempt"] + ".context"), config, changed)


def test_wrong_attempt_result_never_authorizes_restart(scheduler):
    path, config, state = scheduler
    request = host.request_start(path, config)
    state["task"].update(state="Ready")
    host._write(host._result_path(request.root, request.record),
                dict(attempt="another-attempt", status="finished", exit_code=0))
    with pytest.raises(host.ServiceHostError, match="attempt_mismatch"):
        host.request_start(path, config)
    assert state["calls"] == ["register", "start"]


def test_retirement_removes_only_exact_idle_task_and_verifies_absence(scheduler):
    path, config, state = scheduler
    host.request_start(path, config)
    state["task"].update(state="Ready", last_result=0)
    host.remove_owned(path, config)
    assert state["task"] is None and state["calls"][-1] == "remove"
    assert not (host._paths(config) / "launch.json").exists()


def test_failed_action_returns_safe_actual_stage(scheduler):
    path, config, state = scheduler
    request = host.request_start(path, config)
    host._write(host._result_path(request.root, request.record), dict(
        attempt=request.record["attempt"], status="failed", code="service_context_decryption_failed",
        stage="context", exit_code=1))
    with pytest.raises(host.ServiceHostError, match="decryption_failed") as caught:
        request.poll()
    assert caught.value.stage == "context"


def test_start_failure_survives_diagnostic_boundary_without_private_message(scheduler, monkeypatch):
    from mindie_knowledge.loop.diagnostics import _startup
    path, config, state = scheduler
    def unavailable(*args):
        raise host.ServiceHostError("start_outcome_unknown", stage="start", uncertain=True)
    monkeypatch.setattr(host, "_task", unavailable)
    with pytest.raises(host.ServiceHostError):
        host.request_start(path, config)
    assert _startup(path, config)["host"] == dict(
        code="start_outcome_unknown", stage="start", uncertain=True)


def test_cleanup_manager_running_returns_pending_without_stopping_work(scheduler, monkeypatch):
    path, config, state = scheduler
    host.request_start(path, config)
    clock = iter([0, 6])
    monkeypatch.setattr(host.time, "monotonic", lambda: next(clock))
    with pytest.raises(host.ServiceHostError, match="task_cleanup_pending"):
        host.remove_owned(path, config)
    assert state["task"]["state"] == "Running"
    assert state["calls"] == ["register", "start"]


def test_serve_applies_installed_paths_before_engine_loading(tmp_path, monkeypatch):
    from mindie_knowledge.loop import cli, lifecycle
    monkeypatch.delenv("SYNTHETIC_NATIVE_HOME", raising=False)
    class Observed(Exception):
        pass
    def acquire(path, config):
        assert os.environ["SYNTHETIC_NATIVE_HOME"] == str(tmp_path)
        raise Observed()
    monkeypatch.setattr(lifecycle, "acquire_consumer", acquire)
    with pytest.raises(Observed):
        cli._serve("unused", dict(service_launch_context={"paths": {"SYNTHETIC_NATIVE_HOME": str(tmp_path)}}))


def test_manager_ipc_is_bounded_and_spawned_failure_is_uncertain(monkeypatch):
    from mindie_knowledge.loop import process
    def bounded(argv, payload, **kwargs):
        assert kwargs["timeout"] == 30 and kwargs["max_output"] == host.LIMIT
        raise TimeoutError("private native stderr")
    monkeypatch.setattr(process, "bounded_run", bounded)
    with pytest.raises(host.ServiceHostError) as caught:
        host._ps("Start-ScheduledTask", stage="start")
    assert caught.value.uncertain
    assert "private" not in str(caught.value)


def test_windows_wake_uses_same_start_lock_as_query_and_retirement(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from mindie_knowledge.loop import cli, handoff
    from mindie_knowledge.loop.locks import StartLock
    from tests.test_review_followup import _ready
    path, *_ = _ready(tmp_path)
    config = cli.config_at(path)
    monkeypatch.setattr(handoff, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(handoff, "_probe", lambda *a, **k: "absent")
    def must_not_start(*args):
        pytest.fail("canonical startup lock must exclude the Stop starter")
    monkeypatch.setattr(host, "request_start", must_not_start)
    lock = StartLock(cli.connection_path(config).with_name("start.lock"))
    lock.acquire()
    try:
        assert handoff.request_wake(path, session_id="manual-A")["wake"] == "coalesced"
    finally:
        lock.release()


def test_windows_wake_retains_safe_host_failure(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from mindie_knowledge.loop import handoff
    from tests.test_review_followup import _ready
    path, *_ = _ready(tmp_path)
    monkeypatch.setattr(handoff, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(handoff, "_probe", lambda *a, **k: "absent")
    def fail(*args):
        raise host.ServiceHostError("start_outcome_unknown", stage="start", uncertain=True)
    monkeypatch.setattr(host, "request_start", fail)
    result = handoff.request_wake(path, session_id="manual-A")
    assert result["host"] == dict(stage="start", code="start_outcome_unknown", uncertain=True)
    assert handoff._result(host=result["host"])["host"] == result["host"]


@pytest.mark.skipif(os.name != "nt", reason="Windows extended config spelling")
def test_launch_record_preserves_existing_extended_config_fingerprint(tmp_path):
    from mindie_knowledge.filesystem import filesystem_path
    config = dict(root=str(tmp_path / "data"), domain="synthetic")
    path = filesystem_path(tmp_path / "engine.json")
    path.write_text(json.dumps(config))
    record = host._record(path, config, filesystem_path(tmp_path / "host"), "S-1-5-21-123")
    host._validate_record(record, record["config"], config, "S-1-5-21-123")
    assert record["config"].startswith("\\\\?\\")


def test_windowless_streams_supply_file_handles_only_for_missing_streams(monkeypatch):
    import io
    import sys
    supplied = io.StringIO()
    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setattr(sys, "stdout", supplied)
    monkeypatch.setattr(sys, "stderr", None)
    with host._windowless_streams():
        assert sys.stdout is supplied
        assert sys.stdin.read() == ""
        os.fstat(sys.stdin.fileno())
        os.fstat(sys.stderr.fileno())
        sys.stderr.write("synthetic service output\n")
        sys.stderr.flush()
        owned = sys.stdin, sys.stderr
    assert sys.stdin is None and sys.stderr is None
    assert sys.stdout is supplied and not supplied.closed
    assert all(stream.closed for stream in owned)


def test_windowless_streams_never_suppress_business_failure(monkeypatch):
    import sys
    monkeypatch.setattr(sys, "stdout", None)
    with pytest.raises(TypeError, match="synthetic business failure"):
        with host._windowless_streams():
            raise TypeError("synthetic business failure")
    assert sys.stdout is None


def test_original_startup_stage_and_class_survive_host_poll(scheduler, monkeypatch):
    from mindie_knowledge.loop import cli, diagnostics
    path, config, state = scheduler
    request = host.request_start(path, config)
    def failed_import(_config):
        raise TypeError("private synthetic detail must not leave the process")
    monkeypatch.setattr(cli, "load_transcript_adapter", failed_import)
    monkeypatch.chdir(path.parent)
    with pytest.raises(TypeError):
        host.run(request.root / "launch.json", request.record["attempt"])
    before = diagnostics._startup(path, config)
    assert before["stage"] == "transcript_adapter" and before["error_class"] == "TypeError"
    result = host._read(host._result_path(request.root, request.record))
    assert result["status"] == "failed"
    assert result["startup"]["stage"] == "transcript_adapter"
    assert not (request.root / (request.record["attempt"] + ".context")).exists()
    with pytest.raises(host.ServiceHostError, match="transcript_adapter:TypeError") as caught:
        request.poll()
    diagnostics.record_startup_failure(path, config, "service_host", caught.value)
    assert diagnostics._startup(path, config) == before
    assert "private synthetic detail" not in str(caught.value)
    assert "private synthetic detail" not in diagnostics._startup_path(config).read_text()


def test_host_rejects_nonstatic_startup_details():
    error = host.ServiceHostError("service_start_failed", stage="service", startup=dict(
        stage="private-source", error_class="TypeError", cause="import_failed"))
    assert error._mindie_startup_failure is None
    assert "private-source" not in str(error)


def test_acl_check_returns_the_owner_it_actually_verified(tmp_path, monkeypatch):
    calls = []
    def checked(script, *, stage):
        calls.append(stage)
        return "S-1-5-21-123"
    monkeypatch.setattr(host, "_ps", checked)
    assert host._verify_private(tmp_path) == "S-1-5-21-123"
    assert calls == ["ownership"]
    monkeypatch.setattr(host, "_ps", lambda *a, **kw: "not-a-sid")
    with pytest.raises(host.ServiceHostError, match="identity_unavailable"):
        host._verify_private(tmp_path)


@pytest.fixture
def task_projection(tmp_path):
    record = dict(label="MindIE Knowledge " + "a" * 24, sid="S-1-5-21-123",
                  python=str(tmp_path / "pythonw.exe"), args=["-I", "-m", "synthetic"])
    manifest = tmp_path / "launch.json"
    value = dict(xml=host._xml(record, manifest).decode(), sid=record["sid"],
                 state="Ready", run_level=0, last_result=267011,
                 last_run="1999-11-29T16:00:00.0000000Z")
    return record, manifest, value


def test_com_query_projects_effective_default_without_weakening_ownership(task_projection, monkeypatch):
    record, manifest, value = task_projection
    tree = ET.fromstring(value["xml"])
    principal = tree.find("{" + host.NS + "}Principals/{" + host.NS + "}Principal")
    principal.remove(principal.find("{" + host.NS + "}RunLevel"))
    value["xml"] = ET.tostring(tree, encoding="unicode")
    monkeypatch.setattr(host, "_ps", lambda *a, **kw: json.dumps(value))
    projected = host._task(record["label"])
    host._verify_task(projected, record, manifest)
    assert projected["last_run"] == value["last_run"]
    assert projected["last_result"] == 267011
    value["run_level"] = 1
    with pytest.raises(host.ServiceHostError, match="foreign_task"):
        host._verify_task(host._task(record["label"]), record, manifest)


@pytest.mark.parametrize("changes", [
    {"state": None}, {"state": "Unknown"}, {"state": "unsupported"},
    {"run_level": None}, {"run_level": True}, {"run_level": 2},
    {"run_level": 1},
])
def test_com_query_rejects_unknown_state_and_inconsistent_explicit_privilege(
        task_projection, monkeypatch, changes):
    record, _, value = task_projection
    value.update(changes)
    monkeypatch.setattr(host, "_ps", lambda *a, **kw: json.dumps(value))
    with pytest.raises(host.ServiceHostError, match="manager_status_invalid"):
        host._task(record["label"])


def test_com_query_does_not_turn_manager_failure_into_task_absence(monkeypatch):
    failure = host.ServiceHostError("manager_operation_failed", stage="query",
                                   uncertain=True, manager_hresult=0x80070005)
    def denied(*args, **kwargs):
        raise failure
    monkeypatch.setattr(host, "_ps", denied)
    with pytest.raises(host.ServiceHostError) as caught:
        host._task("synthetic")
    assert caught.value is failure
    monkeypatch.setattr(host, "_ps", lambda *a, **kw: "null")
    assert host._task("synthetic") is None
