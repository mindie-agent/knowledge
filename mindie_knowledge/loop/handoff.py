"""[Role] Fast Stop-hook capture admission and single-flight background service wake (`handoff`).

[Invariants]
- Stop-hook execution must stay lightweight: it checks `CommunitySettings`,
  `Admission` lease, and existing `state-v4.sqlite3` schema (`SchemaNotReady` if
  uninitialized), then inserts a pending row into `captures` without opening
  transcripts, migrating storage, or calling any LLM.
- Deduplicates events via `_event_hash(harness, kind, session, key)` and durable
  `Admission.claim`.
- Service probe treats a missing or released `consumer.lock` as absent; leftover
  `connection.json` is never live-owner proof.

[Timeouts & Locks]
- Uses `STOP_TIMEOUT_SECONDS = 15.0` (`>= 6.0s` admission + `6.0s` store + `2.0s`
  `WAKE_PROBE_SECONDS` + `1.0s` wake/diagnostics grace) and non-blocking
  `StartLock(wake.lock, wait=0)` so concurrent Stop hooks never pile up.
- Stop SQLite opens apply the unified `PRAGMA busy_timeout=5000` (`5.0s` lock floor)
  once without dynamic overrides; remaining caller budgets below `5.0s` return
  `unavailable`/`budget-exhausted` before SQLite open or transaction start.

[Side-Effect Policy]
- Writes at most one `captures` row and spawns at most one detached background
  service starter via `service_host`.
"""

from __future__ import annotations

from ..state_layout import state_root
from ..filesystem import filesystem_path

import json
import math
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from ..owned_state import (
    SQLITE_BUSY_FLOOR_SECONDS,
    SQLITE_BUSY_TIMEOUT_MS,
    SqliteBudgetExhausted,
    sqlite_busy_timeout_ms,
)
from .activation import Admission, AdmissionUnavailable
from .diagnostics import _DELIVERY_RECOVERY as _RECOVERY
from .store import (
    _UNPROCESSED_CAPTURE,
    commit_capture,
    digest,
    session_key,
)

WAKE_PROBE_SECONDS = 2.0  # Control-plane observation; never a business deadline.
STOP_TIMEOUT_SECONDS = 15.0  # >= 6s admission + 6s store + 2s wake probe + 1s grace.


def _wake_probe_seconds():
    raw = getattr(os, "environ", {}).get("MINDIE_KNOWLEDGE_WAKE_PROBE_SECONDS")
    return float(raw) if raw is not None else WAKE_PROBE_SECONDS


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
    result = {key: value[key] for key in _KEYS}
    if kwargs.get("host") is not None:
        result["host"] = kwargs["host"]
    return result


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
    return state_root(config["root"], config["domain"]) / "state-v4.sqlite3"


def _accept_row(db, *, namespace, root_hash, session, turn, transcript, summary,
                generation, boundary, scope, epoch, kind, event_key):
    db.execute("BEGIN IMMEDIATE")
    try:
        result = commit_capture(
            db, namespace=namespace, root_session=root_hash, session=session,
            turn=turn, transcript=transcript, summary=summary,
            generation=generation, boundary=boundary, scope=scope,
            activation_epoch=epoch, kind=kind, event_key=event_key,
        )
        db.commit()
        return result
    except sqlite3.OperationalError as exc:
        db.rollback()
        text = str(exc).lower()
        if "no such table" in text or "no such column" in text:
            raise SchemaNotReady() from exc
        raise
    except Exception:
        db.rollback()
        raise


def _probe(config, timeout, *, config_path=None):
    from .cli import connect
    from .locks import lock_held
    from .transport import rpc

    # The service holds this OS lock until storage and listener are closed.
    # Windows may report a closed loopback port as a timeout for ~2 seconds;
    # a released or missing lifetime lock is stronger evidence than a short
    # TCP probe or a leftover connection.json fingerprint mismatch.
    consumer = filesystem_path(config["root"]) / config["domain"] / "consumer.lock"
    owner = lock_held(consumer)
    if owner is None:
        try:
            consumer.stat()
        except FileNotFoundError:
            owner = False
        except TimeoutError:
            raise
        except OSError:
            return 'unknown'
        else:
            return 'unknown'
    if owner is False:
        return "absent"
    try:
        connection = connect(config, config_path=config_path)
    except FileNotFoundError:
        return "starting" if owner is True else "absent"
    except PermissionError:
        # Windows CRT reports a concurrent atomic replace as EACCES.
        return "starting" if owner is True else "absent"
    except TimeoutError:
        raise
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
            return "starting" if owner is True else "absent"
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
    except FileNotFoundError:
        return {}
    return raw if isinstance(raw, dict) else {}


def request_wake(config_path, *, session_id, event=None, timeout=None):
    """Spawn at most one detached service starter. Does not create a capture."""
    from . import settings as settings_mod
    from .cli import config_at
    from .locks import StartInProgress, StartLock
    from .lifecycle import require_active, ServiceRetired

    out = dict(wake="not-requested", runtime="not-applicable", reason="not-requested", recovery=None)
    if not isinstance(session_id, str) or not session_id.strip():
        out.update(wake="not-applicable", reason="not-activated")
        return out
    try:
        config = config_at(config_path)
        require_active(config_path, config)
    except ServiceRetired:
        out.update(wake='not-applicable', runtime='retired', reason='service-retired')
        return out
    except TimeoutError:
        raise
    except (OSError, ValueError):
        out.update(wake="failed", runtime="unavailable", reason="configuration")
        return out
    settings = settings_mod.from_engine_config(config)
    if not settings.allows_capture():
        if settings.capture_block_kind() == 'fault':
            from .dfx import failure
            failure('knowledge.capture', stage='authorization', category='authority_unavailable', reportable=False)
            out.update(wake='failed', runtime='unavailable', reason='authority-unavailable')
            return out
        out.update(wake="not-applicable", reason="sharing-disabled")
        return out
    path = config.get("admission_path")
    if not isinstance(path, str):
        out.update(wake="not-applicable", reason="not-activated")
        return out
    try:
        lease = Admission(path).active_lease(session_id)
    except TimeoutError:
        raise
    except (OSError, ValueError, AdmissionUnavailable):
        out.update(wake="failed", runtime="unavailable", reason="admission-unreadable")
        return out
    if lease is None:
        inspected = Admission(path).inspect(session_id)
        if inspected.get("status") == "unavailable":
            out.update(wake="failed", runtime="unavailable", reason="admission-unreadable")
            return out
        out.update(wake="not-applicable", reason="not-activated")
        return out
    state = _probe(config, _wake_probe_seconds(), config_path=config_path)
    if state == "ready":
        out.update(wake="running", runtime="ready", reason="worker-ready")
        return out
    if state != "absent":
        runtime = "unavailable" if state == "worker-dead" else "unknown"
        out.update(wake="not-requested", runtime=runtime, reason=state)
        return out
    lock = StartLock(_store_path(config).with_name("wake.request.lock"))
    try:
        # Other callers keep the historical non-blocking probe; only an
        # explicit Stop budget may wait for the single wake starter.
        lock.acquire(wait=0 if timeout is None else timeout)
    except StartInProgress:
        out.update(wake="coalesced", runtime="unavailable", reason="coalesced")
        return out
    try:
        require_active(config_path, config)
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
            from .service_host import platform_backend
            backend = platform_backend()
            if backend is not None and os.name == "nt":
                from .cli import connection_path
                start = StartLock(connection_path(config).with_name("start.lock"))
                try:
                    start.acquire(wait=0 if timeout is None else timeout)
                except StartInProgress:
                    out.update(wake="coalesced", runtime="unavailable", reason="coalesced")
                    return out
                try:
                    # A query starter may have won while the wake lock was
                    # acquired; the scheduler never replaces a live consumer.
                    current = _probe(config, _wake_probe_seconds(), config_path=config_path)
                    if current == "ready":
                        out.update(wake="running", runtime="ready", reason="worker-ready")
                        return out
                    if current != "absent":
                        out.update(wake="not-requested", runtime="unknown", reason=current)
                        return out
                    process = backend.request_start(config_path, config)
                finally:
                    start.release()
            else:
                from .process import spawn_service
                process = spawn_service(argv)
        except TimeoutError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            out.update(wake="failed", runtime="unavailable", reason="wake-failed",
                       recovery=_RECOVERY["wake-failed"])
            from .service_host import ServiceHostError
            if isinstance(exc, ServiceHostError):
                out["host"] = dict(stage=exc.stage, code=exc.code, uncertain=exc.uncertain)
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
    """Detached owner of the same readiness operation used by normal calls."""
    from .cli import config_at, ensure_service
    from .dfx import failure
    from .diagnostics import clear_delivery_event, record_delivery_failure
    from .lifecycle import ServiceRetired

    try:
        config = config_at(config_path)
    except TimeoutError:
        raise
    except (OSError, ValueError) as exc:
        print(
            f"mindie-knowledge wake: configuration {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        failure(
            "knowledge.capture", stage="wake",
            category="configuration_unavailable", reportable=False,
        )
        return 2
    try:
        ensure_service(config_path, _from_detached_starter=True)
    except ServiceRetired:
        return 0  # An already-spawned old helper is expected after retirement.
    except TimeoutError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"mindie-knowledge wake: {type(exc).__name__}", file=sys.stderr)
        record_delivery_failure(config, stage="wake", cause="wake-failed", event=event)
        return 2
    if event:
        clear_delivery_event(config, event)
    return 0


def _record(config, **kwargs):
    from .diagnostics import record_delivery_failure

    try:
        record_delivery_failure(config, **kwargs)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        pass


_HOOK_ERRORS = (ValueError, TypeError, AttributeError, OSError, RecursionError)


def _record_hook_failure(config_path, cause, event=None):
    from .cli import config_at
    from .dfx import failure
    from .diagnostics import record_delivery_failure

    try:
        config = config_at(config_path)
        record_delivery_failure(config, stage="accept", cause=cause, event=event)
    except _HOOK_ERRORS:
        failure(
            "knowledge.capture", stage="accept",
            category=str(cause).replace("-", "_"), reportable=False,
        )


def run_stop_hook(config_path):
    """Native Stop adapter: record failures, print ``{}``, return 0."""
    from .transport import MAX_BODY

    event = None
    try:
        raw = sys.stdin.buffer.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise ValueError("Stop hook stdin exceeds the 2 MiB transport limit")
        payload = json.loads(raw)
        kind, key = _identity(payload)
        session = payload.get("session_id")
        if kind and _text(session, 256) and _text(key, 256):
            event = _event_hash(payload.get("harness") or "", kind, session, key)
        result = accept_stop(config_path, payload)
        if isinstance(result, dict) and result.get("stage") in {"rejected", "unavailable"}:
            _record_hook_failure(
                config_path, result.get("cause") or result.get("reason") or "internal",
                event=event,
            )
            print(
                f"mindie-knowledge hook: {result.get('stage')}: {result.get('reason')}",
                file=sys.stderr,
            )
    except TimeoutError as exc:
        print(f"mindie-knowledge hook: TimeoutError: {exc}", file=sys.stderr)
        _record_hook_failure(config_path, "budget-exhausted", event=event)
    except _HOOK_ERRORS as exc:
        print(f"mindie-knowledge hook: {type(exc).__name__}: {exc}", file=sys.stderr)
        _record_hook_failure(config_path, "internal", event=event)
    print("{}")
    return 0


def accept_stop(config_path, event, *, timeout=None):
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
    for key in ("transcript_path", "cwd"):
        value = event.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or "\x00" in value:
            return _result(stage="rejected", reason="invalid-envelope")
    harness = event.get("harness") or ""
    if harness and (not isinstance(harness, str) or not _HARNESS.fullmatch(harness)):
        return _result(stage="rejected", reason="invalid-envelope")
    raw_summary = event.get("last_assistant_message")
    transcript = event.get("transcript_path") or None
    if raw_summary is not None and not isinstance(raw_summary, str):
        return _result(stage="rejected", reason="invalid-envelope")
    try:
        config = config_at(config_path)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        from .dfx import failure
        failure('knowledge.capture', stage='handoff', category='configuration_unavailable')
        return _result(stage="unavailable", reason="configuration", cause="internal")
    settings = settings_mod.from_engine_config(config)
    if not settings.allows_capture():
        if settings.capture_block_kind() == 'fault':
            from .dfx import failure
            failure('knowledge.capture', stage='authorization', category='authority_unavailable')
            return _result(stage='unavailable', reason='authority-unavailable',
                           cause='authority-unavailable', summary_dropped=dropped)
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
        if transcript is None:
            return _result(stage="rejected", reason="no-capturable-material")
        # config_at permits only public-transcript, including the omitted
        # default. Selection/redaction belongs to the worker; persist only
        # the native source reference, never another raw copy of its answer.
        summary = ""
        dropped = bool(raw_summary)
    turn = key if kind == "turn" else ""
    event_key = key if kind == "notification" else None
    event_id = hashed
    token = event.get("mindie_activation")
    resolve_lease = kind == "notification" and token in (None, "")
    if not resolve_lease and not _text(token, 512):
        return _result(stage="inert", reason="not-activated", summary_dropped=dropped)
    admission_path = config.get("admission_path")
    if not isinstance(admission_path, str):
        return _result(stage="inert", reason="not-activated", summary_dropped=dropped)
    stop_timeout = (
        float(timeout)
        if timeout is not None
        else float(os.environ.get("MINDIE_KNOWLEDGE_STOP_TIMEOUT_SECONDS", STOP_TIMEOUT_SECONDS))
    )
    if not math.isfinite(stop_timeout):
        raise ValueError("Stop timeout must be a finite number")
    deadline = time.monotonic() + stop_timeout
    remaining = deadline - time.monotonic()
    if sqlite_busy_timeout_ms(remaining) is None:
        _record(config, stage="accept", cause="budget-exhausted", event=event_id)
        return _result(
            stage="unavailable", reason="budget-exhausted", cause="budget-exhausted",
            summary_dropped=dropped,
            recovery=_RECOVERY["budget-exhausted"],
        )
    auth_timeout = min(6.0, remaining - 6.0) if remaining >= 11.0 else remaining
    try:
        admission = Admission(admission_path)
        if resolve_lease:
            auth = admission.resolve_capture_lease(session, timeout=auth_timeout)
        else:
            auth = admission.capture_authorization(
                session, token, timeout=auth_timeout,
            )
    except TimeoutError:
        try:
            _record(config, stage="accept", cause="budget-exhausted", event=event_id)
        except TimeoutError:
            pass
        raise
    except AdmissionUnavailable as exc:
        cause = (
            "budget-exhausted"
            if isinstance(exc.__cause__, SqliteBudgetExhausted)
            or "exceeded caller timeout" in str(exc)
            or time.monotonic() >= deadline
            else "admission-unreadable"
        )
        _record(config, stage="accept", cause=cause, event=event_id)
        return _result(
            stage="unavailable", reason=cause, cause=cause,
            summary_dropped=dropped,
            recovery=_RECOVERY[cause],
        )
    if auth["state"] == "inactive":
        return _result(stage="inert", reason="not-activated", summary_dropped=dropped)
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
    db = None
    try:
        if sqlite_busy_timeout_ms(deadline - time.monotonic()) is None:
            _record(config, stage="accept", cause="budget-exhausted", event=event_id)
            return _result(
                stage="unavailable", reason="budget-exhausted", cause="budget-exhausted",
                summary_dropped=dropped,
                recovery=_RECOVERY["budget-exhausted"],
            )
        db = sqlite3.connect(path, timeout=SQLITE_BUSY_FLOOR_SECONDS)
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        db.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        if sqlite_busy_timeout_ms(deadline - time.monotonic()) is None:
            db.set_progress_handler(None, 0)
            db.close()
            db = None
            _record(config, stage="accept", cause="budget-exhausted", event=event_id)
            return _result(
                stage="unavailable", reason="budget-exhausted", cause="budget-exhausted",
                summary_dropped=dropped,
                recovery=_RECOVERY["budget-exhausted"],
            )
    except sqlite3.OperationalError as exc:
        if db is not None:
            db.set_progress_handler(None, 0)
            db.close()
        text = str(exc).lower()
        if time.monotonic() >= deadline or "interrupted" in text:
            _record(config, stage="accept", cause="budget-exhausted", event=event_id)
            return _result(
                stage="unavailable", reason="budget-exhausted", cause="budget-exhausted",
                summary_dropped=dropped,
                recovery=_RECOVERY["budget-exhausted"],
            )
        if "locked" in text or "busy" in text:
            _record(config, stage="accept", cause="store-locked", event=event_id)
            return _result(
                stage="unavailable", reason="store-locked", cause="store-locked",
                summary_dropped=dropped,
                recovery=_RECOVERY["store-locked"],
            )
        _record(config, stage="accept", cause="store-not-ready", event=event_id)
        return _result(stage="unavailable", reason="store-not-ready", cause="store-not-ready",
                       summary_dropped=dropped)
    except sqlite3.Error:
        if db is not None:
            db.set_progress_handler(None, 0)
            db.close()
        _record(config, stage="accept", cause="store-not-ready", event=event_id)
        return _result(stage="unavailable", reason="store-not-ready", cause="store-not-ready",
                       summary_dropped=dropped)
    db.row_factory = sqlite3.Row
    try:
        try:
            activated = auth.get("activated_at")
            boundary = max(
                settings.enabled_at or 0,
                activated if type(activated) in (int, float) else 0,
            )
            accepted = _accept_row(
                db, namespace=harness, root_hash=session_key(auth["root_session"]),
                session=session, turn=turn, transcript=transcript, summary=summary,
                generation=settings.generation, boundary=boundary, scope=auth["scope"],
                epoch=auth["epoch"], kind=kind, event_key=event_key,
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
            if time.monotonic() >= deadline or "interrupted" in text:
                _record(config, stage="accept", cause="budget-exhausted", event=event_id)
                return _result(
                    stage="unavailable", reason="budget-exhausted", cause="budget-exhausted",
                    summary_dropped=dropped,
                    recovery=_RECOVERY["budget-exhausted"],
                )
            if "locked" in text or "busy" in text:
                _record(config, stage="accept", cause="store-locked", event=event_id)
                return _result(
                    stage="unavailable", reason="store-locked", cause="store-locked",
                    summary_dropped=dropped,
                    recovery=_RECOVERY["store-locked"],
                )
            raise
    finally:
        db.set_progress_handler(None, 0)
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
    probe_budget = min(_wake_probe_seconds(), deadline - time.monotonic())
    if status == "processing":
        state = _probe(config, probe_budget, config_path=config_path) if probe_budget > 0 else "unknown"
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
    state = _probe(config, probe_budget, config_path=config_path) if probe_budget > 0 else "unknown"
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
    if state == "absent":
        wake = request_wake(
            config_path, session_id=session, event=event_id,
            timeout=max(0.0, deadline - time.monotonic()),
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
        host=wake.get("host"),
    )
