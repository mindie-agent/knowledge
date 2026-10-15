"""One on-demand knowledge core owned by the current user's systemd manager.

The unit owns crash cleanup of the whole service cgroup. Local receipts only
bind one launch attempt; readiness and consumption still use the existing
authenticated endpoint and OS lock.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

from ..filesystem import path_identity, resolved_path
from . import service_context
from .service_host import ServiceHostError, LIMIT, _read, _write, _result_path, read_result, run_owned

MARKER = "mindie-knowledge.systemd-service/1"
_LIVE = {"active", "activating", "deactivating", "reloading", "refreshing"}
_PROPERTIES = {
    "Type": "exec", "ExitType": "main", "KillMode": "control-group",
    "Restart": "no", "RemainAfterExit": "no",
}
_FIELDS = ("Id", "Description", "LoadState", "ActiveState", "SubState", "ControlGroup",
           "Transient", "ExecMainStatus", "Result", "MainPID", *_PROPERTIES)


def _manager(command, *, stage):
    from .process import bounded_run
    try:
        return bounded_run(command, "", timeout=30, max_output=LIMIT).strip()
    except Exception as exc:
        raise ServiceHostError(
            "manager_operation_failed", stage=stage,
            uncertain=stage in {"start", "remove"}
            and getattr(exc, "mindie_execution", None) != "not_started",
        ) from None


def _properties(raw):
    values = {}
    for line in raw.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in values:
            raise ServiceHostError("manager_status_invalid", stage="query")
        values[key] = value
    return values


def _supported():
    values = _properties(_manager(
        ["systemctl", "--user", "show", "--property=Version"], stage="query"))
    version = re.match(r"([0-9]+)(?:[.\s-]|$)", values.get("Version", ""))
    if version is None or int(version[1]) < 254:
        raise ServiceHostError("systemd_version_unsupported", stage="query")


def _private(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return _verify_private(root)


def _verify_private(root):
    info = root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ServiceHostError("unowned_directory", stage="ownership")
    return os.getuid()


def _paths(config):
    return resolved_path(Path(config["root"]) / config["domain"] / ".systemd-service")


def _unit(label):
    value = _properties(_manager(
        ["systemctl", "--user", "show", label, "--property=" + ",".join(_FIELDS)],
        stage="query"))
    if value.get("LoadState") == "not-found" and value.get("ActiveState") == "inactive":
        return None
    if (value.get("LoadState") != "loaded"
            or value.get("ActiveState") not in _LIVE | {"inactive", "failed"}
            or not set(_FIELDS) <= value.keys()):
        raise ServiceHostError("manager_status_invalid", stage="query")
    try:
        raw = json.loads(_manager([
            "busctl", "--user", "--json=short", "call", "org.freedesktop.systemd1",
            "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
            "GetUnit", "s", label,
        ], stage="query"))
        if (raw.get("type") != "o" or len(raw["data"]) != 1
                or not re.fullmatch(r"/org/freedesktop/systemd1/unit/[A-Za-z0-9_]+", raw["data"][0])):
            raise ValueError()
        action = json.loads(_manager([
            "busctl", "--user", "--json=short", "get-property", "org.freedesktop.systemd1",
            raw["data"][0], "org.freedesktop.systemd1.Service", "ExecStart",
        ], stage="query"))
        if (action.get("type") != "a(sasbttttuii)" or len(action["data"]) != 1
                or len(action["data"][0]) != 10):
            raise ValueError()
        value["exec"] = action["data"][0][:3]
        return value
    except (ValueError, TypeError, KeyError):
        raise ServiceHostError("manager_status_invalid", stage="query") from None


def _description(manifest):
    return MARKER + ":" + hashlib.sha256(str(manifest).encode()).hexdigest()


def _verify_unit(unit, record, manifest):
    if unit is None:
        return
    expected = dict(_PROPERTIES, Id=record["label"], Transient="yes",
                    Description=_description(manifest))
    if (any(unit.get(key) != value for key, value in expected.items())
            or unit.get("exec") != [record["python"], [record["python"], *record["args"]], False]):
        raise ServiceHostError("foreign_task", stage="ownership")


def _record(config_path, config, root, uid):
    from .lifecycle import config_fingerprint
    python = Path(sys.executable).absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ServiceHostError("interpreter_path_unsupported", stage="interpreter")
    attempt = uuid.uuid4().hex
    owner = hashlib.sha256((str(uid) + "\n" + path_identity(root.parent)).encode()).hexdigest()[:16]
    return dict(
        schema=MARKER, uid=uid, attempt=attempt,
        label="mindie-knowledge-" + owner + "-" + attempt + ".service",
        config=str(Path(config_path).resolve()), cwd=str(Path.cwd().resolve()),
        fingerprint=config_fingerprint(config_path, config), python=str(python),
        args=["-I", "-m", "mindie_knowledge.loop.systemd_service",
              "--manifest", str(root / "launch.json"), "--attempt", attempt],
    )


def _binding(record):
    return {key: record[key] for key in ("uid", "attempt", "fingerprint", "cwd")}


def _validate_process(record):
    from .service_host import _mark_process_contained
    unit = _unit(record["label"])
    _verify_unit(unit, record, Path(record["args"][-3]))
    groups = Path("/proc/self/cgroup").read_text().splitlines()
    if (unit is None or unit.get("MainPID") != str(os.getpid())
            or groups != ["0::" + unit.get("ControlGroup", "")]):
        raise ServiceHostError("service_containment_unverified", stage="ownership")
    _mark_process_contained()


def _validate_record(record, config_path, config, uid):
    from .lifecycle import config_fingerprint
    if (not isinstance(record, dict) or record.get("schema") != MARKER
            or record.get("uid") != uid
            or record.get("fingerprint") != config_fingerprint(config_path, config)
            or record.get("config") != str(Path(config_path).resolve())
            or not isinstance(record.get("cwd"), str) or not Path(record["cwd"]).is_absolute()
            or not isinstance(record.get("attempt"), str) or not isinstance(record.get("label"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", record.get("attempt", ""))
            or not re.fullmatch(r"mindie-knowledge-[a-f0-9]{16}-" + record["attempt"] + r"\.service",
                                record.get("label", ""))):
        raise ServiceHostError("foreign_launch_record", stage="ownership")


class UserService:
    externally_owned = True
    pid = None

    def __init__(self, root, record):
        self.root, self.record = root, record

    def poll(self):
        result = read_result(self.root, self.record)
        if result and result.get("status") == "failed":
            raise ServiceHostError(result.get("code"), stage=result.get("stage"),
                                   startup=result.get("startup"))
        if result and result.get("status") == "finished":
            return result.get("exit_code", 1)
        unit = _unit(self.record["label"])
        _verify_unit(unit, self.record, self.root / "launch.json")
        if unit and unit["ActiveState"] in _LIVE:
            return None
        if unit is not None and unit["ActiveState"] == "failed":
            raise ServiceHostError("service_action_exited", stage="readiness")
        raise ServiceHostError("start_outcome_unknown", stage="readiness", uncertain=True)


def request_start(config_path, config):
    from .diagnostics import record_startup_failure
    try:
        return _request_start(config_path, config)
    except (OSError, ValueError, RuntimeError) as exc:
        try:
            record_startup_failure(config_path, config, "service_host", exc)
        except (OSError, ValueError):
            pass
        raise


def _request_start(config_path, config):
    """The existing domain start lock serializes launch and exact retirement."""
    from .lifecycle import require_active
    require_active(config_path, config)
    service_context.validate(config)
    _supported()
    root = _paths(config)
    uid = _private(root)
    manifest = root / "launch.json"
    existing = _read(manifest)
    if existing:
        _validate_record(existing, config_path, config, uid)
        unit = _unit(existing["label"])
        _verify_unit(unit, existing, manifest)
        operation = _read(root / "operation.json")
        result = read_result(root, existing)
        if not operation or operation.get("attempt") != existing["attempt"]:
            raise ServiceHostError("launch_operation_invalid", stage="reconcile", uncertain=True)
        if unit and unit["ActiveState"] in _LIVE:
            return UserService(root, existing)
        phase = operation.get("phase")
        if phase in {"starting", "started"}:
            terminal = result and result.get("status") in {"failed", "finished"}
            # An inactive unit can still have a queued Start job. Its mere
            # existence/absence does not prove the accepted attempt finished.
            if not terminal and not (unit and unit["ActiveState"] == "failed"):
                raise ServiceHostError("start_outcome_unknown", stage="reconcile", uncertain=True)
        elif phase == "prepared":
            if unit is not None:
                raise ServiceHostError("launch_operation_invalid", stage="reconcile", uncertain=True)
            return _start(root, existing, config)
        elif phase != "failed":
            raise ServiceHostError("launch_operation_invalid", stage="reconcile", uncertain=True)
        _remove_unit(root, existing, unit)
        (root / (existing["attempt"] + ".context")).unlink(missing_ok=True)
    record = _record(config_path, config, root, uid)
    if _unit(record["label"]) is not None:
        raise ServiceHostError("foreign_task", stage="ownership")
    _write(root / (record["attempt"] + ".context"), service_context.capture(config, _binding(record)))
    _write(manifest, record)
    _write(root / "operation.json", dict(attempt=record["attempt"], phase="prepared"))
    return _start(root, record, config)


def _start(root, record, config):
    from .lifecycle import require_active
    require_active(record["config"], config)
    if (root / (record["attempt"] + ".claimed")).exists():
        raise ServiceHostError("launch_attempt_already_consumed", stage="reconcile", uncertain=True)
    _write(root / "operation.json", dict(attempt=record["attempt"], phase="starting"))
    command = ["systemd-run", "--user", "--no-ask-password", "--quiet", "--expand-environment=no",
               "--unit=" + record["label"], "--description=" + _description(root / "launch.json")]
    command.extend("--property=" + key + "=" + value for key, value in _PROPERTIES.items())
    command.extend(["--", record["python"], *record["args"]])
    try:
        _manager(command, stage="start")
    except ServiceHostError as exc:
        if not exc.uncertain:
            try:
                _write(root / "operation.json", dict(attempt=record["attempt"], phase="failed"))
                (root / (record["attempt"] + ".context")).unlink(missing_ok=True)
            except (OSError, ServiceHostError):
                pass
        raise
    try:
        _write(root / "operation.json", dict(attempt=record["attempt"], phase="started"))
    except (OSError, ServiceHostError):
        raise ServiceHostError("start_receipt_write_failed", stage="start", uncertain=True) from None
    return UserService(root, record)


def _remove_unit(root, record, unit):
    _verify_unit(unit, record, root / "launch.json")
    if unit is None:
        return
    if unit["ActiveState"] in _LIVE:
        raise ServiceHostError("task_cleanup_pending", stage="remove", uncertain=True)
    _manager(["systemctl", "--user", "stop", record["label"]], stage="remove")
    remaining = _unit(record["label"])
    if remaining is not None:
        _verify_unit(remaining, record, root / "launch.json")
        _manager(["systemctl", "--user", "reset-failed", record["label"]], stage="remove")
    if _unit(record["label"]) is not None:
        raise ServiceHostError("task_removal_unconfirmed", stage="remove", uncertain=True)


def remove_owned(config_path, config):
    root = _paths(config)
    manifest = root / "launch.json"
    record = _read(manifest)
    if record is None:
        return
    _validate_record(record, config_path, config, _verify_private(root))
    stop = time.monotonic() + 5
    while True:
        unit = _unit(record["label"])
        _verify_unit(unit, record, manifest)
        if unit is None or unit["ActiveState"] not in _LIVE:
            break
        if time.monotonic() >= stop:
            raise ServiceHostError("task_cleanup_pending", stage="remove", uncertain=True)
        time.sleep(.1)
    _remove_unit(root, record, unit)
    (root / (record["attempt"] + ".context")).unlink(missing_ok=True)
    _write(root / "operation.json", dict(attempt=record["attempt"], phase="removed"))
    manifest.unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--attempt", required=True)
    args = parser.parse_args()
    return run_owned(args.manifest, args.attempt, verify_owner=_verify_private,
                     validate_record=_validate_record, binding=_binding,
                     validate_process=_validate_process)


if __name__ == "__main__":
    sys.exit(main())
