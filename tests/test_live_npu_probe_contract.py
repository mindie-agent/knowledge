"""The live probe may skip unavailable SSH, never connected-host failures."""
import subprocess

import pytest

from test_live_npu_knowledge_loop import _ssh_probe


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
