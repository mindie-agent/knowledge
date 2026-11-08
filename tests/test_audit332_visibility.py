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
    from mindie_knowledge.loop.activation import Admission

    path = make_admission(tmp_path, project_root=tmp_path)
    gate = Admission(path)
    token = admission_token(path)
    statements = []
    original = Admission._database

    def traced(self, *args, **kwargs):
        db = original(self, *args, **kwargs)
        if db is not None:
            db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(Admission, "_database", traced)
    assert gate.capture_authorization("manual-A", token, timeout=0.1)["state"] == "admitted"
    busy = []
    for statement in statements:
        match = re.search(r"PRAGMA\s+busy_timeout=(\d+)", statement, re.I)
        if match:
            busy.append(int(match.group(1)))
    assert busy and min(busy) >= 1000


def test_sqlite_busy_timeout_never_goes_sub_second():
    assert sqlite_busy_timeout_ms(None) == SQLITE_BUSY_TIMEOUT_MS
    assert sqlite_busy_timeout_ms(0.05) is None
    assert sqlite_busy_timeout_ms(0.999) is None
    assert sqlite_busy_timeout_ms(1.0) == 1000
    assert sqlite_busy_timeout_ms(2.5) == 2500
    assert sqlite_busy_timeout_ms(3.0) == 3000
    assert sqlite_busy_timeout_ms(6.0) == 6000


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
            if "journal_mode" in lowered and "wal" in lowered:
                clock["now"] += wal_delay
            if "busy_timeout" in lowered:
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
        monkeypatch, handoff._store_path(loaded), clock, 1.2, pragmas, begins,
    )
    result = cli.capture_hook(config, _stop_event(admission), timeout=2.0)
    assert result["stage"] == "unavailable"
    assert result["reason"] == "budget-exhausted"
    assert begins == []
    assert not any("busy_timeout=2000" in item for item in pragmas)


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
        monkeypatch, handoff._store_path(loaded), clock, 0.5, pragmas, begins,
    )
    result = cli.capture_hook(config, _stop_event(admission), timeout=2.0)
    assert result["stage"] in {"accepted-local", "accepted-runtime", "accepted"}
    assert any("busy_timeout=1500" in item for item in pragmas)
    assert begins


def test_truncated_utf8_ledger_tail_is_dropped_not_corruption(tmp_path):
    complete = (json.dumps({"run_id": "ok"}, ensure_ascii=False) + "\n").encode("utf-8")
    zhong = "中".encode("utf-8")
    rest = b'{"run_id": "' + zhong + b'"}\n'
    payload = complete + rest[: rest.index(zhong) + 1]
    path = tmp_path / "experiment-ledger.jsonl"
    path.write_bytes(payload)
    assert read_bounded_jsonl(path, max_bytes=len(payload)) == [{"run_id": "ok"}]
