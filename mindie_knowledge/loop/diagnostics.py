"""Bounded, read-only local diagnostics; never a source of authorization.

Native callers supply an already established task identity. Operator CLI callers
may inspect a local identity, but this view grants no access and changes no state.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit

from mindie_knowledge.markdown import _atomic_write_text
from .store import session_key
from .process import AGENT_ERROR_EXIT_CODES

MAX_JSON = 64 * 1024
DB_SECONDS = 0.25
_SAFE_CLASSES = frozenset({
    "RuntimeError", "ValueError", "TypeError", "KeyError", "OSError",
    "PermissionError", "FileNotFoundError", "TimeoutError", "TimeoutExpired",
    "ConnectionRefusedError", "ConnectionResetError", "URLError", "HTTPError",
    "DatabaseError", "OperationalError", "IntegrityError", "JSONDecodeError",
    "ImportError", "ModuleNotFoundError", "SyntaxError", "BudgetExceeded",
    "MaintenanceCancelled", "CommandTimedOut", "OutputLimitExceeded",
})
_NATIVE_FAILURE = re.compile(
    r"RuntimeError: maintenance agent exited (-?\d{1,5}); "
    r"category=([a-z_]+); elapsed=(\d{1,7}(?:\.\d{1,6})?)s\Z"
)
_DEADLINE_FAILURE = re.compile(
    r"TimeoutError: maintenance deadline exceeded; "
    r"category=deadline; elapsed=(\d{1,7}(?:\.\d{1,6})?)s\Z"
)
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_PR_URL = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9]\d*\Z")
_CAPTURE_STATUSES = frozenset({"queued", "pending", "processing", "apply-pending", "organized", "failed",
                              "cancelled", "discarded", "no-new-material", "no-shareable-material"})
_DELIVERY_CAUSES = frozenset({
    "missing-identity", "admission-unreadable", "store-not-ready", "schema-not-ready",
    "store-locked", "budget-exhausted", "service-unavailable", "worker-unavailable",
    "wake-failed", "internal",
})
_DELIVERY_RECOVERY = {
    "missing-identity": (
        "Native Stop must pass session_id and either a native turn_id "
        "(identity_kind turn) or an explicit notification event_id "
        "(identity_kind notification). Do not invent a turn id or read one "
        "from a transcript tail."
    ),
    "admission-unreadable": (
        "The admission store could not be read, so no capture was accepted. "
        "This is not an inactive session. Inspect its permissions. The next "
        "real task completion can hand off after it is readable. Do not replay "
        "this Stop. A healthy query does not accept the missed event."
    ),
    "store-not-ready": (
        "The store was not ready, so this Stop accepted nothing. An authorized "
        "knowledge query or activation prepares it. The next real task "
        "completion hands off. Do not replay this Stop. A healthy query does "
        "not accept the missed event."
    ),
    "schema-not-ready": (
        "The schema was not ready, so this Stop accepted nothing and did not "
        "migrate. An authorized knowledge query, activation, or update restore "
        "prepares it. The next real task completion hands off. Do not replay "
        "this Stop. A healthy query does not accept the missed event."
    ),
    "store-locked": (
        "The store was locked, so this Stop accepted nothing. The next real "
        "task completion can hand off after the current write finishes. Do not "
        "replay this Stop. A healthy query does not accept the missed event."
    ),
    "budget-exhausted": (
        "The Stop budget ran out before commit, so this event was not accepted. "
        "The next real task completion can hand it off. Do not replay this Stop. "
        "A healthy query does not accept the missed event."
    ),
    "service-unavailable": (
        "The capture row is durable. The next real task completion wakes the "
        "worker once if the process is absent. Do not resubmit model work. A "
        "healthy query does not clear this record or replace the row."
    ),
    "worker-unavailable": (
        "The service process is up but its worker is not consuming. Do not "
        "start a second service. Pending rows remain. Run maintenance-resume "
        "only for a paused maintenance circuit. A healthy query does not "
        "apply or republish the pending row."
    ),
    "wake-failed": (
        "The capture row is durable. Inspect startup status. The next real "
        "task completion may wake once. Nothing was scheduled to restart. Do "
        "not replay this Stop. A healthy query does not accept a missed event."
    ),
    "internal": (
        "Inspect knowledge capture diagnostics. This call did not report "
        "success. Do not replay this Stop."
    ),
}
_BATCH_STATUSES = frozenset({"pending", "intent", "unknown", "failed", "needs_review",
                            "disabled", "unavailable", "submitted", "updated", "unchanged"})


def error_class(exc):
    name = type(exc).__name__
    return name if name in _SAFE_CLASSES else "UnknownError"


def failure(detail):
    """Project only the fixed core grammar; arbitrary exception prose is private."""
    if not isinstance(detail, str) or not detail:
        return {}
    match = _NATIVE_FAILURE.fullmatch(detail)
    if match:
        code, category, elapsed = match.groups()
        code = int(code)
        expected = next((k for k, v in AGENT_ERROR_EXIT_CODES.items() if v == code), "unknown")
        return dict(error_class="RuntimeError", exit_code=code,
                    category=expected if category == expected else "unknown",
                    elapsed_seconds=float(elapsed))
    match = _DEADLINE_FAILURE.fullmatch(detail)
    if match:
        return dict(error_class="TimeoutError", category="deadline",
                    elapsed_seconds=float(match[1]))
    name = detail.partition(":")[0]
    return dict(error_class=name if name in _SAFE_CLASSES else "UnknownError")


def _bytes(path, limit=MAX_JSON):
    # Follow ordinary configuration symlinks, but never wait for a FIFO writer
    # or read a device. fstat checks the opened object, avoiding a stat/open race.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("diagnostic metadata must be a regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
    finally:
        os.close(fd)
    if len(raw) > limit:
        raise ValueError("diagnostic metadata exceeds bound")
    return raw


def _json(path, limit=MAX_JSON):
    value = json.loads(_bytes(path, limit))
    if not isinstance(value, dict):
        raise ValueError("diagnostic metadata must be an object")
    return value


def _ro(path):
    db = sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True, timeout=0.1)
    deadline = time.monotonic() + DB_SECONDS
    db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    db.row_factory = sqlite3.Row
    return db


def _startup_path(config):
    return Path(config["root"]) / config["domain"] / "latest-startup.json"


def _delivery_path(config):
    return Path(config["root"]) / config["domain"] / "latest-delivery.json"


def _fingerprint(config):
    # Bind to the configuration actually parsed by this process. Re-reading the
    # path here could associate an old child failure with a concurrently edited config.
    raw = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def record_startup_failure(config_path, config, stage, exc):
    """Owned serve child only. Persist a small safe failure, never provider stderr."""
    if stage not in {"store", "admission", "transcript_adapter", "engine", "service"}:
        raise ValueError("invalid startup stage")
    cause = "startup_failed"
    component = config.get("transcript_adapter") if stage == "transcript_adapter" else None
    if isinstance(exc, PermissionError):
        cause = "permission"
    elif isinstance(exc, sqlite3.Error):
        cause = "database"
    elif stage == "transcript_adapter":
        cause = "missing_component" if component and not Path(component).is_file() else "import_failed"
    payload = dict(status="failed", stage=stage, error_class=error_class(exc), cause=cause,
                   config_fingerprint=_fingerprint(config))
    if component:
        payload["component_path"] = str(component)
    path = _startup_path(config)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps(payload) + "\n")
    path.chmod(0o600)


def record_delivery_failure(config, *, stage, cause, capture_id=None, event=None):
    """One latest unresolved handoff. No transcript, token, or endpoint."""
    if cause not in _DELIVERY_CAUSES:
        cause = "internal"
    if stage not in {"accept", "wake", "runtime"}:
        raise ValueError("invalid delivery stage")
    payload = dict(
        status="unresolved", component="knowledge.capture", stage=stage, cause=cause,
        config_fingerprint=_fingerprint(config),
    )
    if isinstance(capture_id, str) and _SAFE_ID.fullmatch(capture_id):
        payload["capture_id"] = capture_id
    if isinstance(event, str) and _SAFE_ID.fullmatch(event):
        payload["event"] = event
    path = _delivery_path(config)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps(payload) + "\n")
    path.chmod(0o600)


def clear_delivery_event(config, event):
    """Clear only the record for this event. A healthy runtime is not enough."""
    if not isinstance(event, str) or not _SAFE_ID.fullmatch(event):
        return
    path = _delivery_path(config)
    try:
        raw = _json(path, 8192)
    except (OSError, ValueError):
        return
    if raw.get("config_fingerprint") != _fingerprint(config) or raw.get("event") != event:
        return
    try:
        path.unlink()
    except OSError:
        pass


def clear_startup_failure(config_path, config):
    """Only the caller's successful readiness probe may retire its old failure."""
    path = _startup_path(config)
    try:
        if _json(path, 8192).get("config_fingerprint") == _fingerprint(config):
            path.unlink()
    except (OSError, ValueError):
        pass


def _startup(config_path, config):
    try:
        raw = _json(_startup_path(config), 8192)
    except FileNotFoundError:
        return dict(status="absent")
    except (OSError, ValueError) as exc:
        return dict(status="unavailable", error_class=error_class(exc))
    if raw.get("config_fingerprint") != _fingerprint(config):
        return dict(status="absent")
    if not isinstance(raw.get("stage"), str) or raw["stage"] not in {"store", "admission", "transcript_adapter", "engine", "service"}:
        return dict(status="unavailable", error_class="ValueError")
    result = dict(status="failed", stage=raw["stage"],
                  error_class=raw.get("error_class") if isinstance(raw.get("error_class"), str) and raw["error_class"] in _SAFE_CLASSES else "UnknownError",
                  cause=raw.get("cause") if isinstance(raw.get("cause"), str) and raw["cause"] in {"startup_failed", "permission", "database", "missing_component", "import_failed"} else "startup_failed")
    # Derive the path from the current local config, not arbitrary record content.
    if raw["stage"] == "transcript_adapter" and config.get("transcript_adapter"):
        result["component_path"] = config["transcript_adapter"]
    return result


def _delivery(config):
    try:
        raw = _json(_delivery_path(config), 8192)
    except FileNotFoundError:
        return dict(status="absent")
    except (OSError, ValueError) as exc:
        return dict(status="unavailable", error_class=error_class(exc))
    if raw.get("config_fingerprint") != _fingerprint(config):
        return dict(status="absent")
    cause = raw.get("cause") if raw.get("cause") in _DELIVERY_CAUSES else "internal"
    stage = raw.get("stage") if raw.get("stage") in {"accept", "wake", "runtime"} else "accept"
    result = dict(status="unresolved", component="knowledge.capture", stage=stage,
                  cause=cause, recovery=_DELIVERY_RECOVERY[cause])
    if isinstance(raw.get("capture_id"), str) and _SAFE_ID.fullmatch(raw["capture_id"]):
        result["capture_id"] = raw["capture_id"]
    if isinstance(raw.get("event"), str) and _SAFE_ID.fullmatch(raw["event"]):
        result["event"] = raw["event"]
    return result


def _service(config):
    from .transport import rpc
    try:
        connection = _json(Path(config["root"]) / config["domain"] / "connection.json")
    except FileNotFoundError:
        return dict(status="not-running")
    except (OSError, ValueError) as exc:
        return dict(status="unavailable", stage="connection", error_class=error_class(exc))
    try:
        if not isinstance(connection.get("url"), str):
            raise ValueError("connection URL must be text")
        url = urlsplit(connection["url"])
        if (url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port
                or url.username or url.password or url.path not in {"", "/"}
                or url.query or url.fragment or connection.get("domain") != config["domain"]
                or not isinstance(connection.get("token"), str) or not 1 <= len(connection["token"]) <= 512):
            raise ValueError("invalid existing loopback connection")
        status = rpc(connection, "status", timeout=0.4)
        result = dict(status="running")
        if isinstance(status, dict) and "worker_alive" in status:
            result["worker_failed"] = status.get("worker_failed") is True
            result["worker_alive"] = status.get("worker_alive") is True and not result["worker_failed"]
        return result
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        return dict(status="unavailable", stage="probe", error_class=error_class(exc))


def _task_roots(config, session):
    roots = {session}
    path = config.get("admission_path")
    if path:
        try:
            db = _ro(path)
            try:
                row = db.execute("SELECT root_session FROM leases WHERE session=? LIMIT 1", (session,)).fetchone()
                if row and isinstance(row[0], str) and row[0].strip() and len(row[0]) <= 256:
                    roots.add(row[0])
            finally:
                db.close()
        except (OSError, sqlite3.Error):
            # Missing/unavailable admission never broadens task scope.
            pass
    return sorted(session_key(root) for root in roots)


def _store(config, session, result):
    path = Path(config["root"]) / config["domain"] / "store-v3.sqlite3"
    try:
        path.stat()
    except FileNotFoundError:
        result["store"] = dict(status="absent")
        return
    except OSError as exc:
        result["store"] = dict(status="unavailable", error_class=error_class(exc))
        return
    db = None
    try:
        db = _ro(path)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        paused = bool(db.execute("SELECT 1 FROM state WHERE key='maintenance_paused'").fetchone())
        counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("captures", "outbox")}
        result["maintenance"] = dict(status="paused" if paused else "ok", scope="shared", paused=paused, counts=counts)
        if "maintenance_attempts" in tables:
            result["maintenance"]["calls_last_hour"] = db.execute(
                "SELECT count(*) FROM maintenance_attempts WHERE started>?", (time.time() - 3600,)
            ).fetchone()[0]
        if session is not None:
            roots = _task_roots(config, session)
            placeholders = ",".join("?" for _ in roots)
            rows = db.execute(
                f"SELECT substr(id,1,129) AS id,substr(status,1,64) AS status,substr(detail,1,1000) AS detail FROM captures "
                f"WHERE session=? OR root_session IN ({placeholders}) ORDER BY created DESC LIMIT 5",
                (session, *roots),
            )
            result["captures"] = [dict(id=row["id"], status=row["status"] if row["status"] in _CAPTURE_STATUSES else "unknown", **failure(row["detail"]))
                                   for row in rows if isinstance(row["id"], str) and _SAFE_ID.fullmatch(row["id"])]
            # JSON is inspected inside SQLite, not copied into Python. The time
            # progress handler bounds history scans; invalid/oversized payloads
            # cannot prove ownership through entry_refs. Votes remain independent.
            rows = db.execute(f"""
                WITH task_owners AS (
                    SELECT opaque FROM opaque_roots WHERE root_hash IN ({placeholders})
                )
                SELECT substr(o.batch_id,1,129) AS batch_id,substr(o.status,1,64) AS status,
                       substr(o.pr_url,1,1024) AS pr_url,substr(o.detail,1,1000) AS detail
                FROM outbox o WHERE
                  EXISTS (SELECT 1 FROM votes v WHERE v.batch_id=o.batch_id
                          AND v.root_opaque IN (SELECT opaque FROM task_owners))
                  OR EXISTS (
                    SELECT 1 FROM json_each(
                      CASE WHEN length(CAST(o.batch AS BLOB))<=4194304
                           THEN CASE WHEN json_valid(o.batch) THEN o.batch ELSE '{{}}' END
                           ELSE '{{}}' END, '$.entry_refs') ref
                    JOIN owners own ON ref.value LIKE 'mindie://' || ? || '/' || own.entry_id || '@%'
                    WHERE own.owner IN (SELECT opaque FROM task_owners)
                  )
                ORDER BY o.updated DESC LIMIT 5
            """, (*roots, config["domain"]))
            result["contributions"] = [
                dict(batch_id=row["batch_id"], status=row["status"] if row["status"] in _BATCH_STATUSES else "unknown",
                     pr_url=row["pr_url"] if isinstance(row["pr_url"], str) and _PR_URL.fullmatch(row["pr_url"]) else None,
                     **failure(row["detail"]))
                for row in rows if isinstance(row["batch_id"], str) and _SAFE_ID.fullmatch(row["batch_id"])
            ]
        result["store"] = dict(status="present")
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        result["store"] = dict(status="unavailable", error_class=error_class(exc))
    finally:
        if db is not None:
            db.close()


def snapshot(config_path, *, session=None):
    """Read existing local state, scoped to the caller's established native task."""
    result = dict(configuration=dict(status="unavailable"), service=dict(status="unknown"),
                  store=dict(status="unknown"), admission=dict(status="missing", enabled=False),
                  maintenance=dict(status="unknown", scope="shared"), captures=[], contributions=[],
                  startup=dict(status="absent"), delivery=dict(status="absent"), hints=[])
    try:
        if session is not None:
            session_key(session)  # Validate; the native identity itself is never returned.
        from .cli import validate_config
        config = validate_config(_json(config_path))
        from .documents import DOMAIN_RE
        if not isinstance(config["root"], str) or not isinstance(config["domain"], str) or not DOMAIN_RE.fullmatch(config["domain"]):
            raise ValueError("invalid diagnostic configuration")
        result["configuration"] = dict(status="ok")
    except (OSError, ValueError, TypeError) as exc:
        result["configuration"] = dict(status="absent" if isinstance(exc, FileNotFoundError) else "unavailable", error_class=error_class(exc))
        result["hints"] = ["Inspect the existing configured component or installation; continue native or independent remote work."]
        return result
    if session is not None and config.get("admission_path"):
        from .activation import Admission
        try:
            result["admission"] = Admission(config["admission_path"]).inspect(session)
        except (OSError, ValueError) as exc:
            result["admission"] = dict(status="unavailable", enabled=False, error_class=error_class(exc))
    result["service"] = _service(config)
    _store(config, session, result)
    try:
        result["startup"] = _startup(config_path, config)
    except (OSError, ValueError) as exc:
        result["startup"] = dict(status="unavailable", error_class=error_class(exc))
    try:
        result["delivery"] = _delivery(config)
    except (OSError, ValueError) as exc:
        result["delivery"] = dict(status="unavailable", error_class=error_class(exc))
    if result["delivery"].get("status") == "unresolved" and result["delivery"].get("recovery"):
        result["hints"].append(result["delivery"]["recovery"])
    if result["admission"].get("status") == "paused":
        result["hints"].append("Task admission is paused. Diagnose and fix its cause before explicit deactivate/reactivate; consumed attempts are not replayed.")
    if result["maintenance"].get("paused"):
        result["hints"].append("Shared maintenance is paused. Diagnose and fix its cause before explicit maintenance-resume; failed input is not replayed.")
    if result["contributions"]:
        result["hints"].append("Use this task's contributions batch_id for recover inspect; reconcile unknown writes before any explicit retry.")
    if result["startup"].get("status") == "failed":
        result["hints"].append("Inspect the reported configured startup component and installation.")
    if result["service"].get("status") != "running" or result["store"].get("status") == "unavailable":
        result["hints"].append("Knowledge is unavailable; continue native/local or independent remote business work while inspecting the reported stage and class. Status does not retry work.")
    return result
