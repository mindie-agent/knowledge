"""The Stop wake must outlive the hook process group. Not native acceptance."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(os, "killpg"),
    reason=(
        "POSIX process groups only (os.killpg). Skipped on Windows; "
        "this skip is not a Windows acceptance result."
    ),
)
def test_wake_survives_hook_process_group_kill(tmp_path):
    from tests.conftest import admission_token, make_admission, write_settings

    project = tmp_path / "proj"
    project.mkdir()
    settings = tmp_path / "community.json"
    write_settings(settings, enabled=True, roots=[project])
    admission = make_admission(tmp_path, project_root=project)
    calls = tmp_path / "calls.txt"
    from datetime import datetime, timezone
    from mindie_knowledge.loop.transcript_redaction import install_scanner
    from material_worker_fixture import command
    import transcript_double
    transcript = project / 'wire.jsonl'
    from mindie_knowledge.loop.store import Store

    Store(tmp_path / "root", "test").close()
    transcript.write_text(json.dumps(dict(type='session_meta', payload=dict(id='manual-A'))) + '\n' +
        json.dumps(dict(type='response_item', timestamp=datetime.now(timezone.utc).isoformat(),
                        payload=dict(type='message', role='user', content=[dict(type='input_text',
                            text='Anonymous resumed precision case remains unverified.')]))) + '\n')
    config = tmp_path / "engine.json"
    config.write_text(json.dumps(dict(
        root=str(tmp_path / "root"), domain="test",
        community_config=str(settings), admission_path=str(admission),
        capture_mode="public-transcript", transcript_adapter=transcript_double.__file__,
        redactor_executable=install_scanner(), summary_command=command(calls=calls),
    )))
    event = dict(
        hook_event_name="Stop", identity_kind="turn", session_id="manual-A",
        turn_id="turn-1", harness="codex", mindie_activation=admission_token(admission),
        transcript_path=str(transcript),
        budget_seconds=1.0,
    )
    marker = tmp_path / "hook.json"
    hook = tmp_path / "hook.py"
    hook.write_text(
        "import json, os, time\n"
        "from mindie_knowledge.loop.cli import capture_hook\n"
        f"result = capture_hook({str(config)!r}, json.loads({json.dumps(event)!r}))\n"
        f"open({str(marker)!r}, 'w').write(json.dumps(result))\n"
        "time.sleep(30)\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    proc = subprocess.Popen(
        [sys.executable, str(hook)], start_new_session=True, env=env,
    )
    service = None
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not marker.is_file():
            if proc.poll() is not None:
                raise AssertionError("hook exited before recording the handoff")
            time.sleep(0.05)
        assert marker.is_file()
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=2)
        connection = tmp_path / "root" / "test" / "connection.json"
        deadline = time.monotonic() + 12
        from mindie_knowledge.loop.cli import connect
        from mindie_knowledge.loop.transport import rpc

        status = None
        while time.monotonic() < deadline:
            if connection.is_file():
                try:
                    status = rpc(connect(json.loads(config.read_text())), "status", timeout=0.4)
                except OSError:
                    status = None
                if isinstance(status, dict) and status.get("worker_alive") is True:
                    break
            time.sleep(0.1)
        assert isinstance(status, dict) and status.get("worker_alive") is True, (
            marker.read_text(),
            list((tmp_path / "root" / "test").iterdir()) if (tmp_path / "root" / "test").is_dir() else None,
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if calls.is_file() and calls.read_text().count("x") >= 1:
                break
            time.sleep(0.1)
        assert calls.is_file() and calls.read_text().count("x") == 1, (
            calls.read_text() if calls.is_file() else "no index call",
        )
        row = Store(tmp_path / "root", "test")
        try:
            assert row.status()["captures"][0]["status"] in {"organized", "queued", "pending", "processing"}
        finally:
            row.close()
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        wake = tmp_path / "root" / "test" / "wake.json"
        if wake.is_file():
            holder = json.loads(wake.read_text())
            for key in ("service_pid", "wake_pid"):
                pid = holder.get(key)
                if isinstance(pid, int):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
        connection = tmp_path / "root" / "test" / "connection.json"
        if connection.is_file():
            try:
                from mindie_knowledge.loop.cli import connect
                from mindie_knowledge.loop.transport import rpc

                rpc(connect(json.loads(config.read_text())), "stop", timeout=2)
            except OSError:
                pass
