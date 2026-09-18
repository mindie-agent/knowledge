"""Maintenance hints do not acquire authority over runtime readiness."""
import json
from unittest.mock import Mock

from mindie_knowledge.cli import main
from mindie_knowledge.maintenance import maintain
from mindie_knowledge.server.layers import load_config


def setup(tmp_path):
    root = tmp_path / "notes"
    root.mkdir()
    mapping = {"backend": "memory", "state_root": str(tmp_path / "state"),
               "layers": {"project": str(root), "candidate": {"enabled": False}, "shared": {"enabled": False}},
               "shared_sync": {"enabled": False}, "publishing": {"enabled": False}}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(mapping), encoding="utf-8")
    return load_config(mapping, env={}), root, config_path


def test_cli_is_local_bounded_and_does_not_edit_notes(tmp_path, monkeypatch, capsys):
    _config, root, path = setup(tmp_path)
    for index in range(3):
        (root / f"{index}.md").write_text(f"# Note {index}\n\n[source](missing-{index}.txt)", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    forbidden = Mock(side_effect=AssertionError("health called network/backend"))
    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("mindie_knowledge.local.backend.backend_for_config", forbidden)
    assert main(["health", "--config", str(path), "--limit", "1"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["finding_count"] == 3
    assert len(output["findings"]) == 1 and output["truncated"]
    assert output["status"] == "partial"
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before


def test_worklist_failure_does_not_change_index_readiness(tmp_path, monkeypatch):
    config, root, _path = setup(tmp_path)
    (root / "one.md").write_text("# Observation\n\nCause remains uncertain.", encoding="utf-8")
    monkeypatch.setattr("mindie_knowledge.health.inspect_knowledge", Mock(side_effect=OSError("cache unavailable")))
    report = maintain(config, verify=True)
    assert report["ready"]
    assert report["health"]["status"] == "unknown"
    assert "cache unavailable" in report["health"]["reason"]


def test_health_deadline_is_reused_even_when_capture_wakes_incremental_refresh(tmp_path, monkeypatch):
    config, root, _path = setup(tmp_path)
    (root / "one.md").write_text("# Observation\n\nSource retained.", encoding="utf-8")
    health = Mock(return_value={"status": "ok", "findings": [], "snapshot": "observed"})
    monkeypatch.setattr("mindie_knowledge.health.inspect_knowledge", health)
    initial = maintain(config, verify=True)
    assert health.call_count == 1
    monkeypatch.setattr("mindie_knowledge.maintenance.time.time", lambda: initial["next_check"] + 1)
    assert maintain(config)["ready"]
    assert health.call_count == 1
    assert maintain(config, force=True)["ready"]
    assert health.call_count == 1
    monkeypatch.setattr("mindie_knowledge.maintenance.time.time", lambda: initial["next_health"] + 1)
    assert maintain(config)["ready"]
    assert health.call_count == 2
