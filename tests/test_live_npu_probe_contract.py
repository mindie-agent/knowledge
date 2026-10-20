"""The live probe may skip unavailable SSH, never connected-host failures."""
import subprocess

import pytest

import test_live_npu_knowledge_loop as live_loop
from test_live_npu_knowledge_loop import (
    A2_CONTAINER,
    A2_HOST,
    A3_CONTAINER,
    A3_HOST,
    _live_targets,
    _ssh_probe,
)


def test_connected_remote_command_failure_is_a_test_failure(monkeypatch):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "No such container: test-container")
    monkeypatch.setattr(subprocess, "run", run)
    try:
        with pytest.raises(pytest.fail.Exception, match="No such container"):
            _ssh_probe("test-host", "test-container")
    except pytest.skip.Exception:
        pytest.fail("A connected remote command failure was incorrectly skipped")


def test_connected_remote_command_timeout_is_a_test_failure(monkeypatch):
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(
            argv, 15, stderr=b"MINDIE_NPU_PROBE_CONNECTED\nremote command stalled",
        )
    monkeypatch.setattr(subprocess, "run", run)
    try:
        with pytest.raises(pytest.fail.Exception, match="remote command"):
            _ssh_probe("test-host", "test-container")
    except pytest.skip.Exception:
        pytest.fail("A connected remote command timeout was incorrectly skipped")


def test_unavailable_ssh_remains_an_explicit_skip(monkeypatch):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 255, "", "Connection refused")
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(pytest.skip.Exception, match="Connection refused"):
        _ssh_probe("test-host", "test-container")


def test_connected_probe_preserves_hardware_output(monkeypatch):
    output = "Total Count:1\nChip Count:8\n"
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, output, "MINDIE_NPU_PROBE_CONNECTED\n")
    monkeypatch.setattr(subprocess, "run", run)
    assert _ssh_probe("test-host", "test-container") == output.strip()


def test_live_targets_env_override_and_blank_fallback(monkeypatch):
    for key in (
        "MINDIE_LIVE_A2_HOST",
        "MINDIE_LIVE_A2_CONTAINER",
        "MINDIE_LIVE_A3_HOST",
        "MINDIE_LIVE_A3_CONTAINER",
    ):
        monkeypatch.delenv(key, raising=False)
    assert _live_targets() == {
        "a2": (A2_HOST, A2_CONTAINER),
        "a3": (A3_HOST, A3_CONTAINER),
    }

    monkeypatch.setenv("MINDIE_LIVE_A2_CONTAINER", "mindie-bench-a2-custom-20261020")
    monkeypatch.setenv("MINDIE_LIVE_A3_CONTAINER", "   ")
    assert _live_targets() == {
        "a2": (A2_HOST, "mindie-bench-a2-custom-20261020"),
        "a3": (A3_HOST, A3_CONTAINER),
    }


def test_configured_container_override_flows_into_probe_and_redacted_package(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("MINDIE_LIVE_A2_CONTAINER", "mindie-bench-a2-override-20261020")
    monkeypatch.setenv("MINDIE_LIVE_A3_CONTAINER", "vaws-offload-pd165-override-20261020")
    observed_cmds: list[str] = []

    real_run = subprocess.run

    def run(argv, **kwargs):
        if argv and argv[0] == "ssh":
            observed_cmds.append(argv[-1])
            return subprocess.CompletedProcess(
                argv,
                0,
                "Total Count : 8\nChip Count : 8\n/usr/local/Ascend/cann-9.1.0\naarch64\n",
                "MINDIE_NPU_PROBE_CONNECTED\n",
            )
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    live_loop.test_live_a2_and_a3_npu_capture_redaction_store_and_reme_recall(tmp_path)
    assert len(observed_cmds) == 2
    assert any("mindie-bench-a2-override-20261020" in command for command in observed_cmds)
    assert any("vaws-offload-pd165-override-20261020" in command for command in observed_cmds)
