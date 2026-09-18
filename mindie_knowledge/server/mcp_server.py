"""stdio JSON-RPC MCP server exposing the knowledge service.

Three tools:

    knowledge_query     search local reference material
    knowledge_capture   write one entry to the candidate layer (only)
    knowledge_explain   read one Markdown document by ref

Framing is newline-delimited JSON-RPC on stdio, matching the MCP stdio
transport. Messages MUST NOT contain embedded newlines. The official MCP SDK
is not required for transport; indexing uses the package's OpenViking backend.
If the SDK is importable we report it in ``initialize``. Nothing but JSON-RPC
messages is written to stdout.

Degradation contract
--------------------
The consuming workspace has a standing rule: **an absent fact means
"unknown", never "supported"**. That has to survive partial availability, so:

* every response carries `version` (the installed package version),
  `layers_available`, `layers_absent` (with reasons) and `degraded`;
* every response carries `absent_fact_semantics: "unknown"`;
* an empty result set is returned as an explicit "unknown" reading, not as an
  empty success;
* a layer that fails to load is reported in `load.errors` rather than being
  silently skipped, so a caller can tell "nothing matched" from "we could not
  look".
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from mindie_knowledge import package_version
from mindie_knowledge.observability import observed, capture_failure

from .capture import CaptureRefused, CaptureRejected, capture
from .layers import (
    ENV_CORPUS,
    LAYERS,
    ConfigError,
    ServiceConfig,
    load_config,
    shared_source,
)
from .query import explain, query

SERVER_NAME = "mindie-knowledge"
MCP_PROTOCOL_VERSION = "2025-11-25"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

DEGRADATION_CONTRACT = {
    "absent_fact_means": "unknown",
    "detail": (
        "A missing entry is never evidence that something works. When a layer is "
        "absent or fails to load, responses say so; treat any gap as unexamined."
    ),
}


class FramingError(Exception):
    """A message could not be read off the wire."""


# --------------------------------------------------------------------------
# framing — newline-delimited JSON-RPC (MCP stdio)
# --------------------------------------------------------------------------


def write_message(stream: BinaryIO, payload: Mapping[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if b"\n" in body:
        raise FramingError("MCP stdio message must not contain an embedded newline")
    stream.write(body + b"\n")
    stream.flush()


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    """Read one newline-delimited JSON-RPC message. Returns None at EOF."""

    while True:
        line = stream.readline()
        if not line:
            return None
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FramingError(f"invalid JSON line: {exc}") from exc
        if not isinstance(payload, dict):
            raise FramingError("JSON-RPC message must be an object")
        return payload


# --------------------------------------------------------------------------
# tool schemas
# --------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "knowledge_query",
        "description": (
            "Search reference notes by free text. Results retain known conditions "
            "and uncertainty; assess them against current evidence. Lookup is optional."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["text"],
            "properties": {
                "text": {"type": "string", "description": "Question or symptom, with useful context."},
                "limit": {"type": "integer", "default": 8, "minimum": 1},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "knowledge_capture",
        "description": (
            "Save a local Markdown note; the same title updates its body. Reuse an existing "
            "summary when useful; no template or separate report is required. "
            "Keep known conditions, sources and uncertainty in the prose. "
            "Sharing follows the user's existing publishing configuration."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["title", "content"],
            "properties": {"title": {"type": "string"}, "content": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "knowledge_explain",
        "description": "Read the original Markdown and recorded context for a search result.",
        "inputSchema": {
            "type": "object",
            "required": ["ref"],
            "properties": {"ref": {"type": "string"}},
            "additionalProperties": False,
        },
    },
]


def _sdk_available() -> bool:
    try:  # pragma: no cover - depends on the host environment
        import mcp  # type: ignore  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


# --------------------------------------------------------------------------
# service
# --------------------------------------------------------------------------


class KnowledgeService:
    """Tool implementations plus the degradation envelope."""

    def __init__(
        self,
        config: ServiceConfig | None = None,
        *,
        config_path: str | Path | None = None,
        config_mapping: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
        today: _dt.date | None = None,
    ):
        self.today = today
        self.maintenance = None
        # The stdio lifecycle supplies this factory without creating a backend.
        # Direct library use continues to leave maintenance with its caller.
        self.maintenance_factory = None
        self.config_error: str | None = None
        if config is not None:
            self.config = config
        else:
            try:
                self.config = load_config(config_mapping, path=config_path, env=env)
            except ConfigError as exc:
                # Unreadable config is not a reason to die: come up with no
                # layers and say plainly that everything is unknown.
                self.config_error = str(exc)
                self.config = ServiceConfig(mounts={}, warnings=[f"configuration error: {exc}"])

    # -- envelope ---------------------------------------------------------

    def envelope(self) -> dict[str, Any]:
        consulted = self.config.consulted()
        env: dict[str, Any] = {
            "version": package_version(),
            "layers_available": consulted["layers_available"],
            "layers_absent": consulted["layers_absent"],
            "degraded": consulted["degraded"],
            "absent_fact_semantics": "unknown",
            "degradation_contract": DEGRADATION_CONTRACT,
        }
        env.update(shared_source())
        if self.config_error:
            env["configuration_error"] = self.config_error
        return env

    def with_environment(self, payload: dict[str, Any]) -> dict[str, Any]:
        environment = self.envelope()
        degraded = bool(environment["degraded"] or payload.get("degraded"))
        return {**environment, **payload, "degraded": degraded}

    def server_info(self) -> dict[str, Any]:
        info = self.config.describe()
        info.update(
            {
                "server": {"name": SERVER_NAME, "version": package_version()},
                "tools": [tool["name"] for tool in TOOLS],
                "degradation_contract": DEGRADATION_CONTRACT,
                "official_mcp_sdk_importable": _sdk_available(),
                "framing": "newline-delimited JSON-RPC (MCP stdio; SDK not required)",
                "writable_layers": ["candidate"],
                "capture_required": ["title", "content"],
            }
        )
        return info

    # -- tools ------------------------------------------------------------

    def activate_maintenance(self, *, changed: bool = False) -> None:
        if self.maintenance is None:
            if self.maintenance_factory is None:
                return
            self.maintenance = self.maintenance_factory(self.config)
            # Set the wakeup before starting a new worker so a capture is not
            # delayed by a fresh next_check or raced by the initial pass.
            if changed:
                self.maintenance.request()
            self.maintenance.start()
        elif changed:
            self.maintenance.request()

    def knowledge_query(self, args: Mapping[str, Any]) -> dict[str, Any]:
        text = str(args.get("text") or "").strip()
        if not text:
            raise ValueError("text is required")
        limit = int(args.get("limit", 8))
        if limit < 1:
            raise ValueError("limit must be positive")
        self.activate_maintenance()
        response = query(
            self.config,
            text=text,
            limit=limit,
        )
        payload = response.to_dict()
        payload = self.with_environment(payload)
        if payload.get("unavailable"):
            payload["answer"] = "unknown"
            payload["answer_detail"] = payload["no_result_meaning"]
        elif not payload["results"]:
            payload["answer"] = "unknown"
            payload["answer_detail"] = payload["no_result_meaning"]
        return payload

    def knowledge_explain(self, args: Mapping[str, Any]) -> dict[str, Any]:
        ident = str(args.get("ref") or "").strip()
        if not ident:
            raise ValueError("ref is required")
        payload = explain(
            self.config,
            ident,
        )
        payload = self.with_environment(payload)
        if not payload.get("found"):
            payload["answer"] = "unknown"
        return payload

    def knowledge_capture(self, args: Mapping[str, Any]) -> dict[str, Any]:
        payload = capture(
            title=str(args.get("title") or ""),
            content=str(args.get("content") or ""),
            config=self.config,
            index=False,
        )
        payload = self.with_environment(payload)
        self.activate_maintenance(changed=True)
        return payload

    @observed("knowledge.tool")
    def call_tool(self, name: str, args: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        """Returns ``(payload, is_error)``. Refusals are results, not crashes."""

        handlers = {
            "knowledge_query": self.knowledge_query,
            "knowledge_capture": self.knowledge_capture,
            "knowledge_explain": self.knowledge_explain,
        }
        handler = handlers.get(name)
        if handler is None:
            return (
                {
                    "ok": False,
                    "error": "unknown_tool",
                    "detail": f"no such tool: {name}",
                    "available_tools": list(handlers),
                    **self.envelope(),
                },
                True,
            )
        try:
            schema = next(tool["inputSchema"] for tool in TOOLS if tool["name"] == name)
            unknown = set(args) - set(schema["properties"])
            if unknown:
                raise ValueError(f"unsupported arguments: {', '.join(sorted(unknown))}")
            for key, value in args.items():
                kind = schema["properties"][key]["type"]
                if ((kind == "string" and not isinstance(value, str))
                        or (kind == "integer" and type(value) is not int)):
                    raise ValueError(f"{key} must be {kind}")
            return handler(args), False
        except CaptureRefused as exc:
            return (
                {
                    "ok": False,
                    "error": "capture_refused",
                    "refused_layer": exc.layer,
                    "detail": str(exc),
                    **self.envelope(),
                },
                True,
            )
        except CaptureRejected as exc:
            return (
                {
                    "ok": False,
                    "error": "capture_rejected",
                    "problems": exc.problems,
                    "detail": str(exc),
                    **self.envelope(),
                },
                True,
            )
        except ValueError as exc:
            return (
                {"ok": False, "error": "invalid_arguments", "detail": str(exc), **self.envelope()},
                True,
            )
        except Exception as exc:  # noqa: BLE001 - a tool fault is not a dead server
            capture_failure(exc)
            return (
                {
                    "ok": False,
                    "error": "internal_error",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "answer": "unknown",
                    **self.envelope(),
                },
                True,
            )


# --------------------------------------------------------------------------
# JSON-RPC
# --------------------------------------------------------------------------


def _result(request_id: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": err}


@observed("knowledge.mcp.request", level="DEBUG")
def handle_message(service: KnowledgeService, message: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(message, Mapping) or "method" not in message:
        return _error(message.get("id") if isinstance(message, Mapping) else None,
                      INVALID_REQUEST, "not a JSON-RPC request")

    method = str(message.get("method"))
    request_id = message.get("id")
    is_notification = "id" not in message
    params = message.get("params") or {}
    if not isinstance(params, Mapping):
        return None if is_notification else _error(request_id, INVALID_PARAMS, "params must be an object")

    if method == "initialize":
        return None if is_notification else _result(
            request_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "experimental": {
                        "mindie-knowledge": {
                            "version": package_version(),
                            "capture_required": ["title", "content"],
                        },
                    },
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": package_version(),
                },
                "serviceInfo": service.server_info(),
            },
        )

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return None if is_notification else _result(request_id, {"version": package_version()})

    if method == "shutdown":
        return None if is_notification else _result(request_id, {})

    if method == "tools/list":
        return None if is_notification else _result(
            request_id, {"tools": TOOLS, "version": package_version()}
        )

    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        if not isinstance(args, Mapping):
            return None if is_notification else _error(
                request_id, INVALID_PARAMS, "arguments must be an object"
            )
        payload, is_error = service.call_tool(name, args)
        if is_notification:
            return None
        return _result(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                "structuredContent": payload,
                "isError": is_error,
            },
        )

    if is_notification:
        return None
    return _error(
        request_id,
        METHOD_NOT_FOUND,
        f"unsupported method: {method}",
        {"supported": ["initialize", "tools/list", "tools/call", "ping", "shutdown"]},
    )


@observed("knowledge.mcp")
def serve(
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    service: KnowledgeService | None = None,
) -> int:
    reader = stdin or sys.stdin.buffer
    writer = stdout or sys.stdout.buffer
    service = service or KnowledgeService()

    while True:
        try:
            message = read_message(reader)
        except FramingError as exc:
            write_message(writer, _error(None, PARSE_ERROR, f"framing error: {exc}"))
            continue
        if message is None:
            return 0
        try:
            response = handle_message(service, message)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            capture_failure(exc, "protocol_handler_failed")
            response = _error(
                message.get("id") if isinstance(message, Mapping) else None,
                INTERNAL_ERROR,
                f"{type(exc).__name__}: {exc}",
            )
        if response is not None:
            write_message(writer, response)


@observed("knowledge.server")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mindie-knowledge server",
        description="stdio MCP server over a local mindie-knowledge corpus checkout",
    )
    parser.add_argument(
        "--corpus",
        help="shared Markdown directory or a checkout containing corpus/",
    )
    parser.add_argument("--config", help="path to a JSON/YAML service config")
    parser.add_argument(
        "--describe",
        action="store_true",
        help="print resolved mounts as JSON and exit (no server loop)",
    )
    args = parser.parse_args(argv)

    env = dict(os.environ)
    if args.corpus:
        env[ENV_CORPUS] = args.corpus

    try:
        service = KnowledgeService(config_path=args.config, env=env)
    except ConfigError as exc:  # pragma: no cover - KnowledgeService absorbs these
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if args.describe:
        print(json.dumps(service.server_info(), indent=2, ensure_ascii=False))
        return 0

    if service.config_error:
        return serve(service=service)

    from mindie_knowledge.maintenance import MaintenanceWorker

    service.maintenance_factory = MaintenanceWorker
    try:
        return serve(service=service)
    finally:
        if service.maintenance is not None:
            service.maintenance.stop()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
