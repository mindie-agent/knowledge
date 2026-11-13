"""#332 knowledge-chapter regressions: no silent empty success, no sub-second SQLite."""
from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest

from mindie_knowledge.graded.explore import read_bounded_jsonl
from mindie_knowledge.loop import cli, handoff
from mindie_knowledge.loop.diagnostics import _delivery_path
from mindie_knowledge.loop.store import Store
from mindie_knowledge.loop.transport import MAX_BODY
from mindie_knowledge.owned_state import SQLITE_BUSY_TIMEOUT_MS, sqlite_busy_timeout_ms
from tests.conftest import admission_token, make_admission, write_settings
from lane_support import parser_path


def test_short_admission_read_keeps_busy_timeout_floor(tmp_path, monkeypatch):
    import re
    from mindie_knowledge import owned_state
    from mindie_knowledge.loop.activation import Admission, AdmissionUnavailable

    path = make_admission(tmp_path, project_root=tmp_path)
    gate = Admission(path)
    token = admission_token(path)
    statements = []
    real_connect = owned_state.sqlite3.connect

    def traced_connect(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(owned_state.sqlite3, "connect", traced_connect)
    with pytest.raises(AdmissionUnavailable):
        gate.capture_authorization("manual-A", token, timeout=0.1)
    assert statements == []

    assert gate.capture_authorization("manual-A", token, timeout=6.0)["state"] == "admitted"
    busy = []
    for statement in statements:
        match = re.search(r"^\s*PRAGMA\s+busy_timeout=(\d+)", statement, re.I)
        if match:
            busy.append(int(match.group(1)))
    assert busy and set(busy) == {SQLITE_BUSY_TIMEOUT_MS}


def test_sqlite_busy_timeout_never_goes_sub_second():
    assert sqlite_busy_timeout_ms(None) == SQLITE_BUSY_TIMEOUT_MS
    assert sqlite_busy_timeout_ms(0.05) is None
    assert sqlite_busy_timeout_ms(0.999) is None
    assert sqlite_busy_timeout_ms(1.0) is None
    assert sqlite_busy_timeout_ms(4.999) is None
    assert sqlite_busy_timeout_ms(5.0) == SQLITE_BUSY_TIMEOUT_MS
    assert sqlite_busy_timeout_ms(6.0) == SQLITE_BUSY_TIMEOUT_MS


def _engine_config(tmp_path, project):
    settings = tmp_path / "community.json"
    write_settings(settings, consent_choice="contribute", enabled=True, roots=[project])
    admission = make_admission(tmp_path, project_root=project)
    Store(tmp_path / "root", "test").close()
    path = tmp_path / "engine.json"
    path.write_text(json.dumps(dict(
        root=str(tmp_path / "root"), domain="test",
        community_config=str(settings), admission_path=str(admission),
        capture_mode="public-transcript",
        redactor_executable=str(tmp_path / "gitleaks"),
        transcript_adapter=str(parser_path("codex")),
    )), encoding="utf-8", newline="\n")
    return path, admission


def _stop_event(admission, **extra):
    event = dict(
        hook_event_name="Stop", identity_kind="turn", session_id="manual-A",
        turn_id="turn-1", harness="codex", mindie_activation=admission_token(admission),
        transcript_path=str(Path(admission).parent / "native.jsonl"),
    )
    event.update(extra)
    return event


def _stdin_buffer(monkeypatch, data):
    monkeypatch.setattr(cli.sys, "stdin", type("Stdin", (), {"buffer": io.BytesIO(data)})())


def test_stop_hook_cli_records_invalid_json_instead_of_empty_success(tmp_path, monkeypatch, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    config, _admission = _engine_config(tmp_path, project)
    _stdin_buffer(monkeypatch, b"{not-json")
    assert cli.main(["hook", "--config", str(config)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "{}"
    assert "JSON" in captured.err or "json" in captured.err
    payload = json.loads(_delivery_path(cli.config_at(config)).read_text(encoding="utf-8"))
    assert payload["status"] == "unresolved"
    assert payload["cause"] == "internal"
    assert payload["stage"] == "accept"


def test_stop_hook_cli_rejects_unbounded_stdin(tmp_path, monkeypatch, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    config, _admission = _engine_config(tmp_path, project)

    class Oversize:
        def read(self, n=-1):
            return b"x" * (MAX_BODY + 1 if n < 0 else n)

    monkeypatch.setattr(cli.sys, "stdin", type("Stdin", (), {"buffer": Oversize()})())
    assert cli.main(["hook", "--config", str(config)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "{}"
    assert "2 MiB" in captured.err
    payload = json.loads(_delivery_path(cli.config_at(config)).read_text(encoding="utf-8"))
    assert payload["cause"] == "internal"


def test_stop_hook_cli_records_rejected_envelope(tmp_path, monkeypatch, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    config, admission = _engine_config(tmp_path, project)
    event = _stop_event(admission, session_id=" leading-space")
    _stdin_buffer(monkeypatch, json.dumps(event).encode("utf-8"))
    assert cli.main(["hook", "--config", str(config)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "{}"
    assert "rejected" in captured.err
    payload = json.loads(_delivery_path(cli.config_at(config)).read_text(encoding="utf-8"))
    assert payload["status"] == "unresolved"
    assert payload["stage"] == "accept"


def test_short_stop_budget_is_exhausted_before_sub_second_sqlite_wait(tmp_path):
    import sqlite3
    import time

    project = tmp_path / "proj"
    project.mkdir()
    config, admission = _engine_config(tmp_path, project)
    store_path = tmp_path / "root" / "test" / "state-v1" / "state-v4.sqlite3"
    held = sqlite3.connect(store_path)
    held.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        result = cli.capture_hook(config, _stop_event(admission), timeout=0.1)
    finally:
        held.rollback()
        held.close()
    assert result["stage"] == "unavailable"
    assert result["reason"] == "budget-exhausted"
    assert result["capture_id"] is None
    assert time.monotonic() - started < 1.5


def test_run_wake_unreadable_config_is_not_success(tmp_path, capsys):
    missing = tmp_path / "missing-engine.json"
    assert handoff.run_wake(missing) == 2
    err = capsys.readouterr().err
    assert "configuration" in err
    assert "FileNotFoundError" in err or "not found" in err.lower() or "No such file" in err


def test_run_wake_invalid_config_is_not_success(tmp_path, capsys):
    path = tmp_path / "engine.json"
    path.write_text("{not-json", encoding="utf-8", newline="\n")
    assert handoff.run_wake(path) == 2
    assert "configuration" in capsys.readouterr().err


def test_missing_experiment_ledger_is_empty_history(tmp_path):
    assert read_bounded_jsonl(tmp_path / "logs" / "experiment-ledger.jsonl") == []


def test_corrupt_experiment_ledger_is_not_empty_history(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    path.write_text('{"run_id": "ok"}\nnot-json\n', encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match=r"trailing JSONL row 2 is not valid JSON"):
        read_bounded_jsonl(path)


def test_non_object_experiment_ledger_row_is_not_skipped(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    path.write_text('{"run_id": "ok"}\n[]\n', encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match=r"trailing JSONL row 2 must be an object"):
        read_bounded_jsonl(path)


def test_unreadable_experiment_ledger_is_not_empty_history(tmp_path, monkeypatch):
    path = tmp_path / "experiment-ledger.jsonl"
    path.write_text('{"run_id": "ok"}\n', encoding="utf-8", newline="\n")
    original = Path.open

    def denied(self, *args, **kwargs):
        if Path(self).resolve() == path.resolve():
            raise PermissionError(13, "denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(ValueError, match="cannot read experiment ledger"):
        read_bounded_jsonl(path)


def test_experiment_ledger_timeout_is_not_empty_history(tmp_path, monkeypatch):
    path = tmp_path / "experiment-ledger.jsonl"
    path.write_text('{"run_id": "ok"}\n', encoding="utf-8", newline="\n")
    error = TimeoutError("ledger read exceeded its remaining budget")
    original = Path.open

    def timed_out(self, *args, **kwargs):
        if Path(self).resolve() == path.resolve():
            raise error
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", timed_out)
    with pytest.raises(TimeoutError) as raised:
        read_bounded_jsonl(path)
    assert raised.value is error


def test_workspace_query_does_not_treat_corrupt_ledger_as_no_history(tmp_path, monkeypatch):
    from mindie_knowledge.graded.index import RoutingIndex
    from tests.test_graded_context_and_exploration import _unit

    premise = _unit(
        "unit-ledger-l1", "L1", "axiom", {"soc": ["Ascend910B"]},
        body="UB premise for workflow alpha.", path="_common",
    )
    workflow = _unit(
        "unit-ledger-l3", "L3", "workflow", {"soc": ["Ascend910B"]},
        parents=[premise.id], body="Baseline workflow alpha.",
    )
    task = tmp_path / "task"
    ledger = task / "logs" / "experiment-ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("not-json\n", encoding="utf-8", newline="\n")
    monkeypatch.setenv("MINDIE_BENCH_WORKSPACE", str(task))
    monkeypatch.delenv("MINDIE_DISABLE_RSI_EXPLORE", raising=False)
    with pytest.raises(ValueError, match="not valid JSON"):
        RoutingIndex([premise, workflow]).query(
            "workflow alpha", conditions={"soc": "Ascend910B"}, mode="reuse",
        )


def test_stop_hook_cli_keeps_host_protocol_when_config_reread_raises_typeerror(
    tmp_path, monkeypatch, capsys,
):
    project = tmp_path / "proj"
    project.mkdir()
    config, admission = _engine_config(tmp_path, project)
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["transcript_adapter"] = 123
    config.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    _stdin_buffer(monkeypatch, json.dumps(_stop_event(admission)).encode("utf-8"))
    assert cli.main(["hook", "--config", str(config)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "{}"
    assert captured.err
    assert "unavailable" in captured.err or "TypeError" in captured.err or "transcript_adapter" in captured.err


def test_stop_hook_cli_keeps_host_protocol_when_reporting_config_at_raises_typeerror(
    tmp_path, monkeypatch, capsys,
):
    project = tmp_path / "proj"
    project.mkdir()
    config, admission = _engine_config(tmp_path, project)

    def boom(_path):
        raise TypeError("expected str, not int")

    monkeypatch.setattr(cli, "config_at", boom)
    _stdin_buffer(monkeypatch, json.dumps(_stop_event(admission)).encode("utf-8"))
    assert cli.main(["hook", "--config", str(config)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "{}"
    assert "TypeError" in captured.err


def test_admission_timeout_propagates_from_capture_hook(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    config, admission = _engine_config(tmp_path, project)
    error = TimeoutError("admission read exceeded its remaining budget")

    def boom(self, *args, **kwargs):
        raise error

    monkeypatch.setattr(handoff.Admission, "capture_authorization", boom)
    with pytest.raises(TimeoutError) as raised:
        cli.capture_hook(config, _stop_event(admission), timeout=6.0)
    assert raised.value is error


def test_admission_timeout_is_preserved_when_receipt_write_times_out(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    config, admission = _engine_config(tmp_path, project)
    error = TimeoutError("admission read exceeded its remaining budget")

    def boom(self, *args, **kwargs):
        raise error

    def receipt_timeout(*args, **kwargs):
        raise TimeoutError("delivery receipt exceeded its remaining budget")

    monkeypatch.setattr(handoff.Admission, "capture_authorization", boom)
    monkeypatch.setattr(handoff, "_record", receipt_timeout)
    with pytest.raises(TimeoutError) as raised:
        cli.capture_hook(config, _stop_event(admission), timeout=6.0)
    assert raised.value is error


def test_stop_hook_cli_timeout_still_emits_host_protocol(tmp_path, monkeypatch, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    config, admission = _engine_config(tmp_path, project)
    error = TimeoutError("admission read exceeded its remaining budget")

    def boom(self, *args, **kwargs):
        raise error

    monkeypatch.setattr(handoff.Admission, "capture_authorization", boom)
    _stdin_buffer(monkeypatch, json.dumps(_stop_event(admission)).encode("utf-8"))
    assert cli.main(["hook", "--config", str(config)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "{}"
    assert "TimeoutError" in captured.err
    payload = json.loads(_delivery_path(cli.config_at(config)).read_text(encoding="utf-8"))
    assert payload["status"] == "unresolved"
    assert payload["cause"] == "budget-exhausted"
    assert payload.get("event")


def test_stop_hook_cli_keeps_delivery_event_correlation(tmp_path, monkeypatch, capsys):
    from mindie_knowledge.loop.diagnostics import clear_delivery_event

    project = tmp_path / "proj"
    project.mkdir()
    config, admission = _engine_config(tmp_path, project)
    monkeypatch.setenv("MINDIE_KNOWLEDGE_STOP_TIMEOUT_SECONDS", "0.1")
    event = _stop_event(admission)
    _stdin_buffer(monkeypatch, json.dumps(event).encode("utf-8"))
    assert cli.main(["hook", "--config", str(config)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "{}"
    loaded = cli.config_at(config)
    payload = json.loads(_delivery_path(loaded).read_text(encoding="utf-8"))
    assert payload["cause"] == "budget-exhausted"
    kind, key = handoff._identity(event)
    hashed = handoff._event_hash(event.get("harness") or "", kind, event["session_id"], key)
    assert payload.get("event") == hashed
    clear_delivery_event(loaded, hashed)
    assert not _delivery_path(loaded).exists()


def _trace_store_connect(monkeypatch, store_path, clock, wal_delay, pragmas, begins):
    real_connect = sqlite3.connect
    store = Path(store_path).resolve()

    def connect_wrapper(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        target = args[0] if args else kwargs.get("database")
        try:
            same = Path(str(target)).resolve() == store
        except OSError:
            same = False
        if not same:
            return db

        def tracer(sql):
            lowered = sql.lower().replace(" ", "")
            if lowered.startswith("pragmajournal_mode=wal"):
                clock["now"] += wal_delay
            if lowered.startswith("pragmabusy_timeout="):
                pragmas.append(lowered)
            if lowered.startswith("begin"):
                begins.append(sql)

        db.set_trace_callback(tracer)
        return db

    monkeypatch.setattr(sqlite3, "connect", connect_wrapper)
    monkeypatch.setattr(handoff.sqlite3, "connect", connect_wrapper)


def test_stop_store_busy_timeout_deducts_wal_wait(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    config, admission = _engine_config(tmp_path, project)
    loaded = cli.config_at(config)
    clock = {"now": 100.0}
    monkeypatch.setattr(handoff.time, "monotonic", lambda: clock["now"])
    pragmas, begins = [], []
    _trace_store_connect(
        monkeypatch, handoff._store_path(loaded), clock, 2.5, pragmas, begins,
    )
    result = cli.capture_hook(config, _stop_event(admission), timeout=7.0)
    assert result["stage"] == "unavailable"
    assert result["reason"] == "budget-exhausted"
    assert begins == []
    assert all("busy_timeout=5000" in item for item in pragmas)


def test_stop_store_busy_timeout_uses_remaining_after_wal(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    config, admission = _engine_config(tmp_path, project)
    loaded = cli.config_at(config)
    clock = {"now": 100.0}
    monkeypatch.setattr(handoff.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(handoff, "_probe", lambda *args, **kwargs: "ready")
    pragmas, begins = [], []
    _trace_store_connect(
        monkeypatch, handoff._store_path(loaded), clock, 1.5, pragmas, begins,
    )
    result = cli.capture_hook(config, _stop_event(admission), timeout=7.0)
    assert result["stage"] in {"accepted-local", "accepted-runtime", "accepted"}
    assert pragmas and all("busy_timeout=5000" in item for item in pragmas)
    assert begins


def test_truncated_utf8_ledger_tail_is_dropped_not_corruption(tmp_path):
    complete = (json.dumps({"run_id": "ok"}, ensure_ascii=False) + "\n").encode("utf-8")
    zhong = "中".encode("utf-8")
    rest = b'{"run_id": "' + zhong + b'"}\n'
    payload = complete + rest[: rest.index(zhong) + 1]
    path = tmp_path / "experiment-ledger.jsonl"
    path.write_bytes(payload)
    assert read_bounded_jsonl(path, max_bytes=len(payload)) == [{"run_id": "ok"}]


def test_open_database_and_admission_reads_enforce_floor_during_init_and_single_deadline(tmp_path, monkeypatch):
    import threading
    import time
    from mindie_knowledge import owned_state
    from mindie_knowledge.loop import activation
    from mindie_knowledge.loop.locks import StartLock

    connect_timeouts: list[float] = []
    pre_wal_busy: list[int] = []
    real_connect = owned_state.sqlite3.connect

    def recording_connect(*args, **kwargs):
        connect_timeouts.append(float(kwargs.get("timeout", 0.0)))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(owned_state.sqlite3, "connect", recording_connect)
    db_path = tmp_path / "subsec.sqlite3"
    with pytest.raises(owned_state.SqliteBudgetExhausted):
        owned_state.open_database(
            db_path,
            schema="test/1",
            required={"items": {"id"}},
            initialize=lambda conn: conn.execute("CREATE TABLE items(id TEXT PRIMARY KEY)"),
            timeout=0.1,
        )
    assert connect_timeouts == []
    assert not db_path.exists()

    db = owned_state.open_database(
        db_path,
        schema="test/1",
        required={"items": {"id"}},
        initialize=lambda conn: (
            pre_wal_busy.append(conn.execute("PRAGMA busy_timeout").fetchone()[0]),
            conn.execute("CREATE TABLE items(id TEXT PRIMARY KEY)"),
        ),
        timeout=6.0,
    )
    assert db is not None
    db.close()
    assert connect_timeouts == [owned_state.SQLITE_BUSY_FLOOR_SECONDS]
    assert pre_wal_busy == [owned_state.SQLITE_BUSY_TIMEOUT_MS]

    adm_path = make_admission(tmp_path, project_root=tmp_path)
    gate = activation.Admission(adm_path)
    token = admission_token(adm_path)

    # Real SQLite EXCLUSIVE lock contention: insufficient caller budget must
    # fail closed immediately before entering the 5000 ms SQLite busy wait.
    holder = real_connect(adm_path, check_same_thread=False)
    holder.execute("BEGIN EXCLUSIVE")
    release_timer = threading.Timer(0.4, holder.rollback)
    release_timer.start()
    try:
        started = time.monotonic()
        with pytest.raises(activation.AdmissionUnavailable):
            gate.capture_authorization("manual-A", token, timeout=0.1)
        assert time.monotonic() - started < 0.08
    finally:
        release_timer.join(timeout=2.0)
        holder.close()

    # Contended StartLock must only wait (deadline - 5.0s), never full timeout + 5.0s.
    held_init = StartLock(adm_path.with_name(adm_path.name + ".init.lock"))
    held_init.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(activation.AdmissionUnavailable):
            gate.capture_authorization("manual-A", token, timeout=5.15)
        assert time.monotonic() - started < 0.6
    finally:
        held_init.release()

    # Single-deadline enforcement when database open consumes budget below the 5.0s floor.
    clock = {"now": 50.0}
    monkeypatch.setattr(activation.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(owned_state.time, "monotonic", lambda: clock["now"])
    real_db = activation.Admission._database

    def slow_db(self, *args, **kwargs):
        conn = real_db(self, *args, **kwargs)
        clock["now"] += 1.5
        return conn

    monkeypatch.setattr(activation.Admission, "_database", slow_db)
    with pytest.raises(activation.AdmissionUnavailable, match="exceeded caller timeout"):
        gate.capture_authorization("manual-A", token, timeout=6.0)
    with pytest.raises(activation.AdmissionUnavailable, match="exceeded caller timeout"):
        gate.resolve_capture_lease("manual-A", timeout=6.0)


def test_routing_and_ledger_reject_directories_and_stat_permission_errors(tmp_path, monkeypatch):
    import os as os_mod
    from mindie_knowledge.graded.explore import resolve_workspace_ledger
    from mindie_knowledge.graded.maintenance import ROUTING_MANIFEST_SCHEMA, _existing_routing_document

    dir_target = tmp_path / "logs" / "experiment-ledger.jsonl"
    dir_target.mkdir(parents=True)
    with pytest.raises(ValueError, match="not a regular file"):
        read_bounded_jsonl(dir_target)
    with pytest.raises(ValueError, match="not a regular file"):
        resolve_workspace_ledger(str(tmp_path), tmp_path)

    routing_dir = tmp_path / "routing.json"
    routing_dir.mkdir()
    doc, err = _existing_routing_document(
        routing_dir, label="routing manifest", schemas=frozenset({ROUTING_MANIFEST_SCHEMA}), keys=("schema",),
    )
    assert doc is None and err is not None and "not a regular file" in err

    blocked = tmp_path / "blocked.jsonl"
    real_lstat = os_mod.lstat

    def deny_blocked(target, *args, **kwargs):
        if Path(target) == blocked:
            raise PermissionError("simulated permission denied")
        return real_lstat(target, *args, **kwargs)

    monkeypatch.setattr(os_mod, "lstat", deny_blocked)
    with pytest.raises(ValueError, match="PermissionError"):
        read_bounded_jsonl(blocked)
    doc2, err2 = _existing_routing_document(
        blocked, label="routing manifest", schemas=frozenset({ROUTING_MANIFEST_SCHEMA}), keys=("schema",),
    )
    assert doc2 is None and err2 is not None and "PermissionError" in err2


def test_multi_domain_deactivation_revokes_seven_healthy_domains_and_isolates_locked_store(tmp_path):
    import sys as sys_mod
    import time
    from mindie_knowledge.loop.activation import Admission
    from mindie_knowledge.loop.domain_routes import deactivate_registered_domains

    configs: dict[str, Path] = {}
    admissions: dict[str, Path] = {}
    for index in range(7):
        domain = f"domain-{index}"
        settings = tmp_path / f"{domain}-community.json"
        write_settings(settings, consent_choice="contribute", enabled=True, roots=[tmp_path])
        adm_path = tmp_path / f"{domain}-admission.sqlite3"
        cfg_path = tmp_path / f"{domain}-engine.json"
        cfg_path.write_text(
            json.dumps(
                dict(
                    root=str(tmp_path / "state"),
                    domain=domain,
                    admission_path=str(adm_path),
                    community_config=str(settings),
                    transcript_adapter=str(parser_path("codex")),
                    redactor_executable=sys_mod.executable,
                )
            ),
            encoding="utf-8",
            newline="\n",
        )
        Admission(adm_path).activate("current", project_root=str(tmp_path))
        configs[domain] = cfg_path
        admissions[domain] = adm_path

    default_cfg = configs["domain-0"]
    extra_domains = {name: str(path) for name, path in configs.items() if name != "domain-0"}
    started = time.monotonic()
    result = deactivate_registered_domains(
        default_cfg, domain_configs=extra_domains, session="current",
    )
    assert time.monotonic() - started < 2.0
    assert result["status"] == "inactive" and result["changed"] is True
    for name, adm_path in admissions.items():
        assert Admission(adm_path).active_lease("current") is None, name

    # With 6 domains where domain-0 is locked and 5 are healthy, a 24s caller
    # budget caps domain-0 near the 5.5s floor and revokes all 5 healthy stores.
    subset_names = [f"domain-{index}" for index in range(6)]
    for name in subset_names:
        Admission(admissions[name]).activate("current", project_root=str(tmp_path))
    holder = sqlite3.connect(admissions["domain-0"])
    holder.execute("BEGIN IMMEDIATE")
    try:
        partial = deactivate_registered_domains(
            default_cfg,
            domain_configs={name: str(configs[name]) for name in subset_names if name != "domain-0"},
            session="current",
            timeout=24.0,
        )
    finally:
        holder.rollback()
        holder.close()
    assert partial["status"] == "partial" and partial["changed"] is True
    assert Admission(admissions["domain-0"]).active_lease("current") is not None
    for name in subset_names[1:]:
        assert Admission(admissions[name]).active_lease("current") is None, name


def test_stop_default_budget_fits_native_hook_wrapper_and_never_stacks_waits(tmp_path, monkeypatch):
    import time
    from mindie_knowledge.loop import domain_routes

    assert handoff.STOP_TIMEOUT_SECONDS == 6.0
    assert domain_routes.deactivate_registered_domains.__kwdefaults__["timeout"] == 6.0

    project = tmp_path / "proj"
    project.mkdir()
    config, admission = _engine_config(tmp_path, project)
    loaded = cli.config_at(config)
    held = sqlite3.connect(handoff._store_path(loaded))
    held.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        contended = cli.capture_hook(config, _stop_event(admission))
    finally:
        held.rollback()
        held.close()
    elapsed = time.monotonic() - started
    assert contended["stage"] == "unavailable"
    assert contended["reason"] == "store-locked"
    # Even with OS busy-handler timer slack, default Stop completes inside native 8s/10s Hook wrappers.
    assert elapsed < 7.5

    # Partial contention on Admission (> 1.0s under default 6.0s budget) fails closed before Store SQLite.
    clock = {"now": 200.0}
    monkeypatch.setattr(handoff.time, "monotonic", lambda: clock["now"])
    real_auth = handoff.Admission.capture_authorization

    def delayed_auth(self, *args, **kwargs):
        out = real_auth(self, *args, **kwargs)
        clock["now"] += 1.5
        return out

    monkeypatch.setattr(handoff.Admission, "capture_authorization", delayed_auth)
    stacked = cli.capture_hook(config, _stop_event(admission, turn_id="turn-2"))
    assert stacked["stage"] == "unavailable"
    assert stacked["reason"] == "budget-exhausted"
