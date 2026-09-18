"""The MCP surface: framing, capability probing, and degradation.

The behaviour worth pinning down is the last one. The consuming workspace's
standing rule is that an absent fact means "unknown", never "supported", so a
degraded service must still be able to say "I could not look" in a way a
caller can act on. Returning an empty success would break that rule silently.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures" / "server"))

import support  # noqa: E402
from mindie_knowledge import package_version
from mindie_knowledge.local.reconcile import reconcile_markdown
from mindie_knowledge.server.layers import load_config  # noqa: E402
from mindie_knowledge.server.mcp_server import (  # noqa: E402
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    KnowledgeService,
    handle_message,
    read_message,
    serve,
    write_message,
)

REFERENCE_UUID = "7c2e9a10-4b3d-4f1a-8c6e-2a9b0d4e5f11"

TODAY = dt.date(2026, 9, 7)


def frame(*messages) -> bytes:
    out = io.BytesIO()
    for message in messages:
        write_message(out, message)
    return out.getvalue()


def read_all(raw: bytes) -> list[dict]:
    stream = io.BytesIO(raw)
    messages = []
    while True:
        message = read_message(stream)
        if message is None:
            return messages
        messages.append(message)


def service(**kwargs) -> KnowledgeService:
    return KnowledgeService(config=support.build_config(**kwargs), today=TODAY)


def call(svc, name, arguments=None, request_id=1):
    response = handle_message(
        svc,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )
    return response["result"]


class Framing(unittest.TestCase):
    def test_newline_jsonrpc_round_trip(self):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        raw = frame(payload)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"Content-Length:", raw)
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertEqual([payload], read_all(raw))

    def test_blank_lines_are_skipped(self):
        stream = io.BytesIO(b'\n\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n')
        self.assertEqual(2, read_message(stream)["id"])

    def test_eof_returns_none(self):
        self.assertIsNone(read_message(io.BytesIO(b"")))

    def test_malformed_json_is_a_parse_error_and_the_loop_survives(self):
        stdin = io.BytesIO(b"garbage without json\n" + frame({"jsonrpc": "2.0", "id": 3, "method": "ping"}))
        stdout = io.BytesIO()
        self.assertEqual(0, serve(stdin, stdout, service()))
        responses = read_all(stdout.getvalue())
        self.assertEqual(PARSE_ERROR, responses[0]["error"]["code"])
        self.assertEqual(3, responses[1]["id"])


class Handshake(unittest.TestCase):
    def test_initialize_advertises_the_package_version_and_layers(self):
        result = handle_message(service(), {"jsonrpc": "2.0", "id": 1, "method": "initialize"})["result"]
        self.assertEqual(package_version(), result["serverInfo"]["version"])
        info = result["serviceInfo"]
        self.assertEqual(["shared", "project", "candidate"], info["layers_available"])
        self.assertEqual(["candidate"], info["writable_layers"])
        self.assertEqual("unknown", info["degradation_contract"]["absent_fact_means"])
        self.assertEqual(["title", "content"], info["capture_required"])
        self.assertFalse(info["degraded"])
        self.assertIn("newline-delimited JSON-RPC", info["framing"])
        self.assertEqual(
            ["title", "content"],
            result["capabilities"]["experimental"]["mindie-knowledge"]["capture_required"],
        )

    def test_initialize_reports_absent_layers_with_reasons(self):
        info = handle_message(
            service(shared="missing"), {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        )["result"]["serviceInfo"]
        self.assertTrue(info["degraded"])
        self.assertIn("does not exist", info["layers_absent"]["shared"])

    def test_tools_list_exposes_exactly_the_three_tools(self):
        result = handle_message(service(), {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]
        self.assertEqual(
            ["knowledge_query", "knowledge_capture", "knowledge_explain"],
            [tool["name"] for tool in result["tools"]],
        )
        for tool in result["tools"]:
            self.assertFalse(tool["inputSchema"]["additionalProperties"])

    def test_tools_offer_only_task_inputs(self):
        result = handle_message(service(), {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]
        properties = {tool["name"]: set(tool["inputSchema"]["properties"]) for tool in result["tools"]}
        self.assertEqual({
            "knowledge_query": {"text", "limit"},
            "knowledge_capture": {"title", "content"},
            "knowledge_explain": {"ref"},
        }, properties)

    def test_notifications_get_no_response(self):
        self.assertIsNone(
            handle_message(service(), {"jsonrpc": "2.0", "method": "notifications/initialized"})
        )

    def test_unknown_method_is_a_jsonrpc_error_listing_what_is_supported(self):
        response = handle_message(service(), {"jsonrpc": "2.0", "id": 9, "method": "resources/list"})
        self.assertEqual(METHOD_NOT_FOUND, response["error"]["code"])
        self.assertIn("tools/call", response["error"]["data"]["supported"])


class Tools(unittest.TestCase):
    def test_capture_then_query_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = KnowledgeService(config=support.build_config(candidate=tmp), today=TODAY)
            captured = call(
                svc,
                "knowledge_capture",
                {
                    "title": "hostname resolution",
                    "content": "fresh containers lack their hostname in /etc/hosts",
                },
            )
            self.assertFalse(captured["isError"], captured)
            reconcile_markdown(svc.config)
            result = call(svc, "knowledge_query", {"text": "hostname resolution"})
            self.assertFalse(result["isError"])
            payload = result["structuredContent"]
            self.assertEqual(package_version(), payload["version"])
            self.assertEqual("unknown", payload["absent_fact_semantics"])
            self.assertTrue(payload["results"])
            self.assertEqual(payload, json.loads(result["content"][0]["text"]))

    def test_empty_result_set_is_answered_as_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = KnowledgeService(config=support.build_config(candidate=tmp), today=TODAY)
            result = call(svc, "knowledge_query", {"text": "zzz nonexistent symptom zzz"})
            payload = result["structuredContent"]
            self.assertEqual([], payload["results"])
            self.assertEqual("unknown", payload["answer"])
            self.assertIn("UNKNOWN", payload["answer_detail"])

    def test_explain_expands_one_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = KnowledgeService(config=support.build_config(candidate=tmp), today=TODAY)
            saved = call(
                svc,
                "knowledge_capture",
                {"title": "explain me", "content": "the full body"},
            )["structuredContent"]
            payload = call(svc, "knowledge_explain", {"ref": saved["uri"]})["structuredContent"]
            self.assertTrue(payload["found"])
            self.assertEqual("the full body", payload["content"])

    def test_explain_of_an_unknown_ref_is_unknown(self):
        result = call(service(), "knowledge_explain", {"ref": "missing-ref"})
        payload = result["structuredContent"]
        self.assertFalse(result["isError"])
        self.assertFalse(payload["found"])
        self.assertEqual("unknown", payload["answer"])

    def test_explain_without_a_ref_is_an_argument_error_not_a_crash(self):
        result = call(service(), "knowledge_explain", {})
        self.assertTrue(result["isError"])
        self.assertEqual("invalid_arguments", result["structuredContent"]["error"])

    def test_capture_rejects_layer_selection_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = KnowledgeService(config=support.build_config(candidate=tmp), today=TODAY)
            for layer in ("shared", "project", "verified"):
                with self.subTest(layer=layer):
                    result = call(
                        svc,
                        "knowledge_capture",
                        {"layer": layer, "title": "x", "content": "y"},
                    )
                    self.assertTrue(result["isError"])
                    payload = result["structuredContent"]
                    self.assertEqual("invalid_arguments", payload["error"])
                    self.assertIn("layer", payload["detail"])
            self.assertEqual([], list(pathlib.Path(tmp).iterdir()))

    def test_capture_saves_without_starting_the_index(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            svc = KnowledgeService(config=support.build_config(candidate=tmp))
            with patch("mindie_knowledge.server.capture.backend_for_config", side_effect=AssertionError("index started")):
                result = call(svc, "knowledge_capture", {"title": "Local observation", "content": "Recorded with uncertain cause."})
            self.assertFalse(result["isError"], result)
            payload = result["structuredContent"]
            self.assertTrue(pathlib.Path(payload["path"]).is_file())
            self.assertEqual("pending", payload["index"])
            pending = call(svc, "knowledge_query", {"text": "uncertain cause"})["structuredContent"]
            self.assertTrue(pending["results"], pending)
            self.assertEqual(["lexical"], pending["results"][0]["retrieval"])
            self.assertTrue(pending["degraded"])
            reconcile_markdown(svc.config)
            found = call(svc, "knowledge_query", {"text": "uncertain cause"})["structuredContent"]
            self.assertTrue(found["results"])

    def test_capture_rejection_reports_every_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = KnowledgeService(config=support.build_config(candidate=tmp), today=TODAY)
            result = call(svc, "knowledge_capture", {"title": "", "content": ""})
            self.assertTrue(result["isError"])
            payload = result["structuredContent"]
            self.assertEqual("capture_rejected", payload["error"])
            self.assertTrue(payload["problems"])

    def test_unknown_tool_is_an_error_result_not_a_dead_server(self):
        result = call(service(), "knowledge_invent")
        self.assertTrue(result["isError"])
        self.assertEqual("unknown_tool", result["structuredContent"]["error"])


class Degradation(unittest.TestCase):
    def test_index_failure_is_not_overwritten_by_healthy_mounts(self):
        from mindie_knowledge.local.backend import UnavailableBackend
        svc = service()
        svc.config.retrieval = UnavailableBackend("not ready")
        result = call(svc, "knowledge_query", {"text": "reference"})["structuredContent"]
        self.assertTrue(result["degraded"])
        self.assertTrue(result["unavailable"])
        self.assertIn("not ready", result["index_detail"])

    def test_no_layers_at_all_still_answers_unknown(self):
        svc = service(shared=False, project=False, candidate=False)
        payload = call(svc, "knowledge_query", {"text": "anything"})["structuredContent"]
        self.assertEqual([], payload["layers_available"])
        self.assertEqual("unknown", payload.get("answer") or "unknown")
        self.assertEqual("unknown", payload["degradation_contract"]["absent_fact_means"])

    def test_unreadable_configuration_degrades_instead_of_dying(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "mindie-knowledge.json"
            path.write_text("{not json")
            svc = KnowledgeService(config_path=path, env={}, today=TODAY)
            payload = call(svc, "knowledge_query", {"text": "anything"})["structuredContent"]
            self.assertIn("configuration_error", payload)
            self.assertEqual("unknown", payload["answer"])
            self.assertTrue(payload["degraded"])

    def test_serve_loop_handles_a_full_session_over_framed_stdio(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = KnowledgeService(config=support.build_config(candidate=tmp), today=TODAY)
            call(
                svc,
                "knowledge_capture",
                {"title": "loop note", "content": "framed stdio session"},
            )
            reconcile_markdown(svc.config)
            stdin = io.BytesIO(
                frame(
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "knowledge_query",
                            "arguments": {"text": "framed stdio session"},
                        },
                    },
                    {"jsonrpc": "2.0", "id": 4, "method": "shutdown"},
                )
            )
            stdout = io.BytesIO()
            self.assertEqual(0, serve(stdin, stdout, svc))
            responses = read_all(stdout.getvalue())
            self.assertEqual([1, 2, 3, 4], [r["id"] for r in responses])
            self.assertGreaterEqual(
                len(responses[2]["result"]["structuredContent"]["results"]), 1
            )


class StdioSubprocessHandshake(unittest.TestCase):
    """Actual stdio subprocess, not a direct handle_message call."""

    def test_initialize_tools_list_and_query_over_newline_stdio(self):
        import subprocess
        import time

        repo = pathlib.Path(__file__).resolve().parent.parent
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = {
            **os.environ,
            "PYTHONPATH": str(repo),
            "MINDIE_KNOWLEDGE_BACKEND": "memory",
            "MINDIE_KNOWLEDGE_CANDIDATE_ROOT": tmp.name,
            "MINDIE_KNOWLEDGE_STATE": str(pathlib.Path(tmp.name) / "instance"),
            "MINDIE_DIAGNOSTICS_ROOT": str(pathlib.Path(tmp.name) / "diagnostics"),
            "MINDIE_KNOWLEDGE_PROJECT_ROOTS": "",
            "MINDIE_KNOWLEDGE_SHARED_ROOTS": "",
        }
        env.pop("MINDIE_KNOWLEDGE_CORPUS", None)
        proc = subprocess.Popen(
            [sys.executable, "-m", "mindie_knowledge", "server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(repo),
            env=env,
        )
        assert proc.stdin is not None and proc.stdout is not None

        def send(message: dict) -> None:
            proc.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
            proc.stdin.flush()

        def recv() -> dict:
            line = proc.stdout.readline()
            self.assertTrue(line, proc.stderr.read() if proc.poll() is not None else "eof")
            self.assertFalse(line.startswith(b"Content-Length:"), line[:80])
            return json.loads(line)

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            init = recv()
            self.assertEqual(init["id"], 1)
            self.assertEqual(init["result"]["serverInfo"]["name"], "mindie-knowledge")
            self.assertIn("newline-delimited JSON-RPC", init["result"]["serviceInfo"]["framing"])
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            listed = recv()
            names = [tool["name"] for tool in listed["result"]["tools"]]
            self.assertEqual(names, ["knowledge_query", "knowledge_capture", "knowledge_explain"])
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "knowledge_capture",
                        "arguments": {
                            "title": "newline stdio",
                            "content": "newline-delimited JSON-RPC capture",
                        },
                    },
                }
            )
            captured = recv()
            self.assertFalse(captured["result"]["isError"], captured)
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "knowledge_query",
                        "arguments": {"text": "newline-delimited JSON-RPC"},
                    },
                }
            )
            queried = recv()
            payload = queried["result"]["structuredContent"]
            deadline = time.monotonic() + 5
            while not payload["results"] and time.monotonic() < deadline:
                time.sleep(0.05)
                send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                    "name": "knowledge_query", "arguments": {"text": "newline-delimited JSON-RPC"}}})
                queried = recv()
                payload = queried["result"]["structuredContent"]
            self.assertFalse(queried["result"]["isError"])
            self.assertTrue(payload["results"], payload)
            self.assertIn("newline-delimited JSON-RPC", payload["results"][0]["excerpt"])
            send({"jsonrpc": "2.0", "id": 5, "method": "shutdown"})
            self.assertEqual(recv()["id"], 5)
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        leftover = proc.stdout.read()
        self.assertFalse(leftover)
        self.assertNotIn(b"Traceback", proc.stderr.read())


if __name__ == "__main__":
    unittest.main()
