"""On-demand, current-user Windows host for one knowledge service.

The scheduler owns the venv launcher outside the native application's Job.
The existing authenticated endpoint and consumer lock remain the readiness
and lifetime authority. Task metadata is only launch/cleanup bookkeeping.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from xml.etree import ElementTree as ET

from ..filesystem import filesystem_path, git_path, path_identity, resolved_path
from . import service_context

MARKER = "mindie-knowledge.windows-service/1"
LIMIT = 256 * 1024
NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_TRANSIENT = {"Running", "Queued"}
SAFE_STAGES = {"identity", "ownership", "read", "write", "query", "interpreter",
               "reconcile", "register", "start", "readiness", "remove", "context", "service"}
SAFE_CODES = {
    "manager_operation_failed", "manager_status_invalid", "identity_unavailable",
    "unowned_file", "unowned_directory", "launch_record_invalid", "launch_record_too_large",
    "private_directory_unavailable", "foreign_task", "windowless_interpreter_missing",
    "interpreter_path_unsupported",
    "foreign_launch_record", "registered_task_missing", "service_action_exited",
    "start_outcome_unknown", "launch_operation_invalid", "registration_outcome_unknown",
    "start_receipt_write_failed",
    "registration_unconfirmed", "task_removal_unconfirmed", "task_cleanup_pending",
    "launch_attempt_mismatch", "service_start_failed", "service_context_decryption_failed",
    "launch_attempt_already_consumed",
    "service_context_encryption_failed", "service_context_unavailable",
    "service_context_binding_invalid", "service_context_file_invalid",
    "service_context_cleanup_failed", "service_launch_context_invalid",
    "service_launch_path_invalid", "service_launch_context_too_large",
}


class ServiceHostError(RuntimeError):
    def __init__(self, code, *, stage, uncertain=False, manager_hresult=None):
        self.code = code if isinstance(code, str) and code in SAFE_CODES else "service_start_failed"
        self.stage = stage if isinstance(stage, str) and stage in SAFE_STAGES else "service"
        self.uncertain = uncertain is True
        super().__init__("knowledge service host: " + self.stage + ":" + self.code)
        self.manager_hresult = manager_hresult


def _quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def _ps(script, *, stage):
    prefix = ("$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
              "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
              "$env:PSModulePath=(Join-Path $PSHOME 'Modules')+[IO.Path]::PathSeparator+$env:PSModulePath;")
    guarded = (
        "try{" + script + "}catch{"
        "$e=$_.Exception;while($e.InnerException){$e=$e.InnerException};"
        "@{mindie_manager_failure=$true;hresult=([int64]$e.HResult -band 4294967295)}"
        "|ConvertTo-Json -Compress;exit 0}"
    )
    encoded = base64.b64encode((prefix + guarded).encode("utf-16-le")).decode("ascii")
    from .process import bounded_run
    try:
        output = bounded_run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-EncodedCommand", encoded], "", timeout=30, max_output=LIMIT,
        )
    except Exception as exc:
        # Only failure before native spawn proves no manager action was sent.
        # Timeout, nonzero exit and an interrupted reply may follow an effect.
        raise ServiceHostError(
            "manager_operation_failed", stage=stage,
            uncertain=getattr(exc, "mindie_execution", None) != "not_started",
        ) from None
    output = output.strip().lstrip("\ufeff")
    try:
        error = json.loads(output)
    except ValueError:
        error = None
    if isinstance(error, dict) and error.get("mindie_manager_failure") is True:
        hresult = error.get("hresult")
        if type(hresult) is not int or not 0 <= hresult <= 0xffffffff:
            hresult = None
        raise ServiceHostError("manager_operation_failed", stage=stage, uncertain=True,
                               manager_hresult=hresult)
    return output


def _sid():
    value = _ps("[Security.Principal.WindowsIdentity]::GetCurrent().User.Value", stage="identity")
    if not re.fullmatch(r"S-1-[0-9-]+", value):
        raise ServiceHostError("identity_unavailable", stage="identity")
    return value


def _read(path):
    try:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            raise ServiceHostError("unowned_file", stage="ownership")
        if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
            raise ValueError()
        result = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (OSError, ValueError):
        raise ServiceHostError("launch_record_invalid", stage="read") from None


def _write(path, value):
    raw = value if isinstance(value, bytes) else (json.dumps(value, sort_keys=True) + "\n").encode()
    if len(raw) > LIMIT:
        raise ServiceHostError("launch_record_too_large", stage="write")
    fd, temporary = tempfile.mkstemp(prefix=".launch-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _private(root):
    # Reuse the dependency's already-tested TokenUser SID ACL creation.
    from mindie_diagnostics.fallback import _private_mkdir
    _private_mkdir(root, parents=True, exist_ok=True)
    _verify_private(root)


def _verify_private(path):
    if path.is_symlink():
        raise ServiceHostError("unowned_directory", stage="ownership")
    script = (
        "$p=" + _quote(str(path)) + ";$a=Get-Acl -LiteralPath $p;"
        "$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;"
        "if($a.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid){exit 9};"
        "foreach($r in $a.Access){$id=$r.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value;"
        "if($r.AccessControlType -eq 'Allow' -and $id -notin @($sid,'S-1-5-18','S-1-5-32-544')){exit 9}}"
    )
    try:
        _ps(script, stage="ownership")
    except ServiceHostError:
        raise ServiceHostError("private_directory_unavailable", stage="ownership") from None


def _paths(config):
    return resolved_path(Path(config["root"]) / config["domain"] / ".windows-service")


def _task(label):
    value = _ps(
        "$t=@(Get-ScheduledTask | Where-Object {$_.TaskPath -eq '\\' -and $_.TaskName -eq "
        + _quote(label) + "});if($t.Count -eq 0){'null'}else{"
        "$t=$t[0];$i=Get-ScheduledTaskInfo -InputObject $t;"
        "$u=$t.Principal.UserId;if($u -notmatch '^S-1-'){"
        "$u=([Security.Principal.NTAccount]::new($u)).Translate([Security.Principal.SecurityIdentifier]).Value};"
        "@{xml=(Export-ScheduledTask -InputObject $t);sid=$u;state=[string]$t.State;"
        "last_result=$i.LastTaskResult;last_run=$i.LastRunTime.ToUniversalTime().ToString('o')}|ConvertTo-Json -Compress}",
        stage="query",
    )
    try:
        return json.loads(value)
    except ValueError:
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
    label = "MindIE Knowledge " + hashlib.sha256(
        (sid + "\n" + path_identity(root.parent)).encode()).hexdigest()[:24]
    return dict(schema=MARKER, sid=sid, label=label, attempt=attempt,
                # Retirement fingerprints retain the caller's resolved
                # spelling. Do not strip an explicit extended namespace.
                config=str(Path(config_path).resolve()),
                cwd=str(resolved_path(Path.cwd())),
                fingerprint=config_fingerprint(config_path, config),
                python=git_path(resolved_path(python)),
                args=["-I", "-m", "mindie_knowledge.loop.windows_service", "--manifest",
                      str(manifest), "--attempt", attempt])


def _binding(record):
    return {key: record[key] for key in ("sid", "attempt", "fingerprint", "cwd")}


def _validate_record(record, config_path, config, sid):
    from .lifecycle import config_fingerprint
    if (not isinstance(record, dict) or record.get("schema") != MARKER
            or record.get("sid") != sid or record.get("fingerprint") != config_fingerprint(config_path, config)
            or path_identity(record.get("config", "")) != path_identity(config_path)
            or not isinstance(record.get("cwd"), str) or not Path(record["cwd"]).is_absolute()
            or not re.fullmatch(r"[a-f0-9]{32}", record.get("attempt", ""))):
        raise ServiceHostError("foreign_launch_record", stage="ownership")


def _result_path(root, record):
    return root / (record["attempt"] + ".result.json")


class ScheduledService:
    """A scheduler-owned service must never be killed as a caller's child."""
    externally_owned = True
    pid = None

    def __init__(self, root, record):
        self.root, self.record = root, record

    def poll(self):
        result = _read(_result_path(self.root, self.record))
        if result is not None and result.get("attempt") != self.record["attempt"]:
            raise ServiceHostError("launch_attempt_mismatch", stage="readiness", uncertain=True)
        if result is not None and result.get("status") == "failed":
            raise ServiceHostError(result.get("code", "service_start_failed"),
                                   stage=result.get("stage", "readiness"))
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
    _private(root)
    manifest = root / "launch.json"
    sid = _sid()
    existing = _read(manifest)
    if existing:
        _validate_record(existing, config_path, config, sid)
        task = _task(existing["label"])
        _verify_task(task, existing, manifest)
        operation = _read(root / "operation.json")
        result = _read(_result_path(root, existing))
        if not isinstance(operation, dict) or operation.get("attempt") != existing["attempt"]:
            raise ServiceHostError("launch_operation_invalid", stage="reconcile", uncertain=True)
        if result is not None and result.get("attempt") != existing["attempt"]:
            raise ServiceHostError("launch_attempt_mismatch", stage="reconcile", uncertain=True)
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
        _ps("Register-ScheduledTask -TaskName " + _quote(record["label"])
            + " -TaskPath '\\' -Xml ([IO.File]::ReadAllText(" + _quote(str(root / "task.xml"))
            + "))" + (" -Force" if loaded else "") + "|Out-Null", stage="register")
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
        _ps("Start-ScheduledTask -TaskName " + _quote(record["label"]), stage="start")
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
        return
    _verify_private(root)
    _validate_record(record, config_path, config, _sid())
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
        _ps("Unregister-ScheduledTask -TaskName " + _quote(record["label"])
            + " -Confirm:$false", stage="remove")
        if _task(record["label"]) is not None:
            raise ServiceHostError("task_removal_unconfirmed", stage="remove", uncertain=True)
    (root / (record["attempt"] + ".context")).unlink(missing_ok=True)
    _write(root / "operation.json", dict(attempt=record["attempt"], phase="removed"))
    manifest.unlink()


def run(manifest, attempt):
    manifest = resolved_path(manifest)
    root = manifest.parent
    record = _read(manifest)
    if not record or record.get("attempt") != attempt:
        raise ServiceHostError("launch_attempt_mismatch", stage="ownership")
    result_path = _result_path(root, record)
    _verify_private(root)
    # One scheduler action may claim a nonce once. A stale or duplicated start
    # cannot enter serve again, even after its first action has exited.
    fd = os.open(root / (attempt + ".claimed"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    result = None
    try:
        from .cli import config_at, _serve
        from .lifecycle import require_active
        config = config_at(record["config"])
        _validate_record(record, record["config"], config, _sid())
        require_active(record["config"], config)
        environment = service_context.consume(root / (attempt + ".context"), config, _binding(record))
        service_context.apply(config, environment)
        # Preserve direct-spawn cwd semantics without Task Scheduler's
        # 260-character WorkingDirectory schema restriction.
        os.chdir(filesystem_path(record["cwd"]))
        _write(result_path, dict(attempt=attempt, status="starting", pid=os.getpid()))
        code = _serve(record["config"], config)
    except Exception as exc:
        # Static class/stage only: the source exception can contain private
        # paths or provider credentials. Existing service DFX retains its own
        # safe startup/runtime diagnostics.
        safe = ServiceHostError(getattr(exc, "code", "service_start_failed"),
                                stage=getattr(exc, "stage", "service"))
        result = dict(attempt=attempt, status="failed", exit_code=1,
                      stage=safe.stage,
                      error_type=type(exc).__name__,
                      code=safe.code)
        try:
            _write(result_path, result)
        except (OSError, ServiceHostError):
            pass
        if "config" in locals() and isinstance(exc, (ServiceHostError, service_context.ContextError)):
            from .diagnostics import record_startup_failure
            try:
                record_startup_failure(record["config"], config, "service_host", exc)
            except (OSError, ValueError):
                pass
        raise
    else:
        result = dict(attempt=attempt, status="finished", exit_code=code)
        _write(result_path, result)
        return code
    finally:
        # A failed transfer remains diagnosable through the static result,
        # but its encrypted values must not become a long-lived secret cache.
        try:
            (root / (attempt + ".context")).unlink(missing_ok=True)
        except OSError:
            if result is not None:
                result["cleanup_failed"] = True
                result["cleanup_stage"] = "context"
                try:
                    _write(result_path, result)
                except (OSError, ServiceHostError):
                    pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--attempt", required=True)
    args = parser.parse_args()
    return run(args.manifest, args.attempt)


if __name__ == "__main__":
    sys.exit(main())
