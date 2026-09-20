"""MindIE domain service CLI and its small MCP surface.

Bounded local commands: ``serve``, ``hook``, ``status``, ``stop``, ``mcp``,
``sync`` (model-free knowledge update, works with sharing off) and
``sharing-status`` (read-only). There are no authority/upstream/judge modes.

The MCP surface is exactly ``knowledge_query``, ``knowledge_explain`` and the
optional ``knowledge_feedback`` — no use/judging forms, no attach tool, no
capture tool. Each delivered ``tools/call`` is bound to the verified host
metadata in ``params._meta['x-codex-turn-metadata']`` (thread_id, session_id,
turn_id; ``_meta.threadId`` must agree). Missing or contradictory metadata is
a fail-closed diagnostic, never a latest-lease guess. Discovery
(``initialize``/``tools/list``) is static and starts nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import settings as settings_mod
from .engine import Engine
from .store import Store, canonical
from .transport import Service, rpc

READONLY = dict(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True,
    openWorldHint=False,
)
WRITE = dict(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True,
    openWorldHint=False,
)


def config_at(path):
    config = json.loads(Path(path).read_text())
    if not isinstance(config, dict) or not {"root", "domain"} <= set(config):
        raise ValueError("configuration requires root and domain")
    if "agent_command" in config and (
        not isinstance(config["agent_command"], list)
        or not all(isinstance(x, str) for x in config["agent_command"])
    ):
        raise ValueError("agent_command must be an argv list")
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
        for _attempt in range(MAX_STARTUP_PROBES):
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
        name="knowledge_query",
        description="Search the selected domain's knowledge and experience. References are advisory.",
        inputSchema=schema(
            dict(
                query=STRING,
                limit={"type": "integer", "minimum": 1, "maximum": 20},
                conditions={"type": "object", "description": "Optional known software versions or source commits; all other context belongs in the query."},
            ),
            ["query"],
        ),
        annotations=READONLY,
    ),
    dict(
        name="knowledge_explain",
        description=(
            "Read the original content, conditions and revision for one domain "
            "reference. offset and limit are character positions in the entry "
            "body, not lines; without them the full body is returned."
        ),
        inputSchema=schema(
            dict(
                ref=STRING,
                offset={"type": "integer", "minimum": 0,
                        "description": "Body character offset (0-based), not a line number."},
                limit={"type": "integer", "minimum": 1, "maximum": 65536,
                       "description": "Maximum body characters to return, not lines."},
            ),
            ["ref"],
        ),
        annotations=READONLY,
    ),
    dict(
        name="knowledge_feedback",
        description=(
            "Optionally record one up/down vote with an optional one-line reason "
            "for a reference you actually consulted. Never required; silence is "
            "not a signal."
        ),
        inputSchema=schema(
            dict(ref=STRING, rating={"type": "string", "enum": ["up", "down"]},
                 reason=STRING),
            ["ref", "rating"],
        ),
        annotations=WRITE,
    ),
]

IDENTITY_ERROR = (
    "knowledge tools require a host that delivers per-call task metadata "
    "(x-codex-turn-metadata with matching thread_id/session_id); this host "
    "did not, so the call is refused instead of guessing an identity"
)


def _bind_identity(params):
    """Verified per-call identity from host metadata; fail closed otherwise."""
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        raise ValueError(IDENTITY_ERROR)
    turn = meta.get("x-codex-turn-metadata")
    if not isinstance(turn, dict):
        raise ValueError(IDENTITY_ERROR)
    thread_id = turn.get("thread_id")
    session_id = turn.get("session_id")
    if (
        not isinstance(thread_id, str)
        or not thread_id
        or not isinstance(session_id, str)
        or not session_id
        or thread_id != session_id
        or meta.get("threadId") != thread_id
    ):
        raise ValueError(IDENTITY_ERROR)
    return session_id


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
                    serverInfo={"name": "mindie-knowledge", "version": "0.8.0"},
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
                    session = _bind_identity(params)
                    # Check the task's EXISTING lease before any service start:
                    # an unactivated caller must not create business state.
                    config = config_at(config_path)
                    if not config.get("session_activation"):
                        raise ValueError(
                            "no adapter admission is configured; identity unknown"
                        )
                    from .activation import Admission

                    if Admission(config["session_activation"]).active_lease(session) is None:
                        raise ValueError("session is not manually activated")
                    connection = ensure_service(config_path)
                    payload = rpc(
                        connection,
                        name.removeprefix("knowledge_"),
                        dict(args, _session_id=session, _session_verified=True),
                        timeout=5,
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
                                text=f"Knowledge unavailable: {exc}. Continue the task independently."[:500],
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
    """Bounded Stop bridge. Fail open for the user's task: no service start,
    no transcript access, no offline queue. Sharing off short-circuits before
    any capture state exists; read-only config/lease inspection is allowed."""
    try:
        if (
            not isinstance(event, dict)
            or event.get("hook_event_name") != "Stop"
            or event.get("stop_hook_active", False) is not False
        ):
            return
        fields = {}
        for key, limit in (
            ("session_id", 256),
            ("turn_id", 256),
            ("mindie_activation", 512),
            ("last_assistant_message", 32768),
            ("transcript_path", 4096),
            ("cwd", 4096),
        ):
            value = event.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or len(value) > limit:
                return
            fields[key] = value
        if "session_id" not in fields or "turn_id" not in fields:
            return
        config = config_at(config_path)
        settings = settings_mod.from_engine_config(config)
        if not settings.allows_capture():
            return  # community off: no capture row, cursor, draft, worker, model
        activation = fields.get("mindie_activation")
        if not activation or not config.get("session_activation"):
            return
        from .activation import Admission

        admission = Admission(config["session_activation"])
        try:
            lease = admission.capture_lease(fields["session_id"], activation)
        except ValueError:
            return
        scope = admission.scope_root(fields["session_id"])
        if not scope or not settings.in_scope(scope):
            return
        rpc(
            connect(config),
            "capture",
            dict(
                session_id=fields["session_id"],
                turn_id=fields["turn_id"],
                transcript_path=fields.get("transcript_path"),
                summary=fields.get("last_assistant_message", ""),
                cwd=fields.get("cwd"),
                _session_id=fields["session_id"],
                _activation=activation,
            ),
            timeout=0.8,
        )
    except (OSError, ValueError, KeyError, TypeError):
        pass


def _feeds(config, store):
    from .feed import Feed

    feeds = [Feed(store, item) for item in config.get("feeds", [])]
    settings = settings_mod.from_engine_config(config)
    if settings.repository and not any(
        f.repository == settings.repository and f.ref == settings.branch for f in feeds
    ):
        feeds.append(
            Feed(
                store,
                dict(repository=settings.repository, ref=settings.branch,
                     domain=store.domain),
            )
        )
    return feeds


def _open_existing_store(config):
    path = Path(config["root"]) / config["domain"] / "store-v3.sqlite3"
    if not path.is_file():
        return None
    return Store(config["root"], config["domain"])


def main(argv=None):
    parser = argparse.ArgumentParser(description="MindIE single-domain knowledge loop")
    parser.add_argument(
        "operation",
        choices=[
            "serve",
            "hook",
            "status",
            "stop",
            "mcp",
            "sync",
            "sharing-status",
            "maintenance-resume",
        ],
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--resume", action="store_true",
                        help="Explicitly resume deferred remote discovery and exhausted candidates (sync only)")
    args = parser.parse_args(argv)
    if args.resume and args.operation != "sync":
        parser.error("--resume applies only to sync")
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
    if args.operation == "mcp":
        mcp(args.config)
        return 0
    if args.operation == "serve":
        from .activation import Admission

        store = Store(config["root"], config["domain"])
        admission = (
            Admission(config["session_activation"])
            if config.get("session_activation")
            else None
        )
        engine = Engine(
            store,
            agent_command=config.get("agent_command"),
            settings_path=config.get("community_config"),
            admission=admission,
        )
        Service(
            engine,
            connection_path=connection_path(config),
            admission=admission,
            feeds=_feeds(config, store),
        ).serve()
        return 0
    if args.operation == "sync":
        # Standalone model-free knowledge update; never starts the service.
        store = Store(config["root"], config["domain"])
        try:
            result = [feed.sync(force=args.resume) for feed in _feeds(config, store)]
        finally:
            store.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.operation == "sharing-status":
        settings = settings_mod.from_engine_config(config)
        result = dict(settings.public_status())
        store = _open_existing_store(config)
        if store is None:
            result["store"] = "absent"
        else:
            try:
                summary = store.status()
                result.update(
                    store="present",
                    entries=summary["entries"],
                    captures=summary["captures"][:5],
                    coverage_gaps=summary["coverage_gaps"],
                    votes=summary["votes"],
                    outbox=summary["outbox"],
                )
            finally:
                store.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.operation == "maintenance-resume":
        store = _open_existing_store(config)
        if store is None:
            raise ValueError("no knowledge store exists for this domain")
        from .budget import MaintenanceBudget

        try:
            result = MaintenanceBudget(store).resume()
        finally:
            store.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.operation == "status":
        try:
            result = rpc(connect(config), "status", timeout=10)
        except (OSError, ValueError):
            result = dict(status="not-running")
            store = _open_existing_store(config)
            if store is not None:
                try:
                    result["store"] = store.status()
                finally:
                    store.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    # stop
    result = rpc(connect(config), "stop", timeout=10)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"mindie-knowledge: {exc}", file=sys.stderr)
        raise SystemExit(2)
