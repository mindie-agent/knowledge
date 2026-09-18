"""A connected but unused provider does no index or maintenance work."""
from __future__ import annotations

import io
import json
from unittest.mock import Mock

import pytest

from mindie_knowledge import maintenance
from mindie_knowledge.server import mcp_server as mcp
from mindie_knowledge.server.layers import load_config
from mindie_knowledge.server.query import QueryResponse


@pytest.fixture
def config(tmp_path):
    return load_config({"backend": "memory", "state_root": str(tmp_path / "state"),
                        "layers": {"shared": {"enabled": False}, "project": {"roots": []},
                                   "candidate": str(tmp_path / "candidate")},
                        "shared_sync": {"enabled": False}, "publishing": {"enabled": False}}, env={})


def run_main(monkeypatch, service, messages):
    stream = io.BytesIO(b"".join(json.dumps(message).encode() + b"\n" for message in messages))
    output = io.BytesIO()
    serve = mcp.serve
    monkeypatch.setattr(mcp, "KnowledgeService", lambda **kwargs: service)
    monkeypatch.setattr(mcp, "serve", lambda **kwargs: serve(stream, output, **kwargs))
    assert mcp.main([]) == 0
    return [json.loads(line) for line in output.getvalue().splitlines()]


def tool(name, args, ident=1):
    return {"jsonrpc": "2.0", "id": ident, "method": "tools/call",
            "params": {"name": name, "arguments": args}}


@pytest.mark.parametrize("messages", [[], [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    tool("unknown", {}), tool("knowledge_query", {}),
    tool("knowledge_query", {"text": "   "}),
    tool("knowledge_query", {"text": 1}),
    tool("knowledge_query", {"text": "valid", "limit": 0}),
    tool("knowledge_query", {"text": "valid", "limit": True}),
    tool("knowledge_query", {"text": "valid", "limit": "1"}),
    tool("knowledge_query", {"text": "valid", "unknown": 1}),
    tool("knowledge_capture", {"title": "", "content": ""}),
    tool("knowledge_capture", {"title": "valid", "content": 1}),
    tool("knowledge_explain", {"ref": "missing-local-reference"}),
]])
def test_unused_or_invalid_mcp_never_creates_worker_backend_or_network(config, monkeypatch, messages):
    service = mcp.KnowledgeService(config=config)
    factory = Mock(side_effect=AssertionError("worker created without knowledge use"))
    backend = Mock(side_effect=AssertionError("backend created without knowledge use"))
    network = Mock(side_effect=AssertionError("network used without knowledge use"))
    monkeypatch.setattr(maintenance, "MaintenanceWorker", factory)
    monkeypatch.setattr(maintenance, "backend_for_config", backend)
    monkeypatch.setattr("mindie_knowledge.local.backend.backend_for_config", backend)
    monkeypatch.setattr("socket.socket.connect", network)
    run_main(monkeypatch, service, messages)
    factory.assert_not_called()
    backend.assert_not_called()
    network.assert_not_called()
    assert service.maintenance is None
    assert not config.state_root.exists()


def test_valid_queries_activate_one_worker_and_eof_stops_only_that_worker(config, monkeypatch):
    service = mcp.KnowledgeService(config=config)
    worker = Mock()
    factory = Mock(return_value=worker)
    monkeypatch.setattr(maintenance, "MaintenanceWorker", factory)
    monkeypatch.setattr(mcp, "query", Mock(return_value=QueryResponse()))
    run_main(monkeypatch, service, [tool("knowledge_query", {"text": "one"}),
                                    tool("knowledge_query", {"text": "two"}, 2)])
    factory.assert_called_once_with(config)
    worker.start.assert_called_once()
    worker.stop.assert_called_once()
    worker.request.assert_not_called()


def test_capture_activates_after_local_save_and_requests_index_work(config, monkeypatch):
    service = mcp.KnowledgeService(config=config)
    worker = Mock()

    def after_save(actual):
        assert actual is config
        assert list(config.mount("candidate").roots[0].glob("*.md"))
        return worker

    factory = Mock(side_effect=after_save)
    monkeypatch.setattr(maintenance, "MaintenanceWorker", factory)
    responses = run_main(monkeypatch, service, [
        tool("knowledge_capture", {"title": "Observation", "content": "Uncertain cause; source retained."}),
        tool("knowledge_capture", {"title": "Another observation", "content": "Different evidence."}, 2),
    ])
    assert all(not response["result"]["isError"] for response in responses)
    factory.assert_called_once_with(config)
    worker.start.assert_called_once()
    assert worker.request.call_count == 2
    assert worker.mock_calls[0][0] == "request"
    worker.stop.assert_called_once()


def test_summary_capture_does_not_construct_maintenance(config, monkeypatch):
    from mindie_knowledge.summary_hook import capture_summary

    factory = Mock(side_effect=AssertionError("summary activated maintenance"))
    monkeypatch.setattr(maintenance, "MaintenanceWorker", factory)
    result = capture_summary({"hook_event_name": "Stop", "last_assistant_message":
                              "Existing final response contains useful uncertain observations."},
                             config=config, client="codex")
    assert result["status"] == "saved"
    factory.assert_not_called()
    assert not config.state_root.exists()
