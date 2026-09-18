"""Prepare retrieval explicitly or maintain it after knowledge is used.

This is package work, independent of public contribution permission. Markdown
and verified release packs are the recoverable sources; the index is derived.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from mindie_knowledge.distribution.errors import SwitchInProgress
from mindie_knowledge.distribution.manifest import atomic_write_json, read_json
from mindie_knowledge.distribution.sync import SwitchLock
from mindie_knowledge.local.backend import backend_for_config
from mindie_knowledge.local.instance import instance_for_config
from mindie_knowledge.local.reconcile import reconcile_markdown
from mindie_knowledge.markdown import SHARED_BOOTSTRAP_URI
from mindie_knowledge.server.layers import ServiceConfig, load_config
from mindie_knowledge.observability import observed, capture_failure

POLL_SECONDS = 10
CHECK_SECONDS = 60
VERIFY_SECONDS = 3600
HEALTH_SECONDS = 3600


def maintenance_status(config: ServiceConfig) -> dict[str, Any]:
    return read_json(instance_for_config(config).state_root / "maintenance.json") or {}


def _ledger_stamp(root: Path) -> list[int] | None:
    try:
        stat = (root / "markdown-index.json").stat()
        return [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
    except OSError:
        return None


def _shared_identity(current: dict[str, Any] | None) -> dict[str, Any] | None:
    return {key: current.get(key) for key in ("source_git_sha", "root_uri", "prepared_root", "prepared_manifest_sha256")} if current else None


def refresh_references(config: ServiceConfig, previous: dict[str, Any] | None = None, *, verify: bool = False) -> dict[str, Any]:
    """Join one active prepared release with local notes outside the query path."""
    from mindie_knowledge.catalog import catalog_path, refresh_catalog
    from mindie_knowledge.local.shared import current_shared

    before = previous or {}
    current = current_shared(config.state_root)
    unknown = current is None and bool(before.get("shared_identity") or (
        config.state_root and any((Path(config.state_root) / name / "current.json").exists()
                                  for name in ("distribution", "shared"))))
    identity = _shared_identity(current)
    path = catalog_path(config)
    extra = None
    if not unknown and (verify or path is None or not path.exists() or before.get("shared_identity") != identity):
        from mindie_knowledge.distribution.references import prepared_shared_documents

        extra = prepared_shared_documents(config.state_root, current=current) if current else []
    try:
        report = refresh_catalog(config, force=verify, extra_documents=extra)
    except Exception as exc:
        # Rollback of a failed prepared-source read preserves the previous
        # catalog. The next pass retries; absence is never fabricated.
        capture_failure(exc, "catalog_unavailable")
        report = {"status": "partial", "reason": f"{type(exc).__name__}: {exc}"[:1000]}
    if report.get("status") == "ready":
        report["shared_identity"] = identity
    if unknown:
        # A lost/corrupt pointer is not an empty release. Retain the last
        # catalog rows for recovery; query still filters by the active pointer.
        report.update(status="partial", reason="Active shared reference pointer is unavailable",
                      shared_identity=before.get("shared_identity"))
    return report


def _local_delta(uris: list[str]) -> list[str]:
    # Imported release resources have a separate distribution owner. Local
    # reconciliation must never delete/upsert that owner's vector namespace.
    prefixes = (SHARED_BOOTSTRAP_URI + "/", "viking://resources/project/", "viking://resources/candidate/")
    return [uri for uri in uris if isinstance(uri, str) and uri.startswith(prefixes)]


@observed("knowledge.maintain", level="DEBUG")
def maintain(config: ServiceConfig, *, verify: bool = False, force: bool = False) -> dict[str, Any]:
    """Run one bounded pass. Process ownership prevents concurrent repairs."""
    root = instance_for_config(config).state_root
    started = time.perf_counter()
    lock = SwitchLock(root / "maintenance.lock")
    try:
        lock.acquire()
    except SwitchInProgress:
        previous = maintenance_status(config)
        return {**previous, "status": "busy", "ready": bool(previous.get("ready"))}
    try:
        previous = maintenance_status(config)
        now = time.time()
        if (not force and not verify and now < previous.get("next_check", 0)
                and now < previous.get("next_verify", 0)):
            return previous
        audit = verify or now >= previous.get("next_verify", 0)
        result: dict[str, Any] = {
            "status": "pending", "ready": False, "checked_at": now,
            "next_check": now + CHECK_SECONDS,
            "next_verify": previous.get("next_verify", 0),
            "next_health": previous.get("next_health", 0),
        }
        # Lexical preparation remains useful when the vector service is offline.
        # Importers and independent agent work never run in this worker/lock.
        try:
            result["catalog"] = refresh_references(config, previous.get("catalog"), verify=audit)
        except Exception as exc:
            capture_failure(exc, "catalog_unavailable")
            result["catalog"] = {"status": "unavailable", "reason": f"{type(exc).__name__}: {exc}"[:1000]}
        if "health" in previous:
            result["health"] = previous["health"]
        if audit or now >= result["next_health"]:
            # Worklist generation is independent of vector readiness. Its
            # observations never become an admission or repair prerequisite.
            try:
                from mindie_knowledge.health import inspect_knowledge

                health = inspect_knowledge(config)
                result["health"] = {"status": health["status"], "findings": len(health.get("findings", [])),
                                    "snapshot": health.get("snapshot"), "reused": health.get("reused", False)}
                result["next_health"] = now + (60 if health["status"] == "busy" else HEALTH_SECONDS)
            except Exception as exc:
                capture_failure(exc, "health_unavailable")
                result["health"] = {"status": "unknown", "reason": f"{type(exc).__name__}: {exc}"[:1000]}
                result["next_health"] = now + 60
        try:
            backend = backend_for_config(config)
            if backend.name == "openviking":
                backend.instance.ensure(verify_model=audit)
            model_before = dict(backend.index_fingerprint())
            catalog = result["catalog"]
            snapshot = catalog.get("snapshot")
            reusable = bool(not audit and previous.get("local_ready")
                            and catalog.get("status") == "ready" and snapshot is not None
                            and previous.get("local_model") == model_before
                            and previous.get("local_ledger") is not None
                            and previous["local_ledger"] == _ledger_stamp(root))
            if reusable and previous.get("local_snapshot") == snapshot == catalog.get("previous_snapshot"):
                result["local"] = {"ok": True, "reused": True, "upserted": 0, "deleted": 0, "errors": []}
                result["local_ready"] = True
            else:
                delta = reusable and previous.get("local_snapshot") == catalog.get("previous_snapshot")
                kwargs = ({"changed_uris": _local_delta(catalog.get("changed_uris", [])),
                           "deleted_uris": _local_delta(catalog.get("deleted_uris", []))} if delta else {})
                report = reconcile_markdown(config, verify=audit, **kwargs)
                result["local"] = asdict(report)
                result["local_ready"] = not report.degraded
            if result["local_ready"]:
                result.update(local_snapshot=snapshot, local_model=model_before, local_ledger=_ledger_stamp(root))
            # Make local progress visible before a possibly offline release check.
            atomic_write_json(root / "maintenance.json", result)
            if result["local_ready"]:
                from mindie_knowledge.publishing import run_once

                shared = run_once(config, force=force and verify, verify=audit)
                result["shared"] = shared
                sync = shared.get("sync") or {}
                result["ready"] = sync.get("status", shared.get("status")) in {"unchanged", "switched", "disabled"}
                from mindie_knowledge.local.shared import current_shared

                # A legacy upgrade or repair can prepare a new reference source
                # while retaining the same Git version and returning unchanged.
                reference_changed = _shared_identity(current_shared(config.state_root)) != result["catalog"].get("shared_identity")
                if sync.get("status") == "switched" or reference_changed:
                    result["catalog"] = refresh_references(config, result["catalog"])
                    refreshed = result["catalog"]
                    if refreshed.get("status") == "ready" and refreshed.get("previous_snapshot") == result.get("local_snapshot"):
                        changed = _local_delta(refreshed.get("changed_uris", []))
                        deleted = _local_delta(refreshed.get("deleted_uris", []))
                        if changed or deleted:
                            # Local Markdown may have changed while the release
                            # transport ran. Do not bless its new snapshot without
                            # applying precisely those local changes too.
                            report = reconcile_markdown(config, changed_uris=changed, deleted_uris=deleted)
                            result["local"] = asdict(report)
                            result["local_ready"] = not report.degraded
                            result["ready"] = result["ready"] and not report.degraded
                        if result["local_ready"]:
                            result.update(local_snapshot=refreshed.get("snapshot"), local_ledger=_ledger_stamp(root))
                # A newly downloaded release can pin different model files.
                # Local vectors must agree before preparation reports ready.
                if model_before != dict(backend.index_fingerprint()):
                    report = reconcile_markdown(config, verify=True)
                    result["local"] = asdict(report)
                    result["local_ready"] = not report.degraded
                    result["ready"] = result["ready"] and not report.degraded
                    result.update(local_model=dict(backend.index_fingerprint()), local_ledger=_ledger_stamp(root))
                if audit and result["ready"]:
                    result["next_verify"] = now + VERIFY_SECONDS
        except Exception as exc:
            capture_failure(exc, "maintenance_unavailable")
            result["reason"] = f"{type(exc).__name__}: {exc}"[:1000]
        result["ready"] = result["ready"] and result["catalog"].get("status") == "ready"
        result["status"] = "ready" if result["ready"] else "pending"
        if not result["ready"]:
            result["next_check"] = now + 60
            result["next_verify"] = now + 60
        elapsed = time.perf_counter() - started
        result["elapsed_ms"] = round(elapsed * 1000, 3)
        # Larger libraries must not spend a large fraction of a laptop's time
        # rescanning. Captures can still wake an immediate incremental pass.
        result["next_check"] = max(result["next_check"], now + min(3600, elapsed * 20))
        atomic_write_json(root / "maintenance.json", result)
        return result
    finally:
        lock.release()


class MaintenanceWorker:
    """One quiet thread per active knowledge connection, sharing the owner lock."""

    def __init__(self, config: ServiceConfig):
        self.config = config
        self.closed = threading.Event()
        self.wakeup = threading.Event()
        self.thread: threading.Thread | None = None
        # Create the client holder before either thread uses it.
        backend_for_config(config)

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="knowledge-maintenance", daemon=True)
            self.thread.start()

    def _run(self) -> None:
        while not self.closed.is_set():
            changed = self.wakeup.is_set()
            self.wakeup.clear()
            try:
                # Reconnection is not evidence of index loss. Durable check
                # and audit deadlines decide whether existing work is reusable.
                maintain(self.config, force=changed)
            except Exception:
                # The observed maintenance boundary retained the failure. Retry
                # later without terminating or writing on the MCP stdout stream.
                pass
            self.wakeup.wait(POLL_SECONDS)

    def request(self) -> None:
        self.wakeup.set()

    def stop(self) -> None:
        self.closed.set()
        self.wakeup.set()
        if self.thread:
            self.thread.join(timeout=1)


def project_config(project: Path, path: Path | None = None) -> ServiceConfig:
    """Supply local defaults while preserving existing mounts and permissions."""
    project = project.expanduser().resolve()
    path = (path or project / ".mindie-local" / "knowledge" / "service.json").expanduser().resolve()
    payload = read_json(path) if path.exists() else {}
    if not isinstance(payload, dict):
        raise ValueError("existing knowledge configuration must be a JSON object")
    payload.setdefault("state_root", str(path.parent / "instance"))
    layers = payload.setdefault("layers", {})
    layers.setdefault("project", {"roots": [str(project / ".agents" / "knowledge")]})
    layers.setdefault("candidate", {"roots": [str(path.parent / "candidate")]})
    payload.setdefault("shared_sync", {"enabled": True})
    payload.setdefault("publishing", {"enabled": False})
    atomic_write_json(path, payload)
    return load_config(path=path)


@observed("knowledge.prepare")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare local knowledge models, sources and verified indexes")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    try:
        config = project_config(args.project, args.config)
        result = maintain(config, verify=True, force=True)
        result["config"] = str(config.config_path)
    except Exception as exc:
        capture_failure(exc, "prepare_unavailable")
        result = {"status": "pending", "ready": False, "reason": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ready") else 1
