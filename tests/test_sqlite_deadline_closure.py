"""[Role] Deterministic regression suite for SQLite open-stage floor checks and wake deadlines.

[Invariants]
- Existing databases check the 5.0 s SQLite busy floor between each potentially blocking
  schema/validation/WAL stage without self-bricking first-use (`fresh=True`) initialization.
- `request_wake` carries one monotonic `wake_deadline` across `Admission.inspect`, both probes,
  and both OS locks (`wake.request.lock`, `start.lock`), and explicit caller lock waits cap
  `MINDIE_START_LOCK_WAIT_SECONDS`.

[Timeouts & Locks]
- Pure local boundary tests (`< 5s`) with controlled monotonic clocks; zero real multi-second sleeps.

[Side-Effect Policy]
- Writes strictly inside `pytest`'s `tmp_path`.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from mindie_knowledge import owned_state
from mindie_knowledge.loop import handoff, service_host
from mindie_knowledge.loop.locks import StartInProgress, StartLock
from tests.test_audit332_visibility import _engine_config


def test_open_database_checks_floor_between_schema_and_validation_stages(tmp_path, monkeypatch):
    db_path = tmp_path / "multi_stage.sqlite3"
    init_conn = owned_state.open_database(
        db_path,
        schema="test/1",
        required={"t1": {"a"}, "t2": {"b"}},
        initialize=lambda db: (
            db.execute("CREATE TABLE t1 (a INTEGER)"),
            db.execute("CREATE TABLE t2 (b INTEGER)"),
        ),
        timeout=10.0,
    )
    init_conn.close()

    clock = {"now": 500.0}
    monkeypatch.setattr(owned_state.time, "monotonic", lambda: clock["now"])
    real_connect = owned_state.sqlite3.connect
    seen_tables = []

    def traced_connect(*args, **kwargs):
        db = real_connect(*args, **kwargs)

        def on_sql(sql):
            if sql.startswith("PRAGMA table_info("):
                seen_tables.append(sql)
                clock["now"] += 4.1

        db.set_trace_callback(on_sql)
        return db

    monkeypatch.setattr(owned_state.sqlite3, "connect", traced_connect)
    with pytest.raises(owned_state.SqliteBudgetExhausted):
        owned_state.open_database(
            db_path,
            schema="test/1",
            required={"t1": {"a"}, "t2": {"b"}},
            initialize=lambda _db: None,
            validate=lambda _db: pytest.fail("validate must not run below lock floor"),
            timeout=6.0,
        )
    assert len(seen_tables) == 1


def test_request_wake_propagates_deadline_and_caps_env_lock_wait(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    config, admission = _engine_config(tmp_path, project)

    # 1. A 1.0s wake allowance fails closed before entering SQLite on a locked Admission DB.
    held = sqlite3.connect(admission)
    held.execute("BEGIN IMMEDIATE")
    try:
        short = handoff.request_wake(config, session_id="manual-A", timeout=1.0)
    finally:
        held.rollback()
        held.close()
    assert short["wake"] == "not-requested"
    assert short["reason"] == "budget-exhausted"

    # 2. Windows lock + probe sequence shares one monotonic wake_deadline even when
    # MINDIE_START_LOCK_WAIT_SECONDS=10 is set in the environment.
    monkeypatch.setenv("MINDIE_START_LOCK_WAIT_SECONDS", "10")
    clock = {"now": 1000.0}
    monkeypatch.setattr(handoff.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr("mindie_knowledge.loop.activation.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr("mindie_knowledge.owned_state.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr("mindie_knowledge.loop.locks.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        "mindie_knowledge.loop.locks.time.sleep",
        lambda dt: clock.__setitem__("now", clock["now"] + dt),
    )
    monkeypatch.setattr(handoff, "os", SimpleNamespace(name="nt", environ={"MINDIE_START_LOCK_WAIT_SECONDS": "10"}))
    monkeypatch.setattr(
        service_host,
        "platform_backend",
        lambda: SimpleNamespace(request_start=lambda *_a: pytest.fail("must coalesce at deadline")),
    )
    probe_budgets = []

    def fake_probe(_cfg, wait, **_kw):
        probe_budgets.append(round(wait, 2))
        clock["now"] += 0.25
        return "absent"

    monkeypatch.setattr(handoff, "_probe", fake_probe)
    from mindie_knowledge.loop import cli as cli_mod

    loaded_cfg = cli_mod.config_at(config)
    held_start = StartLock(cli_mod.connection_path(loaded_cfg).with_name("start.lock"))
    held_start.acquire(wait=0)
    try:
        win = handoff.request_wake(config, session_id="manual-A", timeout=6.0)
    finally:
        held_start.release()
    assert win["wake"] == "coalesced"
    assert probe_budgets == [2.0]
    assert clock["now"] - 1000.0 == pytest.approx(6.0, abs=0.05)


def test_fresh_open_database_never_self_bricks_on_connect_or_mid_init_timeout(tmp_path, monkeypatch):
    # 1. 10s budget with 4.8s init-lock wait and 0.3s sqlite3.connect succeeds on fresh DB.
    clock = {"now": 3000.0}
    monkeypatch.setattr(owned_state.time, "monotonic", lambda: clock["now"])
    orig_acquire = StartLock.acquire
    orig_connect = owned_state.sqlite3.connect

    def slow_lock_acquire(self, *, wait=None, write_record=True):
        res = orig_acquire(self, wait=0, write_record=write_record)
        clock["now"] += 4.8
        return res

    def slow_connect(*args, **kwargs):
        conn = orig_connect(*args, **kwargs)
        clock["now"] += 0.3
        return conn

    monkeypatch.setattr(StartLock, "acquire", slow_lock_acquire)
    monkeypatch.setattr(owned_state.sqlite3, "connect", slow_connect)
    fresh_ok = tmp_path / "fresh_ok.sqlite3"
    conn = owned_state.open_database(
        fresh_ok,
        schema="test/1",
        required={"t1": {"a"}},
        initialize=lambda db: db.execute("CREATE TABLE t1 (a INTEGER)"),
        timeout=10.0,
    )
    conn.close()

    # 2. Mid-initialization deadline expiry cleans up uncommitted fresh stub so retry succeeds.
    monkeypatch.setattr(StartLock, "acquire", orig_acquire)
    monkeypatch.setattr(owned_state.sqlite3, "connect", orig_connect)
    aborted_path = tmp_path / "fresh_aborted.sqlite3"

    def expiring_init(db):
        db.execute("CREATE TABLE t1 (a INTEGER)")
        clock["now"] += 15.0
        db.execute("INSERT INTO t1 VALUES (1)")

    with pytest.raises(owned_state.SqliteBudgetExhausted):
        owned_state.open_database(
            aborted_path,
            schema="test/1",
            required={"t1": {"a"}},
            initialize=expiring_init,
            timeout=10.0,
        )
    assert not aborted_path.exists()

    recovered = owned_state.open_database(
        aborted_path,
        schema="test/1",
        required={"t1": {"a"}},
        initialize=lambda db: db.execute("CREATE TABLE t1 (a INTEGER)"),
        timeout=10.0,
    )
    recovered.close()


def test_first_use_store_recovers_cleanly_after_pre_connect_or_init_deadline_expiry(
    tmp_path, monkeypatch
):
    from contextlib import closing
    from mindie_knowledge import state_layout
    from mindie_knowledge.loop import store as store_mod
    from mindie_knowledge.loop.store import Store

    clock = {"now": 5000.0}
    monkeypatch.setattr(owned_state.time, "monotonic", lambda: clock["now"])
    orig_acquire = StartLock.acquire
    orig_init = store_mod._initialize_state
    orig_validate = store_mod._validate_state

    # 1. Pre-connect floor exhaustion inside Store -> prepare_layout -> open_database.
    def slow_init_lock(self, *, wait=None, write_record=True):
        res = orig_acquire(self, wait=0, write_record=write_record)
        if self.path.name.endswith(".init.lock"):
            clock["now"] += 6.0
        return res

    with monkeypatch.context() as patch:
        patch.setattr(StartLock, "acquire", slow_init_lock)
        with pytest.raises(owned_state.SqliteBudgetExhausted):
            Store(tmp_path / "case_preconnect", "demo")
    assert not (tmp_path / "case_preconnect" / "demo" / "state-v1").exists()
    assert not (tmp_path / "case_preconnect" / "demo" / "state-layout.json").exists()
    assert state_layout.state_root(tmp_path / "case_preconnect", "demo").name == "state-v1"
    with closing(Store(tmp_path / "case_preconnect", "demo")) as recovered:
        assert recovered.status()["entries"] == {}

    # 2. Mid-schema initialization deadline expiry inside Store -> prepare_layout -> open_database.
    def expiring_store_init(db):
        orig_init(db)
        clock["now"] += 15.0

    with monkeypatch.context() as patch:
        patch.setattr(store_mod, "_initialize_state", expiring_store_init)
        with pytest.raises(owned_state.SqliteBudgetExhausted):
            Store(tmp_path / "case_midinit", "demo")
    assert not (tmp_path / "case_midinit" / "demo" / "state-v1").exists()
    assert not (tmp_path / "case_midinit" / "demo" / "state-layout.json").exists()
    assert state_layout.state_root(tmp_path / "case_midinit", "demo").name == "state-v1"
    with closing(Store(tmp_path / "case_midinit", "demo")) as recovered:
        recovered.create_draft(
            kind="experience",
            title="recovered after mid-init budget expiry",
            summary="synthetic",
            content="SYNTHETIC recovery verification",
        )
        assert recovered.status()["entries"] == {"draft": 1}

    # 3. Post-commit validation deadline expiry before persist_record also rolls back fresh Store.
    def expiring_validate(db):
        orig_validate(db)
        clock["now"] += 15.0

    with monkeypatch.context() as patch:
        patch.setattr(store_mod, "_validate_state", expiring_validate)
        with pytest.raises(owned_state.SqliteBudgetExhausted):
            Store(tmp_path / "case_postvalidate", "demo")
    assert not (tmp_path / "case_postvalidate" / "demo" / "state-v1").exists()
    with closing(Store(tmp_path / "case_postvalidate", "demo")) as recovered:
        assert recovered.status()["entries"] == {}
