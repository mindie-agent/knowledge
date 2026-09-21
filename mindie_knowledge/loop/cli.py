"""MindIE domain service CLI.

Bounded local commands: ``serve``, ``hook``, ``status``, ``stop``, ``sync``
(model-free knowledge update, works with sharing off), ``sharing-status``
(read-only) and the deterministic contribution recovery operations
``contribution-inspect`` / ``contribution-reconcile`` / ``contribution-retry``
/ ``contribution-compact`` (all require ``--batch``). There are no
authority/upstream/judge modes.

Native MCP dispatch belongs to the harness adapters; the former core MCP host
shim (bound to Codex-only turn metadata) is retired — core no longer
interprets any adapter's metadata format. Engine configuration uses
``admission_path`` (an explicit neutral admission SQLite file per harness
domain root) and ``transcript_adapter`` (an absolute local parser module
path); legacy adapter-config indirection is removed, not aliased.
"""

from __future__ import annotations

import argparse
import importlib.util
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


def config_at(path):
    from .diagnostics import _bytes

    return validate_config(json.loads(_bytes(path)))


def validate_config(config):
    if not isinstance(config, dict) or not {"root", "domain"} <= set(config):
        raise ValueError("configuration requires root and domain")
    if "session_activation" in config:
        raise ValueError(
            "session_activation was removed; configure the neutral "
            "admission_path SQLite file instead"
        )
    if "agent_command" in config and (
        not isinstance(config["agent_command"], list)
        or not all(isinstance(x, str) for x in config["agent_command"])
    ):
        raise ValueError("agent_command must be an argv list")
    if "admission_path" in config and not isinstance(config["admission_path"], str):
        raise ValueError("admission_path must be an explicit SQLite file path")
    adapter = config.get("transcript_adapter")
    if adapter is not None:
        candidate = Path(adapter)
        if (
            not isinstance(adapter, str)
            or not candidate.is_absolute()
            or candidate.suffix != ".py"
        ):
            raise ValueError(
                "transcript_adapter must be an absolute local parser module path"
            )
    return config


def load_transcript_adapter(config):
    """Load the configured trusted parser module exactly once. Missing or
    invalid configuration means honest summary-only behavior — core never
    guesses a native format."""
    adapter = (config or {}).get("transcript_adapter")
    if not adapter:
        return None
    path = Path(adapter)
    if not path.is_file():
        raise ValueError(f"transcript_adapter module is missing: {adapter}")
    spec = importlib.util.spec_from_file_location("mindie_transcript_adapter", path)
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: dataclasses and intra-module references resolve
    # the module through sys.modules during execution.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    for name in ("FileIdentity", "identify", "read_material"):
        if not hasattr(module, name):
            raise ValueError(
                f"transcript_adapter must export {name}; refusing to guess"
            )
    return module


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
        from .diagnostics import clear_startup_failure

        clear_startup_failure(config_path, config)
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
        if not activation or not config.get("admission_path"):
            return
        from .activation import Admission

        admission = Admission(config["admission_path"])
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


def contribution_recovery(config, operation, batch_id):
    """Deterministic, model-free recovery for one existing contribution.

    ``contribution-inspect`` is read-only (loop outbox row + community ledger
    receipts; starts nothing). ``contribution-reconcile`` runs the bounded
    explicit read-only inspection — verifying the exact saved expected PR
    head — and updates BOTH the loop outbox and the community ledger; it
    stays available after the automatic read budget is exhausted, and
    exhaustion or a lookup failure stays ``unknown``, never magically
    confirmed-failed. ``contribution-retry`` resubmits exactly one PROVEN
    failed stored payload with ``explicit_retry`` (never an unknown,
    rebuilt or mutated batch).
    ``contribution-compact`` removes the sent private payload of a confirmed
    batch. None of these reruns the organizer, resets a capture cursor or
    replays failed model attempts.
    """
    if operation == "contribution-inspect":
        result = {"batch_id": batch_id, "outbox": None, "ledger": []}
        store = _open_existing_store(config)
        if store is not None:
            try:
                row = store.batch(batch_id)
                if row is not None:
                    result["outbox"] = {
                        key: row[key]
                        for key in ("batch_id", "revision", "status", "detail",
                                    "pr_url", "head_sha", "attempted", "generation")
                    }
            finally:
                store.close()
        from mindie_knowledge.community.ledger import LEDGER_NAME

        ledger_path = Path(config["root"]) / config["domain"] / "outbox" / LEDGER_NAME
        if ledger_path.is_file():
            import sqlite3

            db = sqlite3.connect(ledger_path.as_uri() + "?mode=ro", uri=True)
            try:
                names = [r[1] for r in db.execute("PRAGMA table_info(publication)")]
                result["ledger"] = [
                    dict(zip(names, row))
                    for row in db.execute(
                        "SELECT * FROM publication WHERE batch_id=? ORDER BY created",
                        (batch_id,),
                    )
                ]
            finally:
                db.close()
        return result

    store = _open_existing_store(config)
    if store is None:
        raise ValueError("no knowledge store exists for this domain")
    state_dir = Path(config["root"]) / config["domain"] / "outbox"
    settings = settings_mod.from_engine_config(config)
    try:
        row = store.batch(batch_id)
        if row is None:
            raise ValueError("unknown contribution batch in this domain")
        if operation == "contribution-compact":
            removed = store.compact_confirmed(batch_id)
            if removed is None:
                raise ValueError(
                    "batch is not confirmed; unresolved/failed unsent work stays available"
                )
            return {"batch_id": batch_id, "compacted": removed}
        from mindie_knowledge.community import inspect_batch, submit_batch

        if operation == "contribution-reconcile":
            # Explicit bounded read-only inspection: verifies the exact saved
            # expected PR head and updates the community ledger; works on
            # cap-exhausted rows too. Unknown stays unknown.
            receipt = inspect_batch(batch_id, settings.as_dict(), state_dir)
        else:
            if row["status"] != "failed":
                raise ValueError(
                    "only a proven failed batch is retried explicitly; an "
                    "uncertain (unknown/unresolved) write is inspected with "
                    "contribution-reconcile, never replayed"
                )
            batch = json.loads(row["batch"])
            batch["explicit_retry"] = True
            receipt = submit_batch(batch, settings.as_dict(), state_dir)
        store.mark_batch(
            batch_id, receipt.get("status", "unknown"),
            detail=receipt.get("detail", ""), pr_url=receipt.get("pr_url"),
            head_sha=receipt.get("head_sha"),
        )
        if receipt.get("status") in Store.CONFIRMED_BATCH:
            store.compact_confirmed(batch_id)
        return receipt
    finally:
        store.close()


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


def _serve(config_path, config):
    """Record only failures before this owned service publishes readiness."""
    from .activation import Admission
    from .diagnostics import record_startup_failure

    stage = "store"
    store = service = engine = None
    try:
        store = Store(config["root"], config["domain"])
        stage = "admission"
        admission = Admission(config["admission_path"]) if config.get("admission_path") else None
        stage = "transcript_adapter"
        transcript = load_transcript_adapter(config)
        stage = "engine"
        engine = Engine(store, agent_command=config.get("agent_command"),
                        settings_path=config.get("community_config"), admission=admission,
                        transcript_adapter=transcript)
        stage = "service"
        service = Service(engine, connection_path=connection_path(config),
                          admission=admission, feeds=_feeds(config, store))
        service.serve()
        return 0
    except Exception as exc:
        published = False
        if service is not None:
            try:
                published = connect(config) == service.connection
            except (OSError, ValueError):
                pass
        if not published:
            try:
                record_startup_failure(config_path, config, stage, exc)
            except (OSError, ValueError):
                pass  # An unwritable state root cannot retain its own failure.
        raise
    finally:
        if engine is not None and (engine.thread.is_alive() or engine.outbox_thread.is_alive()):
            try:
                engine.shutdown()
            except RuntimeError:
                # A partial Engine.start can leave one thread unstarted;
                # shutdown already signals cancellation before joining it.
                pass
        if service is not None:
            service.http.server_close()
        if store is not None:
            store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="MindIE single-domain knowledge loop")
    parser.add_argument(
        "operation",
        choices=[
            "serve",
            "hook",
            "status",
            "diagnostic-status",
            "stop",
            "sync",
            "sharing-status",
            "maintenance-resume",
            "contribution-inspect",
            "contribution-reconcile",
            "contribution-retry",
            "contribution-compact",
        ],
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--session", help="Local diagnostic identity; never grants authorization")
    parser.add_argument("--batch",
                        help="Contribution batch id (contribution-* operations only)")
    parser.add_argument("--resume", action="store_true",
                        help="Explicitly resume deferred remote discovery and exhausted candidates (sync only)")
    args = parser.parse_args(argv)
    if args.session and args.operation != "diagnostic-status":
        parser.error("--session applies only to diagnostic-status")
    if args.resume and args.operation != "sync":
        parser.error("--resume applies only to sync")
    if args.batch and not args.operation.startswith("contribution-"):
        parser.error("--batch applies only to contribution-* operations")
    if args.operation == "hook":
        try:
            raw = sys.stdin.buffer.read(128 * 1024 + 1)
            if len(raw) <= 128 * 1024:
                capture_hook(args.config, json.loads(raw))
        except (ValueError, TypeError, AttributeError, OSError, RecursionError):
            pass
        print("{}")
        return 0
    if args.operation == "diagnostic-status":
        from .diagnostics import snapshot

        print(json.dumps(snapshot(args.config, session=args.session), ensure_ascii=False, indent=2))
        return 0
    config = config_at(args.config)
    if args.operation == "serve":
        return _serve(args.config, config)
    if args.operation.startswith("contribution-"):
        if not args.batch:
            parser.error(f"{args.operation} requires --batch")
        result = contribution_recovery(config, args.operation, args.batch)
        print(json.dumps(result, ensure_ascii=False, indent=2))
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
