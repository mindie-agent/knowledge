"""No scanner clock/report truncation; shutdown owns its local process tree."""
import sys
import threading
import time

import pytest

from mindie_knowledge.community import common
from mindie_knowledge.loop.process import MaintenanceCancelled
from mindie_knowledge.loop import transcript_redaction as scanner
from mindie_knowledge.materials.ingest import prepare_increment


def test_default_scanner_has_no_deadline_and_uses_complete_filtered_environment(monkeypatch):
    monkeypatch.setenv("GITLEAKS_CONFIG", "must-not-reach-scanner")
    monkeypatch.setenv("GITLEAKS_ENABLE_TEST_ALLOWLIST", "must-not-reach-scanner")
    owner = threading.Event()
    observed = []
    def run(argv, **kwargs):
        observed.append(kwargs)
        return common.ProcessResult(0, b"[]", b"", False)
    monkeypatch.setattr(scanner, "run_argv", run)
    prepared = prepare_increment(task_id="fixture", text="Public technical text.", start=0, end=1,
                                 source_digest="fixture", scanner_state={}, executable=sys.executable,
                                 key=b"a" * 32, private_paths=(), cancel=owner)
    assert prepared["blocks"][0]["text"] == "Public technical text.\n\n"
    assert len(observed) == 1
    options = observed[0]
    assert options["timeout"] is None and options["max_output"] is None
    assert options["cancel"] is owner and options["inherit_env"] is False
    assert not any(key.startswith("GITLEAKS_") for key in options["env"])


def test_complete_report_exceeds_usual_runner_cap_without_restoring_ambient_scanner_config(monkeypatch):
    monkeypatch.setenv("GITLEAKS_CONFIG", "must-not-reach-scanner")
    source = "public technical words " * 18000 + "SYNTHETIC_MARKER"
    code = """import json,os,sys
assert not any(key.startswith('GITLEAKS_') for key in os.environ)
text=sys.stdin.read()
print(json.dumps([{'StartLine':1,'EndLine':1,'Secret':'SYNTHETIC_MARKER',
                  'Match':text,'RuleID':'fixture'}]))
"""
    sizes = []
    def run(_argv, **kwargs):
        result = common.run_argv([sys.executable, "-c", code], **kwargs)
        sizes.append(len(result.out))
        return result
    monkeypatch.setattr(scanner, "run_argv", run)
    clean, rules = scanner.redact(source, executable=sys.executable, key=b"a" * 32)
    assert sizes[0] > 256 * 1024
    assert clean == source.removesuffix("SYNTHETIC_MARKER") + "[REDACTED_SECRET]"
    assert "secret-fixture" in rules


def test_cancel_before_scan_or_private_key_continuation_starts_no_process(monkeypatch):
    owner = threading.Event()
    owner.set()
    def unexpected(*_args, **_kwargs):
        pytest.fail("cancelled scanner started a process")
    monkeypatch.setattr(scanner, "run_argv", unexpected)
    with pytest.raises(MaintenanceCancelled):
        scanner.redact("public text", executable=sys.executable, key=b"a" * 32, cancel=owner)
    with pytest.raises(MaintenanceCancelled):
        scanner.redact_increment("still inside private key", state={"private_key": "PRIVATE KEY"},
                                 executable=sys.executable, key=b"a" * 32, cancel=owner)


def test_running_scan_cancel_stops_process_and_preserves_cancellation_kind(tmp_path, monkeypatch):
    ready = tmp_path / "scanner-started"
    owner = threading.Event()
    spawned = []
    popen = common.subprocess.Popen
    def observe_process(*args, **kwargs):
        process = popen(*args, **kwargs)
        spawned.append(process)
        return process
    monkeypatch.setattr(common.subprocess, "Popen", observe_process)
    code = "import sys,time;from pathlib import Path;Path(sys.argv[1]).write_text('ready');time.sleep(30)"
    def run(_argv, **kwargs):
        return common.run_argv([sys.executable, "-c", code, str(ready)], **kwargs)
    monkeypatch.setattr(scanner, "run_argv", run)
    def cancel_after_start():
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        owner.set()
    cancel_thread = threading.Thread(target=cancel_after_start)
    cancel_thread.start()
    started = time.monotonic()
    try:
        with pytest.raises(MaintenanceCancelled, match="cancelled by owner"):
            scanner.redact("public text", executable=sys.executable, key=b"a" * 32, cancel=owner)
    finally:
        owner.set()
        cancel_thread.join(timeout=5)
    assert ready.read_text() == "ready"
    assert len(spawned) == 1 and spawned[0].poll() is not None
    assert time.monotonic() - started < 5


def test_scanner_runner_failure_stays_visible_without_disclosing_report(monkeypatch):
    def failure(*_args, **_kwargs):
        raise common.UnknownOutcome("private-report-canary")
    monkeypatch.setattr(scanner, "run_argv", failure)
    with pytest.raises(scanner.ScannerUnavailable) as caught:
        scanner.redact("public text", executable=sys.executable, key=b"a" * 32)
    assert "private-report-canary" not in str(caught.value)
