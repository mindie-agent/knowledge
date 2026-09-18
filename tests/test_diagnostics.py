"""Real owner boundaries; no backend process, downloads, or remote calls."""
import io
import json
import threading
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mindie_diagnostics import configure


def records(root):
    return [json.loads(line) for path in root.glob("events/*/*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def diagnostic_root(tmp_path, monkeypatch):
    root = tmp_path / "diagnostics"
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_ROOT", str(root))
    rec = configure("mindie-knowledge", root=root, level="DEBUG")
    yield root
    rec.close()


def test_tool_fault_retains_stack_and_server_remains_usable(diagnostic_root, monkeypatch):
    from mindie_knowledge.server.mcp_server import KnowledgeService
    service = KnowledgeService(config_mapping={"backend": "memory"}, env={})
    error = RuntimeError("backend unavailable")
    def unavailable(args):
        raise error
    monkeypatch.setattr(service, "knowledge_query", unavailable)
    result, failed = service.call_tool("knowledge_query", {"text": "ordinary words"})
    assert failed and result["error"] == "internal_error"
    monkeypatch.setattr(service, "knowledge_query", lambda args: {"answer": "unknown"})
    assert service.call_tool("knowledge_query", {"text": "ordinary words"}) == ({"answer": "unknown"}, False)
    end = [row for row in records(diagnostic_root) if row["event"] == "operation.end"]
    assert [row["status"] for row in end] == ["error", "success"]
    assert end[0]["attributes"]["exception_chain"] == ["RuntimeError"]
    assert end[0]["attributes"]["stack_frames"]


def test_optional_summary_keeps_stdout_and_records_failure(diagnostic_root, monkeypatch, capsys):
    from mindie_knowledge import summary_hook
    monkeypatch.setattr("sys.stdin", io.StringIO("{bad JSON"))
    assert summary_hook.main(["--client", "codex"]) == 0
    assert capsys.readouterr().out == "{}\n"
    end = records(diagnostic_root)[-1]
    assert end["status"] == "error"
    assert end["attributes"]["category"] == "summary_unavailable"


def test_maintenance_thread_records_real_final_write_failure(diagnostic_root, tmp_path, monkeypatch):
    from mindie_knowledge import maintenance
    from mindie_knowledge.server.layers import load_config
    config = load_config({"backend": "memory", "state_root": str(tmp_path / "state"),
                          "layers": {"shared": {"enabled": False}, "project": {"enabled": False},
                                     "candidate": {"enabled": False}},
                          "shared_sync": {"enabled": False}, "publishing": {"enabled": False}}, env={})
    attempted = threading.Event()
    def disk_full(path, payload):
        attempted.set()
        raise OSError("simulated storage full")
    monkeypatch.setattr(maintenance, "atomic_write_json", disk_full)
    worker = maintenance.MaintenanceWorker(config)
    worker.start()
    try:
        assert attempted.wait(5), "the actual maintenance function reached its final write"
        assert worker.thread.is_alive()
    finally:
        worker.stop()
    assert not worker.thread.is_alive()
    end = [row for row in records(diagnostic_root) if row.get("operation") == "knowledge.maintain"
           and row["event"] == "operation.end"][-1]
    assert end["status"] == "error"
    assert end["attributes"]["error_type"] == "OSError"


def test_backend_initialization_failure_keeps_original_exception(diagnostic_root, tmp_path, monkeypatch):
    from mindie_knowledge.local.instance import LocalInstance
    instance = LocalInstance(tmp_path / "state")
    monkeypatch.setattr(instance, "describe", lambda: {"live": False})
    monkeypatch.setattr(instance, "_credentials", lambda: {})
    original = OSError("model unavailable")
    def fail(*args, **kwargs):
        raise original
    monkeypatch.setattr(instance, "prepare_model", fail)
    with pytest.raises(OSError) as caught:
        instance.ensure()
    assert caught.value is original
    assert records(diagnostic_root)[-1]["status"] == "error"


def test_large_child_log_tail_is_bounded(tmp_path):
    from mindie_knowledge.local.instance import _log_tail
    path = tmp_path / "child.log"
    path.write_bytes(b"old line\n" * 200_000 + "last detail".encode())
    assert _log_tail(path, 80).endswith("last detail")
    assert len(_log_tail(path, 80)) <= 80


def test_logging_failure_never_gates_knowledge(tmp_path, capsys):
    from mindie_knowledge.observability import observed
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    rec = configure("mindie-knowledge", root=blocked)
    @observed("knowledge.local")
    def value():
        return {"answer": "unknown"}
    try:
        assert value() == {"answer": "unknown"}
        assert rec.logging_failed
        assert capsys.readouterr().out == ""
    finally:
        rec.close()


def test_daemon_uses_original_entrypoint_arguments(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from mindie_knowledge.local import daemon
    called = []
    entry = SimpleNamespace(group="console_scripts", name="openviking-server",
                            load=lambda: lambda: called.append(list(sys.argv)))
    monkeypatch.setattr(daemon.metadata, "distribution", lambda name: SimpleNamespace(entry_points=[entry]))
    monkeypatch.setattr(daemon, "capture_output", lambda op: nullcontext())
    previous = sys.argv
    assert daemon.main(["openviking", "--config", "owned-config"]) is None
    assert called == [["openviking-server", "--config", "owned-config"]]
    assert sys.argv is previous


@pytest.mark.parametrize("arguments, exit_code, classification", [
    (["unknown-command"], 2, "caller"),
    (["redact", "--allow", "re:(", "missing.md"], 2, "caller"),
    (["redact", "--check"], 2, "caller"),
])
def test_real_cli_argument_errors_stay_in_inherited_private_root(diagnostic_root, arguments, exit_code, classification):
    proc = subprocess.run([sys.executable, "-m", "mindie_knowledge", *arguments],
                          capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert proc.returncode == exit_code
    ended = [row for row in records(diagnostic_root) if row["event"] == "operation.end"]
    assert len(ended) == 1
    assert ended[0]["status"] == "error"
    assert ended[0]["attributes"]["classification"] == classification


def test_expected_redaction_findings_are_not_tool_failures(diagnostic_root, tmp_path):
    path = tmp_path / "note.md"
    path.write_text("# Fixture\n\n" + ".".join(map(str, (10, 43, 51, 19))), encoding="utf-8")
    proc = subprocess.run([sys.executable, "-m", "mindie_knowledge", "redact", "--check", str(path)],
                          capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert proc.returncode == 1
    events = records(diagnostic_root)
    assert any(row["event"] == "redaction.findings" for row in events)
    assert [row["status"] for row in events if row["event"] == "operation.end"] == ["success"]


def test_unknown_business_exit_two_is_not_inferred_caller(diagnostic_root):
    from mindie_knowledge.observability import observed
    @observed("knowledge.fixture.business")
    def business():
        return 2
    assert business() == 2
    ended = records(diagnostic_root)[-1]
    assert ended["status"] == "error"
    assert ended["attributes"]["exit_code"] == 2
    assert ended["attributes"].get("classification") != "caller"


@pytest.mark.parametrize("classification, error_code", [("caller", -32602), ("unknown", 503)])
def test_public_bundle_retains_failure_classification_and_numeric_code(
    diagnostic_root, tmp_path, classification, error_code
):
    from mindie_diagnostics import get_recorder

    with get_recorder("mindie-knowledge").operation("projection.fixture") as operation:
        operation.fail("argument_validation", classification=classification, error_code=error_code)
    output = tmp_path / "public-bundle.json"
    proc = subprocess.run(
        [sys.executable, "-m", "mindie_knowledge", "diagnostics", "bundle",
         "--root", str(diagnostic_root), "--operation-id", operation.summary()["operation_id"],
         "--output", str(output)],
        capture_output=True, text=True, encoding="utf-8", timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    bundle = json.loads(proc.stdout)
    assert json.loads(output.read_text(encoding="utf-8")) == bundle
    ended = [event for event in bundle["events"] if event["event"] == "operation.end"]
    assert len(ended) == 1
    assert ended[0]["severity"] == "ERROR"
    assert ended[0]["status"] == "error"
    assert ended[0]["attributes"]["category"] == "argument_validation"
    assert ended[0]["attributes"]["classification"] == classification
    assert ended[0]["attributes"]["error_code"] == error_code
