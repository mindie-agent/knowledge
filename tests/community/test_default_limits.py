"""Owner lifetimes replace preset publication and subprocess clocks."""

import sys
import threading
import time

import pytest

from mindie_knowledge.community import common, gitops, transport
from mindie_knowledge.community.common import CommunityError, Deadline, UnknownOutcome, run_argv
from mindie_knowledge.community.settings import SharingDisabled, live_gate, validate_settings


def test_default_transaction_has_no_time_or_operation_cap(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(common.time, "monotonic", lambda: clock[0])
    deadline = Deadline()
    clock[0] = 1_000_000.0
    for _ in range(1001):
        assert deadline.step() is None
    assert deadline.limit is None
    assert deadline.remaining() is None
    assert deadline.operations_used == 1001
    assert deadline.remaining_ops is None


def test_settings_default_to_no_limits_and_accept_explicit_large_or_fractional_limits(settings):
    supplied = {k: v for k, v in settings.items() if k not in ("transaction_seconds", "operation_limit")}
    result = validate_settings(supplied)
    assert result["transaction_seconds"] is None
    assert result["operation_limit"] is None
    for seconds in (0.01, 10000):
        result = validate_settings({**supplied, "transaction_seconds": seconds, "operation_limit": 10000})
        assert result["transaction_seconds"] == seconds
        assert result["operation_limit"] == 10000


@pytest.mark.parametrize("value", [0, -1, True, "30", float("nan"), float("inf"), 10 ** 400])
def test_explicit_invalid_limits_fail_before_spawn(value, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("invalid limit started a process")
    monkeypatch.setattr(common.subprocess, "Popen", unexpected)
    with pytest.raises(CommunityError, match="positive finite"):
        run_argv([sys.executable, "-c", "pass"], timeout=value)
    with pytest.raises(CommunityError, match="positive finite"):
        Deadline(value)


def test_unlimited_subprocess_waits_for_completion():
    result = run_argv([sys.executable, "-c", "print('complete')"])
    assert result.code == 0 and result.out_text.strip() == "complete"
    assert not result.timed_out


def test_unlimited_subprocess_remains_cancellable_after_output_closes(tmp_path):
    # A child can close its streams yet keep working. EOF cannot transfer it
    # into an uninterruptible wait once the default clock has been removed.
    ready = tmp_path / "started"
    cancel = threading.Event()
    timer = threading.Timer(0.35, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(UnknownOutcome, match="cancelled"):
            run_argv([sys.executable, "-c",
                      "import os,time; from pathlib import Path; "
                      "Path(__import__('sys').argv[1]).write_text('ready'); "
                      "os.close(1); os.close(2); time.sleep(30)", str(ready)], cancel=cancel)
    finally:
        timer.cancel()
    assert ready.read_text() == "ready"
    assert time.monotonic() - started < 5


def test_unlimited_subprocess_still_caps_output():
    with pytest.raises(CommunityError, match="output exceeds"):
        run_argv([sys.executable, "-c", "print('x' * 10000)"], max_output=128)


def test_git_and_api_pass_unlimited_lifetime_and_owner_cancel(monkeypatch):
    owner = threading.Event()
    seen = []
    def fake(argv, **kwargs):
        seen.append(kwargs)
        return common.ProcessResult(0, b"HTTP/2.0 200 OK\r\n\r\n{}", b"", False)
    monkeypatch.setattr(gitops, "run_argv", fake)
    monkeypatch.setattr(transport, "run_argv", fake)
    gitops._git(["status"], Deadline(cancel=owner))
    transport.GhTransport({})._api("GET", "/user", Deadline(cancel=owner))
    assert len(seen) == 2
    assert all(call["timeout"] is None and call["cancel"] is owner for call in seen)


def test_live_config_corruption_is_failure_and_explicit_disable_is_disabled(settings):
    import json
    from pathlib import Path
    path = Path(settings["config_path"])
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(CommunityError, match="not valid JSON") as caught:
        live_gate(settings)
    assert caught.value.status == "failed"
    assert not isinstance(caught.value, SharingDisabled)
    path.write_text(json.dumps({**settings, "enabled": False}), encoding="utf-8")
    with pytest.raises(SharingDisabled) as caught:
        live_gate(settings)
    assert caught.value.status == "disabled"


def test_real_publication_without_limits_reaches_remote_and_receipt(settings, state_dir, transport, monkeypatch):
    import json
    from pathlib import Path
    from mindie_knowledge.community import submit_batch
    from mindie_knowledge.community import transport as transport_module
    from .conftest import entry_file, make_batch, make_entry

    supplied = {k: v for k, v in settings.items() if k not in ("transaction_seconds", "operation_limit")}
    Path(supplied['config_path']).write_text(json.dumps(supplied), encoding='utf-8')
    seen = []
    original = common.run_argv
    def trace(argv, **kwargs):
        seen.append(kwargs.get('timeout'))
        return original(argv, **kwargs)
    for owner in (common, gitops, transport_module):
        monkeypatch.setattr(owner, 'run_argv', trace)
    receipt = submit_batch(make_batch('unlimited-default', [entry_file(make_entry())]),
                           supplied, state_dir, transport=transport)
    assert receipt['status'] == 'submitted'
    assert receipt['pr_url'] and receipt['head_sha']
    assert len(seen) > 10 and all(seconds is None for seconds in seen)
