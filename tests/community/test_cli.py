"""CLI executable-path checks: submit / reconcile (contributor side only)."""

import json

from mindie_knowledge.community.cli import main

from .conftest import entry_file, feedback_file, make_batch, make_entry, vote


def _write_settings(tmp_path, settings):
    path = tmp_path / "community.json"
    path.write_text(json.dumps({k: v for k, v in settings.items() if k != "config_path"}),
                    encoding="utf-8")
    return str(path)


def test_cli_submit_and_reconcile(tmp_path, settings, capsys):
    settings_path = _write_settings(tmp_path, settings)
    state = str(tmp_path / "state")
    doc = make_entry()
    batch = tmp_path / "batch.json"
    batch.write_text(json.dumps(make_batch(
        "cli-1", [entry_file(doc), feedback_file([vote("v1", doc["entry_id"], doc["revision"])],
                                                 path="feedback/cli.json")])), encoding="utf-8")

    assert main(["submit", "--settings", settings_path, "--state-dir", state,
                 "--batch", str(batch)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "submitted" and receipt["pr_url"]

    assert main(["reconcile", "--settings", settings_path, "--state-dir", state,
                 "--batch-id", "cli-1"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "submitted"


def test_cli_disabled_sharing_reports_disabled(tmp_path, settings, capsys):
    settings["enabled"] = False
    settings_path = _write_settings(tmp_path, settings)
    batch = tmp_path / "batch.json"
    batch.write_text(json.dumps(make_batch("cli-2", [entry_file(make_entry())])), encoding="utf-8")
    assert main(["submit", "--settings", settings_path, "--state-dir",
                 str(tmp_path / "s2"), "--batch", str(batch)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "disabled"
