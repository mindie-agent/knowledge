"""Tier 1 tests for `mindie-knowledge` authoritative state `contract_violation` diagnostics."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mindie_diagnostics.ingestion import ingest
from mindie_diagnostics.integration import configure_reporting
from mindie_diagnostics.outbox import Outbox
from mindie_diagnostics.reporter import render_issue
from mindie_knowledge.owned_state import open_database


def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    base = tmp_path.resolve()
    diag_root = base / "diag"
    cfg_file = base / "reporting.json"
    monkeypatch.setenv("HOME", str(base / "home"))
    monkeypatch.setenv("MINDIE_CONFIG_HOME", str(base / "config"))
    monkeypatch.setenv("MINDIE_DATA_HOME", str(base / "data"))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_ROOT", str(diag_root))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_CONFIG", str(cfg_file))
    (base / "home").mkdir(parents=True, exist_ok=True)
    diag_root.mkdir(parents=True, exist_ok=True)
    configure_reporting(True, roots=[str(diag_root)], config=cfg_file)
    return diag_root, cfg_file


def test_zero_byte_owned_database_records_contract_violation_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    diag_root, cfg_file = _isolate_env(tmp_path, monkeypatch)
    db_path = tmp_path.resolve() / "data" / "corrupt.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"")

    with pytest.raises(ValueError, match="empty or invalid"):
        open_database(
            db_path,
            schema="CREATE TABLE items (id TEXT PRIMARY KEY)",
            required={"items": ("id",)},
            initialize=lambda db: db.execute("CREATE TABLE items (id TEXT PRIMARY KEY)"),
        )

    queue = Outbox(tmp_path.resolve() / "queue.db")
    assert ingest(diag_root, queue, config=cfg_file)["enqueued"] == 1
    item = queue.claim()
    ev = item["payload"]["events"][-1]
    assert ev["component"] == "mindie-knowledge"
    assert ev["attributes"]["category"] == "contract_violation"
    assert ev["attributes"]["contract_name"] == "owned_sqlite_authority"
    assert ev["attributes"]["reason_code"] == "empty_or_invalid_database"
    assert any("owned_state" in f["module"] for f in ev["attributes"]["stack_frames"])
    title, body = render_issue(item)
    assert "contract violation" in title
    assert "owned_sqlite_authority" in body


def test_missing_required_column_records_contract_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    diag_root, cfg_file = _isolate_env(tmp_path, monkeypatch)
    db_path = tmp_path.resolve() / "data" / "partial.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE items (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="missing or incomplete"):
        open_database(
            db_path,
            schema="CREATE TABLE items (id TEXT PRIMARY KEY, version INTEGER)",
            required={"items": ("id", "version")},
            initialize=lambda db: None,
        )

    queue = Outbox(tmp_path.resolve() / "queue.db")
    assert ingest(diag_root, queue, config=cfg_file)["enqueued"] == 1
    item = queue.claim()
    ev = item["payload"]["events"][-1]
    assert ev["attributes"]["reason_code"] == "missing_required_columns"
    assert "partial.sqlite3" not in json.dumps(item["payload"])
