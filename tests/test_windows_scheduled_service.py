"""Opt-in acceptance on an interactive Windows desktop, using a real task.

Run with MINDIE_TEST_WINDOWS_SCHEDULER=1. Hosted CI has no interactive token;
ordinary Windows CI still checks real PE launchers and process console flags.
"""
import json
import os
from pathlib import Path
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "nt" or os.environ.get("MINDIE_TEST_WINDOWS_SCHEDULER") != "1",
    reason="opt-in real Task Scheduler acceptance on an interactive Windows desktop")


def test_scheduler_starts_ready_windowless_service_and_stops_cleanly(tmp_path):
    from mindie_knowledge.loop import cli, windows_service as host, windows_manager as manager
    from mindie_knowledge.loop.locks import lock_held

    evidence = tmp_path / "windowless.json"
    paths = {key: os.environ[key] for key in (
        "MINDIE_CONFIG_HOME", "MINDIE_DATA_HOME", "MINDIE_DIAGNOSTICS_ROOT",
        "MINDIE_DIAGNOSTICS_CONFIG")}
    paths["MINDIE_WINDOWLESS_PROBE"] = str(evidence)
    config = dict(root=str(tmp_path / "data with spaces 中文"), domain="test",
                  transcript_adapter=str(Path(__file__).with_name("windows_service_probe.py")),
                  redactor_executable=str(tmp_path / "unused-scanner.exe"),
                  service_launch_context=dict(paths=paths))
    config_path = tmp_path / "engine.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    request = None
    connection = None
    try:
        request = host.request_start(config_path, config)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            assert request.poll() is None
            if cli.connection_path(config).exists():
                connection = cli.connect(config, config_path=config_path)
                assert isinstance(cli.rpc(connection, "status", timeout=1), dict)
                break
            time.sleep(.05)
        assert connection is not None, "scheduled service did not become ready"
        state = json.loads(evidence.read_text(encoding="utf-8"))
        assert Path(state["prefix"]).resolve() == Path(sys.prefix).resolve()
        assert Path(state["executable"]).name.casefold() == "pythonw.exe"
        assert state["console"] == 0 and state["visible"] == []
        assert not any(p["image"].casefold() == "conhost.exe" for p in state["processes"])
        assert state["argv"][1:] == request.record["args"][3:]
    finally:
        if request is not None:
            # Stop through the authenticated business endpoint, then remove
            # only this sandbox's verified task. A failed cleanup fails the test.
            try:
                if connection is None and cli.connection_path(config).exists():
                    connection = cli.connect(config, config_path=config_path)
                if connection is not None:
                    cli.rpc(connection, "stop", timeout=2)
                deadline = time.monotonic() + 6
                while request.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                assert request.poll() == 0, "test service did not stop cleanly"
                assert not lock_held(cli.connection_path(config).with_name("consumer.lock"))
            finally:
                # Even a failed startup/assertion must not leave a scheduled
                # test service. Forced cleanup is limited to this verified task.
                task = host._task(request.record["label"])
                host._verify_task(task, request.record, request.root / "launch.json")
                if task and task["state"] in {"Running", "Queued"}:
                    manager._call("remove", lambda folder, api:
                                  folder.GetTask(request.record["label"]).Stop(0))
                host.remove_owned(config_path, config)
                assert host._task(request.record["label"]) is None
