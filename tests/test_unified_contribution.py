import json

from mindie_knowledge import consent_store
from mindie_diagnostics import reporting_commands
from mindie_diagnostics.integration import record_failure
from mindie_diagnostics.reporting import current_consent


def test_legacy_disabled_reporting_requires_explicit_unified_choice(tmp_path):
    consent, config = tmp_path / "consent.json", tmp_path / "diagnostics.json"
    consent.write_text(json.dumps({"schema": "mindie-consent/1", "choice": "contribute",
                                   "reporting": "disabled"}))
    before = consent.read_bytes()
    result = consent_store.activate_reporting(consent, config=config, ensure=False)
    assert result["status"] == "needs_choice"
    assert consent.read_bytes() == before
    assert current_consent(config) is None
    saved = consent_store.record_choice(consent, "contribute")
    result = consent_store.activate_reporting(consent, config=config, ensure=False)
    assert result["status"] == "configured" and result["enabled"]
    assert current_consent(config) is not None
    assert consent_store.record_choice(consent, "contribute")["choice_revision"] == saved["choice_revision"]
    consent_store.record_choice(consent, "disabled")
    assert current_consent(config) is None
    assert consent_store.record_choice(consent, "contribute")["choice_revision"] != saved["choice_revision"]


def test_activation_calls_worker_and_preserves_failed_readiness(tmp_path, monkeypatch):
    consent, config = tmp_path / "consent.json", tmp_path / "diagnostics.json"
    assert consent_store.activate_reporting(consent, config=config)["status"] == "needs_choice"
    consent_store.record_choice(consent, "contribute")
    calls = []

    def failed_worker(path, **kwargs):
        calls.append(path)
        return {"status": "degraded", "category": "command_unavailable"}

    monkeypatch.setattr(reporting_commands, "ensure", failed_worker)
    result = consent_store.activate_reporting(consent, config=config)
    assert calls == [config]
    assert result["status"] == "degraded"
    assert result["worker"]["category"] == "command_unavailable"
    consent_store.record_choice(consent, "disabled")
    assert consent_store.activate_reporting(consent, config=config)["status"] == "disabled"
    assert calls == [config]


def test_explicit_choice_records_authorized_fault_and_disabling_stops_new_events(tmp_path, monkeypatch):
    from mindie_diagnostics.ingestion import ingest
    from mindie_diagnostics.outbox import Outbox

    consent, config, root = tmp_path / "consent.json", tmp_path / "diagnostics.json", tmp_path / "logs"
    consent_store.record_choice(consent, "contribute")
    consent_store.activate_reporting(consent, config=config, roots=[str(root)], ensure=False)
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_CONFIG", str(config))
    arguments = dict(component="mindie-knowledge", operation="index.fixture", stage="index",
                     category="internal_exception", root=root)
    first = record_failure(**arguments)
    consent_store.record_choice(consent, "disabled")
    second = record_failure(**arguments)
    assert first["recorded"] and second["recorded"]
    rows = [json.loads(line) for file in root.rglob("*.jsonl") for line in file.read_text().splitlines()]
    assert next(row for row in rows if row.get("operation_id") == first["incident_id"])["reporting"]
    assert next(row for row in rows if row.get("operation_id") == second["incident_id"])["reporting"] is None
    queue = Outbox(tmp_path / "queue.db")
    assert ingest(root, queue, config=config)["enqueued"] == 0
