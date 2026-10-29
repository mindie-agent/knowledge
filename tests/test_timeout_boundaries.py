"""Timeouts retain their execution classification across local I/O boundaries."""
from pathlib import Path

import pytest

from mindie_knowledge import _common, consent_store, redact
from mindie_knowledge.community import cli, settings
from mindie_knowledge.loop import service_context


@pytest.mark.parametrize("reader,owner,method,path", [
    (_common.load_document, Path, "read_text", Path("selected.yaml")),
    (redact.scan_file, Path, "read_text", Path("selected.md")),
    (cli._read_json_arg, Path, "read_bytes", "selected.json"),
    (settings.load_settings_file, consent_store, "_read_bytes", Path("community.json")),
])
def test_read_timeout_keeps_retry_and_execution_evidence(monkeypatch, reader, owner, method, path):
    error = TimeoutError("selected read exceeded its remaining budget")
    error.mindie_execution = "not_started"

    def timed_out(*args, **kwargs):
        raise error

    monkeypatch.setattr(owner, method, timed_out)
    with pytest.raises(TimeoutError) as raised:
        reader(path)
    assert raised.value is error
    assert raised.value.mindie_execution == "not_started"


@pytest.mark.parametrize("stage", ["read", "cleanup"])
def test_service_context_timeout_never_consumes_or_applies_transfer(tmp_path, monkeypatch, stage):
    config = {"service_launch_context": {"environment": ["MINDIE_TEST_CALLER_VALUE"]}}
    binding = {"attempt": "timeout-contract"}
    monkeypatch.setenv("MINDIE_TEST_CALLER_VALUE", "caller-value")
    path = tmp_path / "one-use.context"
    path.write_bytes(service_context.capture(config, binding))
    path.chmod(0o600)
    initial = path.read_bytes()
    error = TimeoutError("one-use transfer exceeded its remaining budget")

    def timed_out(*args, **kwargs):
        raise error

    with monkeypatch.context() as patch:
        if stage == "cleanup":
            patch.setattr(Path, "unlink", timed_out)
        elif service_context._windows():
            patch.setattr(Path, "read_bytes", timed_out)
        else:
            patch.setattr(service_context.os, "open", timed_out)
        with pytest.raises(TimeoutError) as raised:
            service_context.consume(path, config, binding)
    assert raised.value is error
    assert path.read_bytes() == initial
    assert service_context.consume(path, config, binding)["MINDIE_TEST_CALLER_VALUE"] == "caller-value"
    assert not path.exists()
