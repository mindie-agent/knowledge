"""On-demand, current-user Windows host for one knowledge service.

The scheduler owns the venv launcher outside the native application's Job.
The existing authenticated endpoint and consumer lock remain the readiness
and lifetime authority. Task metadata is only launch/cleanup bookkeeping.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
from xml.etree import ElementTree as ET

from ..filesystem import filesystem_path, git_path, path_identity, resolved_path
from . import service_context

MARKER = "mindie-knowledge.windows-service/1"
NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_TRANSIENT = {"Running", "Queued"}
from .service_host import (LIMIT, SAFE_CODES, SAFE_STAGES, ServiceHostError,
                           _read, _write, _result_path, read_result, run_owned)


def _private(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return _verify_private(root)


def _verify_private(path):
    if path.is_symlink():
        raise ServiceHostError("unowned_directory", stage="ownership")
    from .windows_manager import private_sid
    return private_sid(path)


def _paths(config):
    return resolved_path(Path(config["root"]) / config["domain"] / ".windows-service")


def _task(label):
    from .windows_manager import task as read_task
    value = read_task(label)
    try:
        task = value
        if task is None:
            return None
        if not isinstance(task, dict) or task.get("state") not in {"Disabled", "Queued", "Ready", "Running"}:
            raise ValueError()
        run_level = task.pop("run_level")
        if type(run_level) is not int or run_level not in {0, 1}:
            raise ValueError()
        root = ET.fromstring(task["xml"])
        ns = "{" + NS + "}"
        principals = root.findall(ns + "Principals/" + ns + "Principal")
        if len(principals) != 1:
            raise ValueError()
        levels = principals[0].findall(ns + "RunLevel")
        effective = {0: "LeastPrivilege", 1: "HighestAvailable"}[run_level]
        if not levels:
            # COM omits the default that CIM exports explicitly. Project the
            # observed effective value; the ownership validator stays strict.
            ET.SubElement(principals[0], ns + "RunLevel").text = effective
            task["xml"] = ET.tostring(root, encoding="unicode")
        elif len(levels) != 1 or levels[0].text != effective:
            raise ValueError()
        return task
    except (ValueError, TypeError, KeyError, ET.ParseError):
        raise ServiceHostError("manager_status_invalid", stage="query") from None


def _xml(record, manifest):
    ET.register_namespace("", NS)
    root = ET.Element("{" + NS + "}Task", version="1.2")
    def el(parent, name, text=None, **attrs):
        node = ET.SubElement(parent, "{" + NS + "}" + name, attrs)
        node.text = text
        return node
    el(el(root, "RegistrationInfo"), "Description", MARKER + ":" + str(manifest))
    el(root, "Triggers")
    principal = el(el(root, "Principals"), "Principal", id="Author")
    el(principal, "UserId", record["sid"])
    el(principal, "LogonType", "InteractiveToken")
    el(principal, "RunLevel", "LeastPrivilege")
    settings = el(root, "Settings")
    for key, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"), ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"), ("StartWhenAvailable", "false"),
        ("Enabled", "true"), ("Hidden", "true"), ("ExecutionTimeLimit", "PT0S"),
    ):
        el(settings, key, value)
    action = el(el(root, "Actions", Context="Author"), "Exec")
    el(action, "Command", record["python"])
    el(action, "Arguments", subprocess.list2cmdline(record["args"]))
    # Task Scheduler's pathType limits WorkingDirectory to 260 characters.
    # The module/config/manifest are absolute; data roots need no task cwd.
    return ET.tostring(root, encoding="utf-8")


def _verify_task(task, record, manifest):
    if task is None:
        return
    if record is None:
        raise ServiceHostError("foreign_task", stage="ownership")
    try:
        root = ET.fromstring(task["xml"])
        def values(name):
            return [node.text or "" for node in root.iter() if node.tag == "{" + NS + "}" + name]
        expected = {
            "Description": MARKER + ":" + str(manifest), "Command": record["python"],
            "Arguments": subprocess.list2cmdline(record["args"]),
            "LogonType": "InteractiveToken", "RunLevel": "LeastPrivilege",
            "MultipleInstancesPolicy": "IgnoreNew", "ExecutionTimeLimit": "PT0S",
        }
        actions = root.findall("{" + NS + "}Actions")
        principals = root.findall("{" + NS + "}Principals")
        triggers = root.findall("{" + NS + "}Triggers")
        if (len(actions) != 1 or len(list(actions[0])) != 1
                or actions[0][0].tag != "{" + NS + "}Exec"
                or len(principals) != 1 or len(list(principals[0])) != 1
                or principals[0][0].tag != "{" + NS + "}Principal"
                or {node.tag for node in principals[0][0]} != {
                    "{" + NS + "}" + name for name in ("UserId", "LogonType", "RunLevel")}
                or len(triggers) != 1 or list(triggers[0])
                or values("WorkingDirectory")):
            raise ValueError()
        if (task["sid"] != record["sid"]
                or any(values(key) != [value] for key, value in expected.items())
                or any(list(node) for node in root.iter() if node.tag == "{" + NS + "}Triggers")):
            raise ValueError()
    except (ValueError, KeyError, TypeError, ET.ParseError):
        raise ServiceHostError("foreign_task", stage="ownership") from None


def _record(config_path, config, root, sid):
    from .lifecycle import config_fingerprint
    python = Path(sys.executable).with_name("pythonw.exe")
    if not python.is_file():
        raise ServiceHostError("windowless_interpreter_missing", stage="interpreter")
    if len(git_path(resolved_path(python))) > 260:
        raise ServiceHostError("interpreter_path_unsupported", stage="interpreter")
    attempt = uuid.uuid4().hex
    manifest = root / "launch.json"
    return dict(schema=MARKER, sid=sid, label=_label(sid, root), attempt=attempt,
                # Retirement fingerprints retain the caller's resolved
                # spelling. Do not strip an explicit extended namespace.
                config=str(Path(config_path).resolve()),
                cwd=str(resolved_path(Path.cwd())),
                fingerprint=config_fingerprint(config_path, config),
                python=git_path(resolved_path(python)),
                args=["-I", "-m", "mindie_knowledge.loop.windows_service", "--manifest",
                      str(manifest), "--attempt", attempt])


def _label(sid, root):
    return "MindIE Knowledge " + hashlib.sha256(
        (sid + "\n" + path_identity(root.parent)).encode()).hexdigest()[:24]


def _binding(record):
    return {key: record[key] for key in ("sid", "attempt", "fingerprint", "cwd")}


def _validate_record(record, config_path, config, sid):
    from .lifecycle import config_fingerprint
    if (not isinstance(record, dict) or record.get("schema") != MARKER
            or record.get("sid") != sid or record.get("fingerprint") != config_fingerprint(config_path, config)
            or path_identity(record.get("config", "")) != path_identity(config_path)
            or not isinstance(record.get("cwd"), str) or not Path(record["cwd"]).is_absolute()
            or not isinstance(record.get("attempt"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", record.get("attempt", ""))):
        raise ServiceHostError("foreign_launch_record", stage="ownership")


def _validate_superseded(record, config_path, config, sid, root):
    """Same ownership chain, older generation: every ``_validate_record``
    identity check except the two an update legitimately changes — the
    configuration fingerprint and the configuration FILE PATH. The stable
    ownership identity is the domain state root the record was minted for:
    ``label`` is exactly the registration name derived from sid +
    root/domain at first launch, so two configs that differ only by
    generation path but serve the same state root and domain are the same
    chain. A different state root or domain yields a different label and
    stays hard foreign."""
    from .lifecycle import config_fingerprint
    fingerprint = record.get("fingerprint") if isinstance(record, dict) else None
    if (not isinstance(record, dict) or record.get("schema") != MARKER
            or record.get("sid") != sid
            or not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint)
            or fingerprint == config_fingerprint(config_path, config)
            or record.get("label") != _label(sid, root)
            or not isinstance(record.get("config"), str) or not Path(record["config"]).is_absolute()
            or not isinstance(record.get("cwd"), str) or not Path(record["cwd"]).is_absolute()
            or not isinstance(record.get("attempt"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", record.get("attempt", ""))):
        raise ServiceHostError("foreign_launch_record", stage="ownership")


def _pid_dead(pid):
    """The recorded pid provably refers to an exited process.

    A live, recycled, inaccessible or out-of-range pid is never proof.
    """
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except (OSError, OverflowError):
            pass
        return False
    import pywintypes
    import win32api
    import win32con
    import win32process
    try:
        # pywin32 splits this family: OpenProcess/CloseHandle live in
        # win32api, GetExitCodeProcess lives in win32process.
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    except pywintypes.error as exc:
        return exc.winerror == 87  # ERROR_INVALID_PARAMETER: no such process
    try:
        try:
            return win32process.GetExitCodeProcess(handle) != win32con.STILL_ACTIVE
        except pywintypes.error:
            return False
    finally:
        win32api.CloseHandle(handle)


def _process_proof_dead(root, attempt, result):
    """Local receipts/nonces prove the accepted process ended (or never ran)."""
    if result is not None and result.get("status") in {"finished", "failed"}:
        return True
    if result is not None and result.get("status") == "starting":
        return _pid_dead(result["pid"])
    # Without the one-use nonce the action never entered the owned runner.
    return not (root / (attempt + ".claimed")).exists()


def _attempt_inert(root, record, task, result):
    """The record's accepted action provably ended; it can never serve again."""
    if task is not None and task["state"] in _TRANSIENT:
        return False
    if result is not None and result.get("status") == "starting":
        # A live recorded pid vetoes every other proof, always.
        return _pid_dead(result["pid"])
    if _process_proof_dead(root, record["attempt"], result):
        return True
    # The manager confirms the action exited even without a receipt — but
    # only against a recorded pre-Start run stamp; a record from before the
    # Start was sent proves nothing about execution.
    return (task is not None and record.get("before_run") is not None
            and task["last_run"] != record["before_run"])


def _orphan_attempt(task, manifest, sid):
    """The consumed attempt nonce of a same-chain orphaned registered task.

    The label already pins install and user (the task was queried by name);
    the XML must still carry this install's exact manifest identity, the
    owned module invocation and the strict single-action shape. The
    interpreter path is deliberately NOT pinned: a superseded venv is the
    normal update case. Anything else stays a hard foreign task.
    """
    if task["sid"] != sid:
        raise ServiceHostError("foreign_task", stage="ownership")
    try:
        root = ET.fromstring(task["xml"])
        def values(name):
            return [node.text or "" for node in root.iter() if node.tag == "{" + NS + "}" + name]
        expected = {
            "Description": MARKER + ":" + str(manifest),
            "LogonType": "InteractiveToken", "RunLevel": "LeastPrivilege",
            "MultipleInstancesPolicy": "IgnoreNew", "ExecutionTimeLimit": "PT0S",
        }
        actions = root.findall("{" + NS + "}Actions")
        principals = root.findall("{" + NS + "}Principals")
        triggers = root.findall("{" + NS + "}Triggers")
        if (len(actions) != 1 or len(list(actions[0])) != 1
                or actions[0][0].tag != "{" + NS + "}Exec"
                or len(principals) != 1 or len(list(principals[0])) != 1
                or principals[0][0].tag != "{" + NS + "}Principal"
                or {node.tag for node in principals[0][0]} != {
                    "{" + NS + "}" + name for name in ("UserId", "LogonType", "RunLevel")}
                or len(triggers) != 1 or list(triggers[0])
                or values("WorkingDirectory")
                or any(values(key) != [value] for key, value in expected.items())):
            raise ValueError()
        prefix = subprocess.list2cmdline(
            ["-I", "-m", "mindie_knowledge.loop.windows_service",
             "--manifest", str(manifest), "--attempt"])
        arguments = values("Arguments")
        if (len(arguments) != 1 or not arguments[0].startswith(prefix + " ")
                or not re.fullmatch(r"[a-f0-9]{32}", arguments[0][len(prefix) + 1:])):
            raise ValueError()
        return arguments[0][len(prefix) + 1:]
    except (ValueError, KeyError, TypeError, ET.ParseError):
        raise ServiceHostError("foreign_task", stage="ownership") from None


def _orphan_inert(root, task, attempt):
    """An orphaned task is inert only when its action is provably finished."""
    if task["state"] in _TRANSIENT:
        return False
    try:
        result = read_result(root, dict(attempt=attempt))
    except ServiceHostError:
        return False  # An unreadable receipt proves nothing.
    return _process_proof_dead(root, attempt, result)


def _remove_task(label):
    from .windows_manager import remove
    remove(label)
    if _task(label) is not None:
        raise ServiceHostError("task_removal_unconfirmed", stage="remove", uncertain=True)


def _retire_orphan(root, task, label, manifest, sid, *, stage):
    attempt = _orphan_attempt(task, manifest, sid)
    if not _orphan_inert(root, task, attempt):
        raise ServiceHostError("foreign_task", stage="ownership")
    from .dfx import failure as record_failure
    record_failure("knowledge.service", stage=stage,
                   category="stale_registered_task_retired", reportable=False)
    _remove_task(label)


def _retire_record(root, record, config_path, config, sid, manifest, *, stage):
    """Retire a superseded launch record once it is provably inert.

    A live task, a live recorded pid, or any identity beyond the
    configuration fingerprint keeps the original foreign guard verdict.
    """
    _validate_superseded(record, config_path, config, sid, root)
    task = _task(record["label"])
    _verify_task(task, record, manifest)
    result = read_result(root, record)
    if not _attempt_inert(root, record, task, result):
        raise ServiceHostError("foreign_launch_record", stage="ownership")
    from .dfx import failure as record_failure
    record_failure("knowledge.service", stage=stage,
                   category="stale_launch_record_retired", reportable=False)
    if task is not None:
        _remove_task(record["label"])
    (root / (record["attempt"] + ".context")).unlink(missing_ok=True)
    manifest.unlink()



class ScheduledService:
    """A scheduler-owned service must never be killed as a caller's child."""
    externally_owned = True
    pid = None

    def __init__(self, root, record):
        self.root, self.record = root, record

    def poll(self):
        result = read_result(self.root, self.record)
        if result is not None and result.get("status") == "failed":
            raise ServiceHostError(result.get("code", "service_start_failed"),
                                   stage=result.get("stage", "readiness"),
                                   startup=result.get("startup"))
        if result is not None and result.get("status") == "finished":
            return result.get("exit_code", 1)
        task = _task(self.record["label"])
        _verify_task(task, self.record, self.root / "launch.json")
        if task is None:
            raise ServiceHostError("registered_task_missing", stage="readiness", uncertain=True)
        if task["state"] not in _TRANSIENT and task["last_run"] != self.record.get("before_run"):
            # The manager confirms that this action exited, even if Python
            # failed before it could save a launch result.
            raise ServiceHostError("service_action_exited", stage="readiness")
        if task["state"] not in _TRANSIENT and result is None:
            raise ServiceHostError("start_outcome_unknown", stage="readiness", uncertain=True)
        return None


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
    """Called under the existing domain start lock; never waits for readiness."""
    from .lifecycle import require_active
    require_active(config_path, config)
    service_context.validate(config)
    root = _paths(config)
    sid = _private(root)
    manifest = root / "launch.json"
    existing = _read(manifest)
    if existing:
        try:
            _validate_record(existing, config_path, config, sid)
        except ServiceHostError as exc:
            if exc.code != "foreign_launch_record":
                raise
            # An update changed the engine configuration while the service
            # was off. A provably inert leftover of the same ownership chain
            # is retired; anything live or unprovable keeps the foreign verdict.
            _retire_record(root, existing, config_path, config, sid, manifest,
                           stage="reconcile")
            existing = None
    if existing:
        task = _task(existing["label"])
        _verify_task(task, existing, manifest)
        operation = _read(root / "operation.json")
        result = read_result(root, existing)
        if not isinstance(operation, dict) or operation.get("attempt") != existing["attempt"]:
            raise ServiceHostError("launch_operation_invalid", stage="reconcile", uncertain=True)
        if task and task["state"] in _TRANSIENT:
            return ScheduledService(root, existing)
        phase = operation.get("phase")
        if phase in {"starting", "started"}:
            if result and result.get("status") in {"failed", "finished"}:
                pass  # A later ordinary call may start after a proven exit.
            elif task and task["last_run"] != existing.get("before_run"):
                # Scheduler confirms the attempted action exited before its
                # wrapper could write a result. A new call is a new attempt.
                pass
            else:
                raise ServiceHostError("start_outcome_unknown", stage="reconcile", uncertain=True)
        elif phase in {"registering", "registered", "prepared"}:
            if task is None and phase == "registering":
                raise ServiceHostError("registration_outcome_unknown", stage="reconcile", uncertain=True)
            if task is not None:
                # Registration can be reconciled exactly. No Start was issued
                # in these phases, so continuing is not an external replay.
                return _start_registered(root, existing, task)
        elif phase != "failed":
            raise ServiceHostError("launch_operation_invalid", stage="reconcile", uncertain=True)
        old_context = root / (existing["attempt"] + ".context")
        old_context.unlink(missing_ok=True)
    else:
        task = None
    record = _record(config_path, config, root, sid)
    loaded = _task(record["label"])
    if loaded is not None and existing is None:
        # A registered task outliving its launch record (manual archive, lost
        # manifest) still blocks registration. Retire it only when it is
        # provably this install's own chain and provably inert.
        _retire_orphan(root, loaded, record["label"], manifest, sid, stage="reconcile")
        loaded = None
    _verify_task(loaded, existing, manifest)
    record["before_run"] = loaded["last_run"] if loaded else None
    blob = service_context.capture(config, _binding(record))
    context_path = root / (record["attempt"] + ".context")
    _write(context_path, blob)
    try:
        return _register_new(root, record, loaded)
    except BaseException:
        _cleanup_unstarted_context(root, record, context_path)
        raise


def _cleanup_unstarted_context(root, record, context_path):
    # The durable intent is written before Start. An accepted or uncertain
    # Start may still consume its transfer; only proven pre-start failures
    # can remove it. Reconciliation/terminal exit handles the remaining case.
    try:
        operation = _read(root / "operation.json")
    except (OSError, ServiceHostError):
        return
    if operation is not None and (operation.get("attempt") != record["attempt"]
                                  or operation.get("phase") not in {
                                      "prepared", "registering", "registered", "failed"}):
        return
    try:
        context_path.unlink(missing_ok=True)
    except OSError:
        try:
            _write(root / (record["attempt"] + ".cleanup.json"),
                   dict(attempt=record["attempt"], stage="context", cleanup_failed=True))
        except (OSError, ServiceHostError):
            pass


def _register_new(root, record, loaded):
    manifest = root / "launch.json"
    _write(manifest, record)
    _write(root / "task.xml", _xml(record, manifest))
    _write(root / "operation.json", dict(attempt=record["attempt"], phase="registering"))
    try:
        from .windows_manager import register
        register(record["label"], (root / "task.xml").read_text(encoding="utf-8"),
                 record["sid"], replace=loaded is not None)
    except ServiceHostError as exc:
        if not exc.uncertain:
            try:
                _write(root / "operation.json", dict(attempt=record["attempt"], phase="failed"))
            except (OSError, ServiceHostError):
                pass  # Preserve the primary manager failure.
        raise
    task = _task(record["label"])
    if task is None:
        raise ServiceHostError("registration_unconfirmed", stage="register", uncertain=True)
    _verify_task(task, record, manifest)
    return _start_registered(root, record, task)


def _start_registered(root, record, task):
    from .cli import config_at
    from .lifecycle import require_active
    config = config_at(record["config"])
    require_active(record["config"], config)
    if (root / (record["attempt"] + ".claimed")).exists():
        raise ServiceHostError("launch_attempt_already_consumed", stage="reconcile", uncertain=True)
    context_path = root / (record["attempt"] + ".context")
    if not context_path.exists():
        _write(context_path, service_context.capture(config, _binding(record)))
    try:
        return _send_start(root, record, task)
    except BaseException:
        _cleanup_unstarted_context(root, record, context_path)
        raise


def _send_start(root, record, task):
    record = dict(record, before_run=task["last_run"])
    _write(root / "launch.json", record)
    _write(root / "operation.json", dict(attempt=record["attempt"], phase="starting"))
    try:
        from .windows_manager import start
        start(record["label"])
    except ServiceHostError as exc:
        if not exc.uncertain:
            try:
                _write(root / "operation.json", dict(attempt=record["attempt"], phase="failed"))
            except (OSError, ServiceHostError):
                pass
        raise
    try:
        _write(root / "operation.json", dict(attempt=record["attempt"], phase="started"))
    except (OSError, ServiceHostError):
        # The manager accepted Start, but the local acknowledgement failed.
        # Keep the starting intent and one-use transfer for exact reconciliation.
        raise ServiceHostError("start_receipt_write_failed", stage="start", uncertain=True) from None
    return ScheduledService(root, record)


def remove_owned(config_path, config):
    """After exact retirement and consumer release, remove only our idle task."""
    root = _paths(config)
    manifest = root / "launch.json"
    record = _read(manifest)
    if record is None:
        if not root.is_dir():
            return
        sid = _verify_private(root)
        task = _task(_label(sid, root))
        if task is not None:
            _retire_orphan(root, task, _label(sid, root), manifest, sid, stage="remove")
        return
    sid = _verify_private(root)
    try:
        _validate_record(record, config_path, config, sid)
    except ServiceHostError as exc:
        if exc.code != "foreign_launch_record":
            raise
        _retire_record(root, record, config_path, config, sid, manifest, stage="remove")
        return
    task = _task(record["label"])
    _verify_task(task, record, manifest)
    # The business consumer has exited. This is a finite manager-cleanup
    # observation, not a deadline on service work or a reason to kill it.
    stop = time.monotonic() + 5
    while task is not None and task["state"] in _TRANSIENT:
        if time.monotonic() >= stop:
            raise ServiceHostError("task_cleanup_pending", stage="remove", uncertain=True)
        time.sleep(.1)
        task = _task(record["label"])
        _verify_task(task, record, manifest)
    if task is not None:
        _remove_task(record["label"])
    (root / (record["attempt"] + ".context")).unlink(missing_ok=True)
    _write(root / "operation.json", dict(attempt=record["attempt"], phase="removed"))
    manifest.unlink()


@contextmanager
def _windowless_streams():
    """Match detached DEVNULL semantics only where pythonw has no stream."""
    owned = []
    try:
        for name, mode in (("stdin", "r"), ("stdout", "w"), ("stderr", "w")):
            if getattr(sys, name) is None:
                stream = open(os.devnull, mode, encoding="utf-8")
                owned.append((name, stream))
                setattr(sys, name, stream)
        yield
    finally:
        for name, stream in reversed(owned):
            if getattr(sys, name) is stream:
                setattr(sys, name, None)
            stream.close()


def run(manifest, attempt):
    with _windowless_streams():
        return _run(manifest, attempt)


def _run(manifest, attempt):
    return run_owned(manifest, attempt, verify_owner=_verify_private,
                     validate_record=_validate_record, binding=_binding)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--attempt", required=True)
    args = parser.parse_args()
    return run(args.manifest, args.attempt)


if __name__ == "__main__":
    sys.exit(main())
