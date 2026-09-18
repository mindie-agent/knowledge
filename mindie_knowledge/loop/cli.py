"""MindIE domain service and its small MCP surface."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .engine import Engine
from .store import Store, canonical
from .transport import Service, rpc


def config_at(path):
    config = json.loads(Path(path).read_text())
    if not isinstance(config, dict) or not {"root", "domain", "agent_command"} <= set(
        config
    ):
        raise ValueError("configuration requires root, domain and agent_command")
    return config


def connection_path(config):
    return Path(config["root"]) / config["domain"] / "connection.json"


def connect(config):
    connection = json.loads(connection_path(config).read_text())
    if connection.get("domain") != config["domain"]:
        raise ValueError("connection is for another domain")
    return connection


def ensure_service(config_path):
    config = config_at(config_path)
    try:
        connection = connect(config)
        rpc(connection, "status", timeout=1)
        return connection
    except (OSError, ValueError):
        pass
    # Reuse the existing cross-platform nonblocking lock, not a stale PID file.
    from mindie_knowledge.distribution.errors import SwitchInProgress
    from mindie_knowledge.distribution.sync import SwitchLock

    lock = SwitchLock(connection_path(config).with_name("start.lock"))
    try:
        lock.acquire()
    except SwitchInProgress:
        for _ in range(50):
            time.sleep(0.1)
            try:
                connection = connect(config)
                rpc(connection, "status", timeout=1)
                return connection
            except (OSError, ValueError):
                pass
        raise RuntimeError("knowledge service startup is already in progress")
    try:
        try:
            connection = connect(config)
            rpc(connection, "status", timeout=1)
            return connection
        except (OSError, ValueError):
            pass
        kwargs = (
            {"start_new_session": True}
            if os.name != "nt"
            else {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
            }
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "mindie_knowledge.loop.cli",
                "serve",
                "--config",
                str(Path(config_path).resolve()),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
        for _ in range(80):
            if process.poll() is not None:
                raise RuntimeError(
                    "knowledge service exited during startup; run 'serve' in foreground for diagnostics"
                )
            try:
                connection = connect(config)
                rpc(connection, "status", timeout=1)
                return connection
            except (OSError, ValueError):
                time.sleep(0.1)
        process.terminate()
        raise RuntimeError("knowledge service did not become ready")
    finally:
        lock.release()


def schema(properties, required):
    return dict(
        type="object",
        properties=properties,
        required=required,
        additionalProperties=False,
    )


STRING = {"type": "string"}
TOOLS = [
    dict(
        name="knowledge_query",
        description="Search the selected domain's knowledge and experience. References are advisory.",
        inputSchema=schema(
            dict(
                query=STRING,
                session_id=STRING,
                limit={"type": "integer", "minimum": 1, "maximum": 20},
                conditions={"type": "object"},
            ),
            ["query", "session_id"],
        ),
    ),
    dict(
        name="knowledge_explain",
        description="Read the original content, source and conditions for one domain reference.",
        inputSchema=schema(dict(ref=STRING), ["ref"]),
    ),
    dict(
        name="knowledge_use",
        description="Record how an experience was actually used and the observed evidence. An independent judge evaluates after this task's Stop hook.",
        inputSchema=schema(
            dict(ref=STRING, session_id=STRING, application=STRING, evidence=STRING),
            ["ref", "session_id", "application", "evidence"],
        ),
    ),
]


def mcp(config_path):
    connection = ensure_service(config_path)
    for line in sys.stdin:
        message = {}
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("JSON-RPC object required")
            if "id" not in message:
                continue
            method = message.get("method")
            if method == "initialize":
                result = dict(
                    protocolVersion="2025-11-25",
                    capabilities={"tools": {}},
                    serverInfo={"name": "mindie-knowledge", "version": "0.1.0"},
                )
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = dict(tools=TOOLS)
            elif method == "tools/call":
                params = message.get("params", {})
                name = params.get("name")
                item = next((t for t in TOOLS if t["name"] == name), None)
                if not item:
                    raise ValueError("unknown tool")
                args = params.get("arguments", {})
                if (
                    not isinstance(args, dict)
                    or set(args) - set(item["inputSchema"]["properties"])
                    or set(item["inputSchema"]["required"]) - set(args)
                ):
                    raise ValueError("invalid tool arguments")
                try:
                    try:
                        payload = rpc(connection, name.removeprefix("knowledge_"), args)
                    except OSError:
                        # Owned service upgrades rotate its port/token. Reconnect
                        # once; the three operations are retry-safe by identity.
                        connection = ensure_service(config_path)
                        payload = rpc(connection, name.removeprefix("knowledge_"), args)
                    result = dict(
                        content=[dict(type="text", text=canonical(payload))],
                        structuredContent=payload,
                        isError=False,
                    )
                except (ValueError, OSError) as exc:
                    result = dict(
                        content=[
                            dict(
                                type="text",
                                text=f"Knowledge unavailable: {type(exc).__name__}. Continue the task independently.",
                            )
                        ],
                        isError=True,
                    )
            else:
                response = dict(
                    jsonrpc="2.0",
                    id=message["id"],
                    error=dict(code=-32601, message="Method not found"),
                )
                print(canonical(response), flush=True)
                continue
            response = dict(jsonrpc="2.0", id=message["id"], result=result)
        except (ValueError, KeyError, TypeError) as exc:
            response = dict(
                jsonrpc="2.0",
                id=message.get("id") if isinstance(message, dict) else None,
                error=dict(code=-32602, message=str(exc)[:400]),
            )
        print(canonical(response), flush=True)


def capture_hook(config_path, event):
    """Fail open: do not start the service, inspect transcripts, or queue offline."""
    try:
        if event.get("hook_event_name") != "Stop" or event.get("stop_hook_active"):
            return
        if not all(
            event.get(key)
            for key in ("session_id", "turn_id", "last_assistant_message")
        ):
            return
        rpc(
            connect(config_at(config_path)),
            "capture",
            dict(
                session_id=event["session_id"],
                turn_id=event["turn_id"],
                summary=event["last_assistant_message"],
            ),
            timeout=0.8,
        )
    except (OSError, ValueError, KeyError, TypeError):
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="MindIE single-domain knowledge loop")
    parser.add_argument(
        "operation",
        choices=[
            "start",
            "stop",
            "serve",
            "mcp",
            "hook",
            "status",
            "sync",
            "import",
            "publish",
            "snapshot",
        ],
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--file")
    parser.add_argument("--ref")
    args = parser.parse_args(argv)
    if args.operation == "hook":
        try:
            capture_hook(args.config, json.load(sys.stdin))
        except (ValueError, TypeError, AttributeError):
            pass
        print("{}")
        return 0
    config = config_at(args.config)
    if args.operation == "mcp":
        mcp(args.config)
        return 0
    if args.operation == "serve":
        store = Store(config["root"], config["domain"])
        engine = Engine(
            store,
            agent_command=config["agent_command"],
            auto_publish=config.get("auto_publish", False),
        )
        Service(
            engine,
            connection_path=connection_path(config),
            upstream=config.get("upstream"),
            feeds=config.get("feeds", []),
        ).serve()
        return 0
    if args.operation in {"import", "publish"}:
        store = Store(config["root"], config["domain"])
        try:
            if args.operation == "import":
                if not args.file:
                    parser.error("import requires --file with an entry JSON")
                result = store.add(**json.loads(Path(args.file).read_text()))
            else:
                if not args.ref:
                    parser.error("publish requires --ref")
                store.publish(args.ref)
                result = dict(published=True, ref=args.ref)
        finally:
            store.close()
    else:
        connection = (
            connect(config) if args.operation == "stop" else ensure_service(args.config)
        )
        result = rpc(
            connection,
            "status" if args.operation == "start" else args.operation,
            timeout=130 if args.operation == "sync" else 10,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"mindie-knowledge: {exc}", file=sys.stderr)
        raise SystemExit(2)
