import json
import os
from pathlib import Path

import pytest

from mindie_knowledge.loop import service_context as context
from mindie_knowledge.loop import service_host
from mindie_knowledge.loop import systemd_service as host


@pytest.fixture
def manager(tmp_path, monkeypatch):
    from mindie_knowledge.loop.lifecycle import config_fingerprint
    import transcript_double
    config = dict(root=str(tmp_path / "data"), domain="test",
                  redactor_executable=str(tmp_path / "scanner"),
                  transcript_adapter=transcript_double.__file__)
    path = tmp_path / "engine.json"
    path.write_text(json.dumps(config))
    state = dict(unit=None, calls=[], interrupt=False)
    monkeypatch.setattr(host, "_supported", lambda: None)
    monkeypatch.setattr(host, "_private", lambda root: (root.mkdir(parents=True, exist_ok=True), 123)[1])
    monkeypatch.setattr(host, "_verify_private", lambda root: 123)
    monkeypatch.setattr(context, "_windows", lambda: False)

    def record(config_path, cfg, root, uid):
        import uuid
        attempt = uuid.uuid4().hex
        return dict(schema=host.MARKER, uid=uid, attempt=attempt,
                    label="mindie-knowledge-" + "a" * 16 + "-" + attempt + ".service",
                    config=str(config_path), cwd=str(tmp_path),
                    fingerprint=config_fingerprint(config_path, cfg),
                    python="/usr/bin/python3",
                    args=["-I", "-m", "mindie_knowledge.loop.systemd_service", "--manifest",
                          str(root / "launch.json"), "--attempt", attempt])
    monkeypatch.setattr(host, "_record", record)
    monkeypatch.setattr(host, "_unit", lambda label: state["unit"])

    def execute(command, *, stage):
        state["calls"].append((stage, command))
        if stage == "start":
            current = host._read(host._paths(config) / "launch.json")
            state["unit"] = dict(
                host._PROPERTIES, Id=current["label"], Transient="yes",
                Description=host._description(host._paths(config) / "launch.json"),
                ActiveState="active", LoadState="loaded",
                exec=[current["python"], [current["python"], *current["args"]], False])
            if state["interrupt"]:
                raise host.ServiceHostError("manager_operation_failed", stage="start", uncertain=True)
        elif stage == "remove":
            state["unit"] = None
        return ""
    monkeypatch.setattr(host, "_manager", execute)
    return path, config, state


def test_start_is_not_readiness_and_does_not_expose_transfer(manager):
    path, config, state = manager
    request = host.request_start(path, config)
    assert request.externally_owned and request.poll() is None
    assert not (Path(config["root"]) / "test/connection.json").exists()
    command = state["calls"][0][1]
    assert "--expand-environment=no" in command
    assert all("--property=" + key + "=" + value in command for key, value in host._PROPERTIES.items())
    assert "--scope" not in command
    assert host.request_start(path, config).record == request.record
    assert len(state["calls"]) == 1


def test_interrupted_start_reconciles_active_unit_without_replaying(manager):
    path, config, state = manager
    state["interrupt"] = True
    with pytest.raises(host.ServiceHostError):
        host.request_start(path, config)
    state["interrupt"] = False
    request = host.request_start(path, config)
    assert request.poll() is None
    assert len(state["calls"]) == 1


def test_absent_unit_after_interrupted_start_is_unknown_not_permission_to_retry(manager):
    path, config, state = manager
    state["interrupt"] = True
    with pytest.raises(host.ServiceHostError):
        host.request_start(path, config)
    state["unit"] = None
    state["interrupt"] = False
    with pytest.raises(host.ServiceHostError, match="start_outcome_unknown") as error:
        host.request_start(path, config)
    assert error.value.uncertain and len(state["calls"]) == 1
    record = host._read(host._paths(config) / "launch.json")
    assert (host._paths(config) / (record["attempt"] + ".context")).exists()


def test_successful_start_record_failure_keeps_transfer_and_reconciles(manager, monkeypatch):
    path, config, state = manager
    original = host._write
    def failed_record(target, value):
        if isinstance(value, dict) and value.get("phase") == "started":
            raise OSError("synthetic record failure")
        original(target, value)
    monkeypatch.setattr(host, "_write", failed_record)
    with pytest.raises(host.ServiceHostError, match="start_receipt_write_failed"):
        host.request_start(path, config)
    request = host.request_start(path, config)
    assert (request.root / (request.record["attempt"] + ".context")).exists()
    assert len(state["calls"]) == 1


def test_inactive_unit_without_terminal_result_is_not_a_finished_attempt(manager):
    path, config, state = manager
    first = host.request_start(path, config)
    state["unit"]["ActiveState"] = "inactive"
    with pytest.raises(host.ServiceHostError, match="start_outcome_unknown"):
        first.poll()
    with pytest.raises(host.ServiceHostError, match="start_outcome_unknown"):
        host.request_start(path, config)
    assert len(state["calls"]) == 1


def _dead_pid():
    import subprocess
    import sys
    process = subprocess.Popen([sys.executable, "-c", ""])
    assert process.wait() == 0
    return process.pid


@pytest.mark.skipif(os.name == "nt", reason="POSIX pid liveness")
def test_dead_attempt_with_live_receipt_retires_and_starts_anew(manager):
    path, config, state = manager
    first = host.request_start(path, config)
    host._write(host._result_path(first.root, first.record),
                dict(attempt=first.record["attempt"], status="starting", pid=_dead_pid()))
    state["unit"] = None
    second = host.request_start(path, config)
    assert second.record["attempt"] != first.record["attempt"]
    assert not (first.root / (first.record["attempt"] + ".context")).exists()
    assert [stage for stage, _ in state["calls"]] == ["start", "start"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX pid liveness")
def test_dead_attempt_retires_with_inactive_unit_and_cleans_up(manager, monkeypatch):
    records = []
    monkeypatch.setattr("mindie_knowledge.loop.dfx.failure",
                        lambda *args, **kwargs: records.append((args, kwargs)))
    path, config, state = manager
    first = host.request_start(path, config)
    host._write(host._result_path(first.root, first.record),
                dict(attempt=first.record["attempt"], status="starting", pid=_dead_pid()))
    state["unit"]["ActiveState"] = "inactive"
    second = host.request_start(path, config)
    assert second.record["attempt"] != first.record["attempt"]
    assert [stage for stage, _ in state["calls"]] == ["start", "remove", "start"]
    assert records == [(("knowledge.service",), dict(stage="reconcile",
                                                    category="stale_launch_attempt_retired",
                                                    reportable=False))]


@pytest.mark.skipif(os.name == "nt", reason="POSIX pid liveness")
def test_live_recorded_pid_is_never_retired(manager):
    path, config, state = manager
    first = host.request_start(path, config)
    host._write(host._result_path(first.root, first.record),
                dict(attempt=first.record["attempt"], status="starting", pid=os.getpid()))
    state["unit"] = None
    with pytest.raises(host.ServiceHostError, match="start_outcome_unknown"):
        host.request_start(path, config)
    assert len(state["calls"]) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM")
def test_sigterm_stop_writes_terminal_finished_receipt(tmp_path, monkeypatch):
    import signal
    from mindie_knowledge.loop import cli
    import transcript_double
    config = dict(root=str(tmp_path / "data"), domain="test",
                  redactor_executable=str(tmp_path / "scanner"),
                  transcript_adapter=transcript_double.__file__)
    config_path = tmp_path / "engine.json"
    config_path.write_text(json.dumps(config))
    root = tmp_path / "service"
    root.mkdir()
    attempt = "b" * 32
    record = dict(attempt=attempt, config=str(config_path), cwd=str(tmp_path))
    service_host._write(root / "launch.json", record)
    binding = {"attempt": attempt, "cwd": str(tmp_path)}
    service_host._write(root / (attempt + ".context"), context.capture(config, binding))

    def stopped(_config_path, _config):
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("SIGTERM did not interrupt serve")

    monkeypatch.setattr(cli, "_serve", stopped)
    monkeypatch.chdir(tmp_path)
    previous = signal.getsignal(signal.SIGTERM)
    code = service_host.run_owned(root / "launch.json", attempt,
                                  verify_owner=lambda _root: 123,
                                  validate_record=lambda *args: None,
                                  binding=lambda _record: binding)
    assert code == 0
    result = service_host.read_result(root, record)
    assert result["status"] == "finished" and result["exit_code"] == 0
    assert signal.getsignal(signal.SIGTERM) == previous
    assert not (root / (attempt + ".context")).exists()


def test_known_terminal_result_allows_new_attempt_after_exact_cleanup(manager):
    path, config, state = manager
    first = host.request_start(path, config)
    state["unit"]["ActiveState"] = "failed"
    host._write(host._result_path(first.root, first.record),
                dict(attempt=first.record["attempt"], status="failed", exit_code=1, code="service_start_failed"))
    second = host.request_start(path, config)
    assert first.record["attempt"] != second.record["attempt"]
    assert [stage for stage, _ in state["calls"]] == ["start", "remove", "start"]


def test_foreign_unit_or_wrong_result_never_authorizes_new_start(manager):
    path, config, state = manager
    first = host.request_start(path, config)
    state["unit"]["exec"][1] += ["unexpected"]
    with pytest.raises(host.ServiceHostError, match="foreign_task"):
        host.request_start(path, config)
    state["unit"] = None
    host._write(host._result_path(first.root, first.record), dict(attempt="different", status="finished"))
    with pytest.raises(host.ServiceHostError, match="launch_attempt_mismatch"):
        host.request_start(path, config)
    assert len(state["calls"]) == 1


def test_manager_failure_never_uses_direct_spawn(manager, monkeypatch):
    from mindie_knowledge.loop import process
    path, config, state = manager
    monkeypatch.setattr(host, "_supported", lambda: (_ for _ in ()).throw(
        host.ServiceHostError("manager_operation_failed", stage="query")))
    monkeypatch.setattr(process, "spawn_service", lambda *a, **kw: pytest.fail("unprotected fallback"))
    with pytest.raises(host.ServiceHostError, match="manager_operation_failed"):
        host.request_start(path, config)
    assert state["calls"] == []


@pytest.mark.parametrize("status,exit_code", [
    ("finished", None), ("failed", False), ("unknown", 0), (["finished"], 0),
])
def test_invalid_result_cannot_look_running_or_authorize_another_start(manager, status, exit_code):
    path, config, state = manager
    request = host.request_start(path, config)
    host._write(host._result_path(request.root, request.record),
                dict(attempt=request.record["attempt"], status=status, exit_code=exit_code))
    with pytest.raises(host.ServiceHostError, match="launch_record_invalid"):
        request.poll()
    state["unit"] = None
    with pytest.raises(host.ServiceHostError, match="launch_record_invalid"):
        host.request_start(path, config)
    assert len(state["calls"]) == 1


def test_removal_does_not_kill_an_active_service(manager, monkeypatch):
    path, config, state = manager
    host.request_start(path, config)
    ticks = iter([0, 6])
    monkeypatch.setattr(host.time, "monotonic", lambda: next(ticks))
    with pytest.raises(host.ServiceHostError, match="task_cleanup_pending"):
        host.remove_owned(path, config)
    assert len(state["calls"]) == 1


def test_typed_query_and_unknown_state_are_strict(monkeypatch):
    values = dict.fromkeys(host._FIELDS, "")
    values.update(Id="synthetic.service", LoadState="loaded", ActiveState="active")
    replies = [
        "\n".join(key + "=" + value for key, value in values.items()),
        json.dumps(dict(type="o", data=["/org/freedesktop/systemd1/unit/synthetic_2eservice"])),
        json.dumps(dict(type="a(sasbttttuii)", data=[["/usr/bin/python3", ["/usr/bin/python3"], False, 0, 0, 0, 0, 1, 0, 0]])),
    ]
    monkeypatch.setattr(host, "_manager", lambda *a, **k: replies.pop(0))
    assert host._unit("synthetic.service")["exec"] == ["/usr/bin/python3", ["/usr/bin/python3"], False]
    values["ActiveState"] = "new-unknown-state"
    replies.append("\n".join(key + "=" + value for key, value in values.items()))
    with pytest.raises(host.ServiceHostError, match="manager_status_invalid"):
        host._unit("synthetic.service")


@pytest.mark.skipif(os.name == "nt", reason="POSIX UID/mode and case semantics")
def test_posix_transfer_is_private_one_use_and_preserves_case(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "_windows", lambda: False)
    config = dict(service_launch_context=dict(environment=["SELECTED_KEY", "HTTPS_PROXY"]))
    monkeypatch.delenv("SELECTED_KEY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    path = tmp_path / "context"
    path.write_bytes(context.capture(config, {"attempt": "one"}))
    with pytest.raises(context.ContextError, match="file_invalid"):
        context.consume(path, config, {"attempt": "one"})
    path.chmod(0o600)
    monkeypatch.setenv("SELECTED_KEY", "stale")
    monkeypatch.setenv("https_proxy", "separate-posix-name")
    context.apply(config, context.consume(path, config, {"attempt": "one"}))
    assert "SELECTED_KEY" not in os.environ
    assert os.environ["https_proxy"] == "separate-posix-name"
    assert not path.exists()


def test_containment_mark_is_process_local_and_cannot_be_inherited(monkeypatch):
    monkeypatch.setattr(service_host, "_contained_pid", os.getpid())
    assert service_host.process_is_contained()
    monkeypatch.setattr(service_host, "_contained_pid", os.getpid() + 1)
    assert not service_host.process_is_contained()


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO")
def test_context_rejects_fifo_without_waiting_for_a_writer(tmp_path, monkeypatch):
    import subprocess
    import sys
    fifo = tmp_path / "transfer"
    os.mkfifo(fifo, 0o600)
    result = subprocess.run([
        sys.executable, "-c",
        "import sys; from mindie_knowledge.loop.service_context import consume, ContextError\n"
        "try: consume(sys.argv[1], {}, {})\n"
        "except ContextError as error: print(error.code)\n",
        str(fifo),
    ], capture_output=True, text=True, timeout=3)
    assert result.returncode == 0
    assert result.stdout.strip() == "service_context_file_invalid"


def test_linux_unit_uses_cgroup_but_direct_api_uses_shared_owner_helper(monkeypatch):
    from mindie_knowledge.loop import process
    monkeypatch.setattr(process.sys, "platform", "linux")
    monkeypatch.setattr(process.os, "name", "posix")
    monkeypatch.setattr(service_host, "_contained_pid", None)
    monkeypatch.setattr(process.posix_owner, "spawn", lambda *a, **kw: "helper")
    monkeypatch.setattr(process.subprocess, "Popen", lambda *a, **kw: "cgroup")
    assert process._spawn(["synthetic"], None) == "helper"
    monkeypatch.setattr(service_host, "_contained_pid", os.getpid())
    assert process._spawn(["synthetic"], None) == "cgroup"
