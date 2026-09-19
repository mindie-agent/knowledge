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


STARTUP_TIMEOUT = 5.0
MAX_STARTUP_PROBES = 3


def ensure_service(config_path):
    """One start attempt, at most three readiness probes, absolute startup budget."""
    config = config_at(config_path)
    deadline = time.monotonic() + STARTUP_TIMEOUT

    def probe():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("knowledge startup deadline exceeded")
        connection = connect(config)
        rpc(connection, "status", timeout=min(0.5, remaining))
        return connection

    try:
        return probe()
    except (OSError, ValueError):
        pass
    from .locks import StartInProgress, StartLock

    lock = StartLock(connection_path(config).with_name("start.lock"))
    acquired = False
    process = None
    ready = False
    try:
        try:
            lock.acquire()
            acquired = True
        except StartInProgress:
            pass
        if acquired:
            # A peer may have completed startup between our initial probe and lock.
            try:
                return probe()
            except (OSError, ValueError):
                pass
            spawn = dict(
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if os.name == "nt":
                spawn["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                spawn["start_new_session"] = True
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "mindie_knowledge.loop.cli",
                    "serve",
                    "--config",
                    str(Path(config_path).resolve()),
                ],
                **spawn,
            )
        for attempt in range(MAX_STARTUP_PROBES):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.5, remaining))
            if process is not None and process.poll() is not None:
                raise RuntimeError("knowledge service exited during startup; no retry")
            try:
                connection = probe()
                ready = True
                return connection
            except (OSError, ValueError):
                pass
        raise RuntimeError(
            "knowledge service unavailable after bounded readiness probes; no restart"
        )
    finally:
        if process is not None and not ready:
            from .process import terminate_tree

            terminate_tree(process)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        if acquired:
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
        name="knowledge_attach",
        description=(
            "Explicitly bind this task to the selected domain after manual "
            "activation. Binding never requires a knowledge query; it only "
            "makes this task's Stop summary eligible for bounded capture."
        ),
        inputSchema=schema(dict(session_id=STRING), ["session_id"]),
    ),
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
                    connection = ensure_service(config_path)
                    payload = rpc(
                        connection, name.removeprefix("knowledge_"), args, timeout=5
                    )
                    result = dict(
                        content=[dict(type="text", text=canonical(payload))],
                        structuredContent=payload,
                        isError=False,
                    )
                except (ValueError, OSError, RuntimeError) as exc:
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
        if (
            not isinstance(event, dict)
            or event.get("hook_event_name") != "Stop"
            or event.get("stop_hook_active", False) is not False
        ):
            return
        if any(
            not isinstance(event.get(key), str)
            or not event[key].strip()
            or len(event[key]) > limit
            for key, limit in (
                ("session_id", 256),
                ("turn_id", 256),
                ("last_assistant_message", 32768),
            )
        ):
            return
        rpc(
            connect(config_at(config_path)),
            "capture",
            dict(
                session_id=event["session_id"],
                turn_id=event["turn_id"],
                summary=event["last_assistant_message"],
                **(
                    {
                        "_session_id": event["session_id"],
                        "_activation": event["mindie_activation"],
                    }
                    if "mindie_activation" in event
                    else {}
                ),
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
            "attach",
            "status",
            "sync",
            "import",
            "publish",
            "withdraw",
            "snapshot",
            "maintenance-resume",
        ],
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--file")
    parser.add_argument("--ref")
    parser.add_argument("--session-id")
    parser.add_argument("--activation")
    args = parser.parse_args(argv)
    if args.operation == "hook":
        try:
            raw = sys.stdin.buffer.read(128 * 1024 + 1)
            if len(raw) <= 128 * 1024:
                capture_hook(args.config, json.loads(raw))
        except (ValueError, TypeError, AttributeError, OSError, RecursionError):
            pass
        print("{}")
        return 0
    config = config_at(args.config)
    if args.operation == "maintenance-resume":
        print(canonical(rpc(connect(config), "maintenance_resume")))
        return 0
    if args.operation == "attach":
        if not args.session_id:
            parser.error("attach requires --session-id")
        payload = dict(session_id=args.session_id)
        if args.activation:
            payload.update(_session_id=args.session_id, _activation=args.activation)
        print(canonical(rpc(connect(config), "attach", payload)))
        return 0
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
            session_activation=config.get("session_activation"),
        ).serve()
        return 0
    if args.operation in {"import", "publish", "withdraw"}:
        store = Store(config["root"], config["domain"])
        try:
            if args.operation == "import":
                if not args.file:
                    parser.error("import requires --file with an entry JSON")
                result = store.add(**json.loads(Path(args.file).read_text()))
            elif args.operation == "publish":
                if not args.ref:
                    parser.error("publish requires --ref")
                store.publish(args.ref)
                result = dict(published=True, ref=args.ref)
            else:
                if not args.ref:
                    parser.error("withdraw requires --ref")
                result = store.withdraw(args.ref)
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
