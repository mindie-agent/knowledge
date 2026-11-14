"""Shared launch receipts and one-use service entry, without manager policy.

[Role] Shared launch receipts and one-use service entry without manager policy.
[Invariants] A launch attempt is consumed at most once; uncertain starts stay uncertain until read-only reconciliation.
[Timeouts & Locks] Bounded readiness probes and lock waits; the authenticated endpoint remains the authority.
[Side-Effect Policy] Writes local launch/operation receipts and delegates manager actions to the platform backend.

Each platform retains its own registration/start/reconciliation contract. The
consumer lock and authenticated endpoint remain the business authority.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile

from ..filesystem import filesystem_path, resolved_path
from . import service_context

LIMIT = 256 * 1024
_contained_pid = None
SAFE_STAGES = {"identity", "ownership", "read", "write", "query", "interpreter",
               "reconcile", "register", "start", "readiness", "remove", "context", "service"}
SAFE_CODES = {
    "manager_operation_failed", "manager_status_invalid", "identity_unavailable",
    "unowned_file", "unowned_directory", "launch_record_invalid", "launch_record_too_large",
    "private_directory_unavailable", "foreign_task", "windowless_interpreter_missing",
    "windowless_interpreter_invalid",
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
    "systemd_version_unsupported", "service_containment_unverified",
}


class _ExternalStop(BaseException):
    """The manager's stop signal ended this attempt; it is not a serve failure."""


class ServiceHostError(RuntimeError):
    def __init__(self, code, *, stage, uncertain=False, manager_hresult=None, startup=None):
        from .diagnostics import startup_failure_details
        self.code = code if isinstance(code, str) and code in SAFE_CODES else "service_start_failed"
        self.stage = stage if isinstance(stage, str) and stage in SAFE_STAGES else "service"
        self.uncertain = uncertain is True
        self._mindie_startup_failure = startup_failure_details(startup)
        message = "knowledge service host: " + self.stage + ":" + self.code
        if self._mindie_startup_failure is not None:
            message += " (" + self._mindie_startup_failure["stage"] + ":" + self._mindie_startup_failure["error_class"] + ")"
        super().__init__(message)
        self.manager_hresult = manager_hresult


def linux_user_runtime_dir() -> str | None:
    if not sys.platform.startswith("linux"):
        return None
    uid = os.getuid()
    raw = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    try:
        st_dir = os.lstat(raw)
        if not stat.S_ISDIR(st_dir.st_mode) or st_dir.st_uid != uid:
            return None
        st_bus = os.lstat(os.path.join(raw, "bus"))
        if not stat.S_ISSOCK(st_bus.st_mode) or st_bus.st_uid != uid:
            return None
        return raw
    except TimeoutError:
        raise
    except OSError:
        return None


def _dbus_session_bus_usable(raw_addr: str) -> bool:
    first = raw_addr.split(";", 1)[0].strip()
    if not first.startswith("unix:"):
        return bool(first)
    for item in first[5:].split(","):
        key, sep, val = item.partition("=")
        if sep and key == "path":
            try:
                return stat.S_ISSOCK(os.lstat(val).st_mode)
            except TimeoutError:
                raise
            except OSError:
                return False
    return True


def linux_systemd_user_bus_available() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        if not stat.S_ISDIR(os.stat("/run/systemd/system").st_mode):
            return False
    except TimeoutError:
        raise
    except OSError:
        return False
    from .._host_exec import which_native
    if any(which_native(cmd) is None for cmd in ("systemctl", "systemd-run", "busctl")):
        return False
    bus_addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "").strip()
    if bus_addr:
        return _dbus_session_bus_usable(bus_addr)
    return linux_user_runtime_dir() is not None


def platform_backend():
    if os.name == "nt":
        from . import windows_service
        return windows_service
    if sys.platform.startswith("linux") and linux_systemd_user_bus_available():
        from . import systemd_service
        return systemd_service
    return None


def backend_for_config(config):
    backend = platform_backend()
    if backend is not None:
        return backend
    if sys.platform.startswith("linux"):
        from . import systemd_service
        if systemd_service.has_launch_record(config):
            return systemd_service
    return None


def process_is_contained():
    """A fork cannot inherit its parent's verified service-main identity."""
    return _contained_pid == os.getpid()


def _mark_process_contained():
    global _contained_pid
    _contained_pid = os.getpid()


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
    except TimeoutError:
        raise
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


def _result_path(root, record):
    return root / (record["attempt"] + ".result.json")


def read_result(root, record):
    result = _read(_result_path(root, record))
    if result is None:
        return None
    if result.get("attempt") != record["attempt"]:
        raise ServiceHostError("launch_attempt_mismatch", stage="readiness", uncertain=True)
    status = result.get("status")
    if (not isinstance(status, str) or status not in {"starting", "finished", "failed"}
            or status in {"finished", "failed"} and type(result.get("exit_code")) is not int
            or status == "starting" and (type(result.get("pid")) is not int or result["pid"] <= 0)):
        raise ServiceHostError("launch_record_invalid", stage="readiness", uncertain=True)
    return result


def _handle_sigterm():
    """Route the manager's stop signal through the terminal receipt path."""
    if os.name != "posix":
        return None
    import signal
    previous = signal.getsignal(signal.SIGTERM)

    def stop(_signum, _frame):
        raise _ExternalStop()

    signal.signal(signal.SIGTERM, stop)
    return signal, previous


def run_owned(manifest, attempt, *, verify_owner, validate_record, binding,
              validate_process=None):
    manifest = resolved_path(manifest)
    root = manifest.parent
    record = _read(manifest)
    if not record or record.get("attempt") != attempt:
        raise ServiceHostError("launch_attempt_mismatch", stage="ownership")
    result_path = _result_path(root, record)
    sid = verify_owner(root)
    # One scheduler action may claim a nonce once. A stale or duplicated start
    # cannot enter serve again, even after its first action has exited.
    fd = os.open(root / (attempt + ".claimed"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    result = None
    handler = _handle_sigterm()
    try:
        from .cli import config_at, _serve
        from .lifecycle import require_active
        config = config_at(record["config"])
        validate_record(record, record["config"], config, sid)
        require_active(record["config"], config)
        environment = service_context.consume(root / (attempt + ".context"), config, binding(record))
        service_context.apply(config, environment)
        if validate_process is not None:
            validate_process(record)
        # Preserve direct-spawn cwd semantics without Task Scheduler's
        # 260-character WorkingDirectory schema restriction.
        os.chdir(filesystem_path(record["cwd"]))
        _write(result_path, dict(attempt=attempt, status="starting", pid=os.getpid()))
        code = _serve(record["config"], config)
    except _ExternalStop:
        # systemctl stop delivers SIGTERM: the accepted attempt ended cleanly,
        # without a serve failure. Record the terminal receipt before exiting.
        result = dict(attempt=attempt, status="finished", exit_code=0, stop="sigterm")
        _write(result_path, result)
        return 0
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
        from .diagnostics import startup_failure_details
        startup = startup_failure_details(getattr(exc, "_mindie_startup_failure", None))
        if startup is not None:
            result["startup"] = startup
        try:
            _write(result_path, result)
        except TimeoutError:
            raise
        except (OSError, ServiceHostError):
            pass
        if "config" in locals() and isinstance(exc, (ServiceHostError, service_context.ContextError)):
            from .diagnostics import record_startup_failure
            try:
                record_startup_failure(record["config"], config, "service_host", exc)
            except TimeoutError:
                raise
            except (OSError, ValueError):
                pass
        raise
    else:
        result = dict(attempt=attempt, status="finished", exit_code=code)
        _write(result_path, result)
        return code
    finally:
        if handler is not None:
            handler[0].signal(handler[0].SIGTERM, handler[1])
        # A failed transfer remains diagnosable through the static result,
        # but its encrypted values must not become a long-lived secret cache.
        try:
            (root / (attempt + ".context")).unlink(missing_ok=True)
        except TimeoutError:
            raise
        except OSError:
            if result is not None:
                result["cleanup_failed"] = True
                result["cleanup_stage"] = "context"
                try:
                    _write(result_path, result)
                except TimeoutError:
                    raise
                except (OSError, ServiceHostError):
                    pass

