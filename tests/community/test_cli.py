"""CLI executable-path checks: submit/reconcile/review/event/poll-once/skill-scan."""

import json

from mindie_knowledge.community.cli import main

from .conftest import entry_file, feedback_file, make_batch, make_entry, vote


def _write_settings(tmp_path, settings):
    path = tmp_path / "community.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    return str(path)


def test_cli_submit_reconcile_review_poll_skill(tmp_path, settings, capsys):
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

    # Pure-vote content would merge model-free; this PR also carries an entry
    # and no model is configured, so it stays pending truthfully.
    assert main(["review", "--settings", settings_path, "--state-dir", state, "--pr", "1"]) == 0
    review = json.loads(capsys.readouterr().out)
    assert review["status"] == "pending"

    event = tmp_path / "event.json"
    event.write_text(json.dumps({
        "action": "opened",
        "pull_request": {"number": 1, "head": {"sha": "s", "ref": "mindie-contrib/npu/cli-1"},
                         "user": {"login": "contributor"}},
        "repository": {"full_name": settings["repository"]},
        "sender": {"login": "contributor"}}), encoding="utf-8")
    assert main(["event", "--settings", settings_path, "--state-dir", state,
                 "--file", str(event)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pending"  # already reviewed head

    assert main(["poll-once", "--settings", settings_path, "--state-dir", state]) == 0
    swept = json.loads(capsys.readouterr().out)
    assert swept["repository"] == settings["repository"]

    assert main(["skill-scan", "--settings", settings_path, "--state-dir", state]) == 0
    assert "candidates" in json.loads(capsys.readouterr().out)


def test_cli_disabled_sharing_reports_disabled(tmp_path, settings, capsys):
    settings["enabled"] = False
    settings_path = _write_settings(tmp_path, settings)
    batch = tmp_path / "batch.json"
    batch.write_text(json.dumps(make_batch("cli-2", [entry_file(make_entry())])), encoding="utf-8")
    assert main(["submit", "--settings", settings_path, "--state-dir",
                 str(tmp_path / "s2"), "--batch", str(batch)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "disabled"
