"""Short Stop handoff into the existing capture table, plus one service wake.

Stop does not migrate the store, open a transcript, or call a model.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from .activation import Admission, AdmissionUnavailable
from .diagnostics import _DELIVERY_RECOVERY as _RECOVERY
from .store import (
    _UNPROCESSED_CAPTURE,
    commit_capture,
    digest,
    session_key,
)

SUMMARY_LIMIT = 32768
HOOK_BUDGET = 1.5
_HARNESS = __import__("re").compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_TERMINAL = {
    "organized": "organized",
    "no-new-material": "no-new-material",
    "no-shareable-material": "no-shareable-material",
    "failed": "failed",
    "cancelled": "cancelled",
    "discarded": "discarded",
}
_KEYS = (
    "stage", "capture_id", "duplicate", "wake", "runtime", "reason",
    "cause", "summary_dropped", "recovery",
)


class SchemaNotReady(Exception):
    pass


class StoreNotReady(Exception):
    pass


class StoreLocked(Exception):
    pass


def _result(**kwargs):
    value = dict(
        stage="inert", capture_id=None, duplicate=False, wake="not-applicable",
        runtime="not-applicable", reason="not-stop", cause=None,
        summary_dropped=False, recovery=None,
    )
    value.update(kwargs)
    return {key: value[key] for key in _KEYS}


def _budget(event):
    raw = event.get("budget_seconds", 0.4) if isinstance(event, dict) else 0.4
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.4
    if raw <= 0 or raw != raw or raw == float("inf"):
        return 0.4
    return min(HOOK_BUDGET, float(raw))


def _event_hash(harness, kind, session, key):
    return digest(["handoff-event", kind, harness, session, key])


def _identity(event):
    """Return (kind, key) or (None, 'invalid'|'missing')."""
    kind = event.get("identity_kind")
    if kind not in {"turn", "notification"}:
        return None, "invalid"
    turn = event.get("turn_id")
    event_id = event.get("event_id")
    if kind == "turn":
        if event_id not in (None, ""):
            return None, "invalid"
        if not _text(turn, 256):
            return None, "missing"
        return "turn", turn
    if turn not in (None, ""):
        return None, "invalid"
    if not _text(event_id, 256):
        return None, "missing"
    return "notification", event_id


def _text(value, limit):
    return isinstance(value, str) and "\x00" not in value and 0 < len(value) <= limit


def prepare_schema(config_path):
    """Open the domain store so install, query, activation, and update migrate it.

    Stop does not call this.
    """
    from .cli import config_at
    from .store import Store

    config = config_at(config_path)
    store = Store(config["root"], config["domain"])
    store.close()
    return config


def _store_path(config):
    return Path(config["root"]).resolve() / config["domain"] / "store-v3.sqlite3"


def _columns(db, table):
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _required_schema(db):
    needed = {
        "id", "root_session", "session", "turn", "transcript", "summary",
        "status", "detail", "created", "generation", "boundary", "scope",
        "activation_epoch", "identity_kind", "event_key",
    }
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if not {"captures", "meta", "continuations", "state"} <= tables:
        raise SchemaNotReady()
    if not needed <= _columns(db, "captures"):
        raise SchemaNotReady()
    if "eligible" not in _columns(db, "continuations"):
        raise SchemaNotReady()
    if "capture_floor" not in {
        row[0] for row in db.execute("SELECT key FROM meta")
    }:
        raise SchemaNotReady()


def _accept_row(db, *, namespace, root_hash, session, turn, transcript, summary,
                generation, boundary, scope, epoch, paused, kind, event_key):
    _required_schema(db)
    floor = float(db.execute(
        "SELECT value FROM meta WHERE key='capture_floor'"
    ).fetchone()[0])
    boundary = max(boundary, floor)
    db.execute("BEGIN IMMEDIATE")
    try:
        held = "maintenance-paused" if paused else None
        result = commit_capture(
            db, namespace=namespace, root_session=root_hash, session=session,
            turn=turn, transcript=transcript, summary=summary,
            generation=generation, boundary=boundary, scope=scope,
            activation_epoch=epoch, hold=held, kind=kind, event_key=event_key,
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def _paused(db):
    try:
        return db.execute(
            "SELECT 1 FROM state WHERE key='maintenance_paused'"
        ).fetchone() is not None
    except sqlite3.Error as exc:
        raise SchemaNotReady() from exc


def _probe(config, timeout):
    from .cli import connect
    from .transport import rpc

    try:
        connection = connect(config)
    except FileNotFoundError:
        return "absent"
    except (OSError, ValueError):
        return "unknown"
    try:
        status = rpc(connection, "status", timeout=timeout)
    except FileNotFoundError:
        return "absent"
    except TimeoutError:
        return "unknown"
    except OSError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(exc, ConnectionRefusedError) or isinstance(reason, ConnectionRefusedError):
            return "absent"
        return "unknown"
    except (ValueError, RuntimeError):
        return "unknown"
    if not isinstance(status, dict):
        return "unknown"
    if status.get("worker_failed") is True or status.get("worker_alive") is False:
        return "worker-dead"
    if status.get("worker_alive") is True and status.get("admission_frozen") is not True:
        return "ready"
    if status.get("admission_frozen") is True:
        return "stopping"
    return "unknown"


def _wake_file(config):
    return _store_path(config).with_name("wake.json")


def _read_wake(config):
    try:
        raw = json.loads(_wake_file(config).read_text() or "{}")
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def request_wake(config_path, *, budget_seconds=0.4, session_id, event=None):
    """Spawn at most one detached service starter. Does not create a capture."""
    from . import settings as settings_mod
    from .cli import config_at
    from .locks import StartInProgress, StartLock

    out = dict(wake="not-requested", runtime="not-applicable", reason="not-requested", recovery=None)
    if not isinstance(session_id, str) or not session_id.strip():
        out.update(wake="not-applicable", reason="not-activated")
        return out
    try:
        config = config_at(config_path)
    except (OSError, ValueError):
        out.update(wake="failed", runtime="unavailable", reason="configuration")
        return out
    settings = settings_mod.from_engine_config(config)
    if not settings.allows_capture():
        out.update(wake="not-applicable", reason="sharing-disabled")
        return out
    path = config.get("admission_path")
    if not isinstance(path, str):
        out.update(wake="not-applicable", reason="not-activated")
        return out
    try:
        lease = Admission(path).active_lease(session_id)
    except (OSError, ValueError):
        lease = None
    if lease is None:
        inspected = Admission(path).inspect(session_id)
        if inspected.get("status") == "unavailable":
            out.update(wake="failed", runtime="unavailable", reason="admission-unreadable")
            return out
        out.update(wake="not-applicable", reason="not-activated")
        return out
    remaining = _budget(dict(budget_seconds=budget_seconds))
    if remaining < 0.05:
        out.update(runtime="unknown", reason="budget-exhausted")
        return out
    state = _probe(config, min(0.15, remaining / 2))
    if state == "ready":
        out.update(wake="running", runtime="ready", reason="worker-ready")
        return out
    if state != "absent":
        runtime = "unavailable" if state == "worker-dead" else "unknown"
        out.update(wake="not-requested", runtime=runtime, reason=state)
        return out
    lock = StartLock(_store_path(config).with_name("wake.request.lock"))
    try:
        lock.acquire()
    except StartInProgress:
        out.update(wake="coalesced", runtime="unavailable", reason="coalesced")
        return out
    try:
        # wake.json pids are diagnostic. Ownership is wake.request.lock here
        # and start.lock / consumer.lock in the helper. A reused pid is not
        # a running service and must not suppress this wake.
        argv = [
            sys.executable, "-m", "mindie_knowledge.loop.cli", "wake",
            "--config", str(Path(config_path).resolve()),
        ]
        if isinstance(event, str) and len(event) == 64:
            argv.extend(["--event", event])
        try:
            process = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
            )
        except OSError:
            out.update(wake="failed", runtime="unavailable", reason="wake-failed",
                       recovery=_RECOVERY["wake-failed"])
            return out
        payload = {"wake_pid": process.pid, "at": time.time()}
        target = _wake_file(config)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload))
        out.update(wake="requested", runtime="unavailable", reason="requested")
        return out
    finally:
        lock.release()


def run_wake(config_path, event=None):
    """Detached one-shot starter. Not a retry loop and not an installer."""
    from .cli import config_at
    from .diagnostics import clear_delivery_event, record_delivery_failure
    from .locks import StartInProgress, StartLock
    from .process import terminate_tree

    try:
        config = config_at(config_path)
    except (OSError, ValueError):
        return 0
    lock = StartLock(_store_path(config).with_name("start.lock"))
    try:
        lock.acquire()
    except StartInProgress:
        return 0
    process = None
    ready = False
    try:
        state = _probe(config, 0.3)
        if state == "ready":
            ready = True
            if event:
                clear_delivery_event(config, event)
            return 0
        if state != "absent":
            if state == "worker-dead":
                record_delivery_failure(
                    config, stage="runtime", cause="worker-unavailable", event=event,
                )
            return 0
        process = subprocess.Popen(
            [sys.executable, "-m", "mindie_knowledge.loop.cli", "serve",
             "--config", str(Path(config_path).resolve())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
        )
        holder = _read_wake(config)
        holder["service_pid"] = process.pid
        _wake_file(config).write_text(json.dumps(holder))
        deadline = time.monotonic() + 5
        for _ in range(3):
            if process.poll() is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.5, remaining))
            if _probe(config, min(0.3, max(0.05, deadline - time.monotonic()))) == "ready":
                ready = True
                if event:
                    clear_delivery_event(config, event)
                return 0
        record_delivery_failure(config, stage="wake", cause="wake-failed", event=event)
        return 0
    finally:
        if process is not None and not ready:
            terminate_tree(process)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
        lock.release()


def _record(config, **kwargs):
    from .diagnostics import record_delivery_failure

    try:
        record_delivery_failure(config, **kwargs)
    except (OSError, ValueError):
        pass


def accept_stop(config_path, event):
    """Durable Stop handoff. See the shared contract for the returned stage."""
    from . import settings as settings_mod
    from .cli import config_at
    from .diagnostics import clear_delivery_event

    dropped = False
    if not isinstance(event, dict) or event.get("hook_event_name") != "Stop":
        return _result(stage="inert", reason="not-stop")
    if event.get("stop_hook_active", False) is not False:
        return _result(stage="inert", reason="recursive-stop")
    for key, limit in (
        ("session_id", 256), ("turn_id", 256), ("event_id", 256), ("mindie_activation", 512),
    ):
        value = event.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or "\x00" in value or len(value) > limit or value != value.strip():
            return _result(stage="rejected", reason="invalid-envelope")
    if event.get("identity_kind") is not None and not isinstance(event.get("identity_kind"), str):
        return _result(stage="rejected", reason="invalid-envelope")
    for key, limit in (("transcript_path", 4096), ("cwd", 4096)):
        value = event.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > limit or "\x00" in value:
            return _result(stage="rejected", reason="invalid-envelope")
    harness = event.get("harness") or ""
    if harness and (not isinstance(harness, str) or not _HARNESS.fullmatch(harness)):
        return _result(stage="rejected", reason="invalid-envelope")
    raw_summary = event.get("last_assistant_message")
    transcript = event.get("transcript_path") if _text(event.get("transcript_path"), 4096) else None
    if raw_summary is not None and not isinstance(raw_summary, str):
        return _result(stage="rejected", reason="invalid-envelope")
    deadline = time.monotonic() + _budget(event)
    try:
        config = config_at(config_path)
    except (OSError, ValueError):
        return _result(stage="unavailable", reason="configuration", cause="internal")
    settings = settings_mod.from_engine_config(config)
    if not settings.allows_capture():
        return _result(stage="inert", reason="sharing-disabled", summary_dropped=dropped)
    session = event.get("session_id")
    kind, key = _identity(event)
    if kind is None and key == "invalid":
        return _result(stage="rejected", reason="invalid-envelope", summary_dropped=dropped)
    hashed = (
        _event_hash(harness, kind, session, key)
        if kind and _text(session, 256) and _text(key, 256)
        else None
    )
    if not _text(session, 256) or kind is None:
        _record(config, stage="accept", cause="missing-identity", event=hashed)
        return _result(
            stage="rejected", reason="missing-identity", cause="missing-identity",
            summary_dropped=dropped,
            recovery=(
                "Native Stop must pass session_id and either a native turn_id "
                "(identity_kind turn) or an explicit notification event_id "
                "(identity_kind notification). Do not invent a turn id or read one "
                "from a transcript tail."
            ),
        )
    if kind == "notification":
        if isinstance(raw_summary, str) and raw_summary.strip():
            return _result(stage="rejected", reason="summary-forbidden", summary_dropped=False)
        summary = ""
        if transcript is None:
            return _result(stage="rejected", reason="no-capturable-material", summary_dropped=False)
    else:
        summary = raw_summary if isinstance(raw_summary, str) else ""
        if len(summary) > SUMMARY_LIMIT:
            if transcript is None:
                return _result(stage="rejected", reason="no-capturable-material")
            summary = ""
            dropped = True
        else:
            summary = summary.strip()
        if not summary and transcript is None:
            return _result(stage="rejected", reason="no-capturable-material", summary_dropped=dropped)
    turn = key if kind == "turn" else ""
    event_key = key if kind == "notification" else None
    event_id = hashed
    token = event.get("mindie_activation")
    resolve_lease = kind == "notification" and token in (None, "")
    if not resolve_lease and not _text(token, 512):
        return _result(stage="inert", reason="not-activated", summary_dropped=dropped)
    if time.monotonic() >= deadline:
        _record(config, stage="accept", cause="budget-exhausted", event=event_id)
        return _result(stage="unavailable", reason="budget-exhausted", cause="budget-exhausted",
                       summary_dropped=dropped,
                       recovery=_RECOVERY["budget-exhausted"])
    admission_path = config.get("admission_path")
    if not isinstance(admission_path, str):
        return _result(stage="inert", reason="not-activated", summary_dropped=dropped)
    try:
        admission = Admission(admission_path)
        auth_timeout = min(0.25, max(0.05, deadline - time.monotonic()))
        if resolve_lease:
            auth = admission.resolve_capture_lease(session, timeout=auth_timeout)
        else:
            auth = admission.capture_authorization(
                session, token, timeout=auth_timeout,
            )
    except AdmissionUnavailable:
        _record(config, stage="accept", cause="admission-unreadable", event=event_id)
        return _result(
            stage="unavailable", reason="admission-unreadable", cause="admission-unreadable",
            summary_dropped=dropped,
            recovery=_RECOVERY["admission-unreadable"],
        )
    if auth["state"] == "inactive":
        return _result(stage="inert", reason="not-activated", summary_dropped=dropped)
    if auth["state"] == "paused":
        return _result(stage="rejected", reason="admission-paused", summary_dropped=dropped,
                       recovery="Task admission is paused. Diagnose it, then deactivate and activate. This Stop was not captured.")
    if auth["state"] == "schema":
        return _result(stage="rejected", reason="admission-schema", summary_dropped=dropped)
    if not settings.in_scope(auth["scope"]):
        return _result(stage="inert", reason="out-of-scope", summary_dropped=dropped)
    path = _store_path(config)
    if not path.is_file():
        _record(config, stage="accept", cause="store-not-ready", event=event_id)
        return _result(
            stage="unavailable", reason="store-not-ready", cause="store-not-ready",
            summary_dropped=dropped,
            recovery=_RECOVERY["store-not-ready"],
        )
    timeout = min(0.25, max(0.05, (deadline - time.monotonic()) / 2))
    try:
        db = sqlite3.connect(path, timeout=timeout)
    except sqlite3.Error:
        _record(config, stage="accept", cause="store-not-ready", event=event_id)
        return _result(stage="unavailable", reason="store-not-ready", cause="store-not-ready",
                       summary_dropped=dropped)
    db.row_factory = sqlite3.Row
    try:
        try:
            paused = _paused(db)
            activated = auth.get("activated_at")
            boundary = max(
                settings.enabled_at or 0,
                activated if type(activated) in (int, float) else 0,
            )
            accepted = _accept_row(
                db, namespace=harness, root_hash=session_key(auth["root_session"]),
                session=session, turn=turn, transcript=transcript, summary=summary,
                generation=settings.generation, boundary=boundary, scope=auth["scope"],
                epoch=auth["epoch"], paused=paused, kind=kind, event_key=event_key,
            )
        except SchemaNotReady:
            _record(config, stage="accept", cause="schema-not-ready", event=event_id)
            return _result(
                stage="unavailable", reason="schema-not-ready", cause="schema-not-ready",
                summary_dropped=dropped,
                recovery=_RECOVERY["schema-not-ready"],
            )
        except sqlite3.OperationalError as exc:
            text = str(exc).lower()
            if "locked" in text or "busy" in text:
                _record(config, stage="accept", cause="store-locked", event=event_id)
                return _result(
                    stage="unavailable", reason="store-locked", cause="store-locked",
                    summary_dropped=dropped,
                    recovery=_RECOVERY["store-locked"],
                )
            raise
    finally:
        db.close()
    status = accepted["status"]
    capture_id = accepted["id"]
    duplicate = bool(accepted["duplicate"])
    if accepted.get("revoked"):
        return _result(
            stage="cancelled", capture_id=capture_id, duplicate=True,
            reason=accepted["revoked"], summary_dropped=dropped,
        )
    if status in _TERMINAL:
        clear_delivery_event(config, event_id)
        return _result(
            stage=_TERMINAL[status], capture_id=capture_id, duplicate=duplicate,
            reason="stored", summary_dropped=dropped,
        )
    if status == "processing":
        state = _probe(config, min(0.15, max(0.05, deadline - time.monotonic()))) if time.monotonic() < deadline else "unknown"
        return _result(
            stage="processing", capture_id=capture_id, duplicate=duplicate,
            wake="not-applicable",
            runtime="unavailable" if state != "ready" else "ready",
            reason="reserved", summary_dropped=dropped,
        )
    if status == "apply-pending":
        stage_name = "apply-pending"
    elif status in _UNPROCESSED_CAPTURE or status == "pending":
        stage_name = "accepted-local"
    else:
        stage_name = "accepted-local"
    if paused:
        return _result(
            stage="accepted-local", capture_id=capture_id, duplicate=duplicate,
            wake="not-requested", reason="maintenance-paused", summary_dropped=dropped,
            recovery=(
                "Shared maintenance is paused. Run maintenance-resume. This "
                "capture was recorded and the model was not called."
            ),
        )
    state = "unknown"
    if time.monotonic() < deadline:
        state = _probe(config, min(0.15, max(0.05, deadline - time.monotonic())))
    if state == "ready" and status in _UNPROCESSED_CAPTURE | {"pending", "deferred", "queued"}:
        clear_delivery_event(config, event_id)
        return _result(
            stage="accepted-runtime", capture_id=capture_id, duplicate=duplicate,
            wake="running", runtime="ready", reason="worker-ready", summary_dropped=dropped,
        )
    if status == "apply-pending" and state == "ready":
        clear_delivery_event(config, event_id)
        return _result(
            stage="apply-pending", capture_id=capture_id, duplicate=duplicate,
            wake="running", runtime="ready", reason="apply-pending", summary_dropped=dropped,
        )
    wake = dict(wake="not-requested", runtime="unknown" if state == "unknown" else "unavailable", reason="duplicate" if duplicate else "committed")
    if state == "absent" and time.monotonic() < deadline:
        wake = request_wake(
            config_path, budget_seconds=max(0.05, deadline - time.monotonic()),
            session_id=session, event=event_id,
        )
    elif state == "worker-dead":
        _record(config, stage="runtime", cause="worker-unavailable", capture_id=capture_id, event=event_id)
        wake = dict(wake="not-requested", runtime="unavailable", reason="worker-dead", recovery=None)
    if wake.get("wake") == "failed":
        _record(config, stage="wake", cause="wake-failed", capture_id=capture_id, event=event_id)
    if state == "ready":
        clear_delivery_event(config, event_id)
    return _result(
        stage="accepted-runtime" if wake.get("runtime") == "ready" and status in _UNPROCESSED_CAPTURE else stage_name,
        capture_id=capture_id, duplicate=duplicate,
        wake=wake.get("wake", "not-requested"),
        runtime=wake.get("runtime", "unavailable"),
        reason=wake.get("reason") or ("duplicate" if duplicate else "committed"),
        cause=None if wake.get("wake") != "failed" else "wake-failed",
        summary_dropped=dropped,
        recovery=wake.get("recovery"),
    )
