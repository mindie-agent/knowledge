"""Deadline-gap recovery through the frozen production parsers.

transcript_double is not used. Each case drives Engine, SQLite, and one of
the Kimi, Claude Code, or Codex parsers. The model is a subprocess whose
spawns and exit codes are counted. Close/reopen is a real Store close.
The interrupt case is a separate process killed while the recovery model
is running.
"""

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store

from conftest import make_admission, write_settings
from lane_support import (
    PARSER_NAMES,
    REPO,
    SESSIONS,
    append_record,
    entry_documents,
    load_parser,
    parser_path,
    regions,
    spawn_count,
    transcript_path,
    unfinished_captures,
    write_transcript,
)

PARSERS_AND_IDS = PARSER_NAMES
SUCCESS = (
    "import json, sys\n"
    "payload = json.loads(sys.stdin.buffer.read().decode('utf-8'))\n"
    "text = payload.get('increment', '')[:400]\n"
    "sys.stdout.buffer.write(json.dumps({'entries':[{'entry_id':None,'title':'Observed',"
    "'summary':'production parser recovery','content':text,'conditions':{}}]}).encode('utf-8'))\n"
)
DEADLINE = "import sys\nsys.exit(124)\n"
CHILD = textwrap.dedent(
    """\
    import os, signal, sys
    mode = sys.argv[1]
    if mode == "model":
        marker = open(os.environ["KILL_MARKER"], "a", encoding="utf-8", newline="\\n")
        marker.write("x\\n")
        marker.flush()
        os.fsync(marker.fileno())
        marker.close()
        parent = os.getppid()
        if parent <= 1:
            raise SystemExit("refusing to signal pid %s" % parent)
        # POSIX SIGKILL is not defined on Windows. SIGTERM there is TerminateProcess.
        sig = getattr(signal, "SIGKILL", signal.SIGTERM)
        os.kill(parent, sig)
        raise SystemExit(0)
    sys.path.insert(0, os.environ["GROK_CORE_REPO"])
    import importlib.util
    from mindie_knowledge.loop.activation import Admission
    from mindie_knowledge.loop.engine import Engine
    from mindie_knowledge.loop.store import Store
    spec = importlib.util.spec_from_file_location("frozen_parser", os.environ["PARSER"])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    store = Store(os.environ["STORE_ROOT"], os.environ["STORE_DOMAIN"])
    engine = Engine(
        store,
        agent_command=[sys.executable, __file__, "model"],
        settings_path=os.environ["SETTINGS"],
        admission=Admission(os.environ["ADMISSION"]),
        transcript_adapter=module,
    )
    capture = os.environ["CAPTURE"]
    store.db.execute(
        "UPDATE continuations SET due=0, eligible=1 WHERE capture_id=?",
        (capture,),
    )
    store.db.commit()
    engine._process(capture)
    store.close()
    """
)


def _runner(path, marker, body):
    path.write_text(
        "import pathlib\n"
        f"mark = pathlib.Path({str(marker)!r})\n"
        "mark.parent.mkdir(parents=True, exist_ok=True)\n"
        "handle = mark.open('a', encoding='utf-8', newline='\\n')\n"
        "handle.write('x\\n')\n"
        "handle.close()\n"
        + body
    )


def _fire(store, capture_id):
    store.db.execute(
        "UPDATE continuations SET due=0, eligible=1 WHERE capture_id=?",
        (capture_id,),
    )
    store.db.commit()


def _docs(store):
    return "\n".join(entry_documents(store))


def _open_world(tmp_path, parser_name):
    project = tmp_path / "proj"
    project.mkdir()
    settings = tmp_path / "community.json"
    write_settings(settings, enabled=True, roots=[project])
    session = SESSIONS[parser_name]
    admission = make_admission(tmp_path, project_root=project, session=session)
    store = Store(tmp_path / "store", "test")
    parser = load_parser(parser_name)
    assert Path(parser.__file__).resolve() == parser_path(parser_name).resolve()
    box = {
        "tmp": tmp_path,
        "project": project,
        "settings": settings,
        "session": session,
        "admission": admission,
        "store": store,
        "parser": parser,
        "parser_name": parser_name,
        "marker": tmp_path / "spawns",
        "runner": tmp_path / "runner.py",
        "when": time.time() + 180,
    }
    return box


def _close(box):
    box["store"].close()


def _engine(box):
    return Engine(
        box["store"],
        agent_command=[sys.executable, str(box["runner"])],
        settings_path=box["settings"],
        admission=Admission(box["admission"]),
        transcript_adapter=box["parser"],
    )


def _reopen(box):
    root = box["store"].root.parent
    domain = box["store"].domain
    box["store"].close()
    box["store"] = Store(root, domain)
    return box["store"]


def _log(box, text):
    path = transcript_path(box["parser_name"], box["tmp"] / "logs", box["session"])
    write_transcript(box["parser_name"], path, box["session"], [text], box["when"])
    box["log"] = path
    return path


def _deadline_capture(box, text, turn="t1"):
    _runner(box["runner"], box["marker"], DEADLINE)
    path = _log(box, text)
    engine = _engine(box)
    captured = engine.capture(
        session_id=box["session"],
        turn_id=turn,
        transcript_path=str(path),
        summary="",
    )
    assert captured.get("status") == "queued", captured
    engine._process(captured["id"])
    row = box["store"].capture_row(captured["id"])
    assert row["status"] == "pending", row
    failed = [item for item in regions(box["store"]) if item["status"] == "failed"]
    assert len(failed) == 1, failed
    assert "gap-recovery:" in (box["store"].continuation_reason(captured["id"]) or "")
    box["capture"] = captured["id"]
    box["region"] = failed[0]
    return captured["id"]


def _recover(box, body):
    store = _reopen(box)
    _runner(box["runner"], box["marker"], body)
    engine = _engine(box)
    _fire(store, box["capture"])
    engine._process(box["capture"])
    return engine


@pytest.mark.parametrize("parser_name", PARSERS_AND_IDS)
@pytest.mark.parametrize(
    ("label", "text", "small"),
    [
        ("small", "SMALLCTRL 映射 observed", True),
        ("large", "LARGECTRL " + ("y" * 1200), False),
    ],
)
def test_failed_region_recovers_once_after_growth_and_reopen(tmp_path, parser_name, label, text, small):
    box = _open_world(tmp_path, parser_name)
    try:
        _deadline_capture(box, text)
        span = box["region"]["finish"] - box["region"]["start"]
        if small:
            assert span < 1024, (label, span)
        else:
            assert span >= 1024, (label, span)
        assert spawn_count(box["marker"]) == 1
        tail = f"TAILCTRL {label} later turn"
        append_record(box["parser_name"], box["log"], box["session"], tail, box["when"] + 5)
        duplicate = _engine(box).capture(
            session_id=box["session"],
            turn_id="t1",
            transcript_path=str(box["log"]),
            summary="",
        )
        assert duplicate["duplicate"] is True
        assert spawn_count(box["marker"]) == 1
        _recover(box, SUCCESS)
        assert spawn_count(box["marker"]) == 2
        docs = _docs(box["store"])
        assert text.split()[0] in docs
        assert "TAILCTRL" not in docs
        row = box["store"].capture_row(box["capture"])
        if row["status"] == "pending":
            _fire(box["store"], box["capture"])
            _engine(box)._process(box["capture"])
        row = box["store"].capture_row(box["capture"])
        assert row["status"] == "organized", row
        docs = _docs(box["store"])
        assert text.split()[0] in docs
        assert "TAILCTRL" in docs
        assert docs.count(text.split()[0]) == 1
        assert docs.count("TAILCTRL") == 1
        assert spawn_count(box["marker"]) == 3
        assert unfinished_captures(box["store"]) == []
        assert box["store"].coverage_gaps() == []
        _fire(box["store"], box["capture"])
        _engine(box)._process(box["capture"])
        assert spawn_count(box["marker"]) == 3
        assert _docs(box["store"]).count(text.split()[0]) == 1
    finally:
        _close(box)


@pytest.mark.parametrize("parser_name", PARSERS_AND_IDS)
def test_half_line_and_split_utf8_do_not_break_the_original_range(tmp_path, parser_name):
    box = _open_world(tmp_path, parser_name)
    try:
        _deadline_capture(box, "UTF8CTRL 设备映射 observed")
        assert box["region"]["finish"] - box["region"]["start"] < 1024
        finish = box["region"]["finish"]
        complete = append_record(
            box["parser_name"], box["log"], box["session"], "TAILUTF 后续设备完成", box["when"] + 5
        )
        # Put back only a prefix that ends inside a multibyte sequence.
        original = box["log"].read_bytes()[:finish]
        body = complete[:-1]
        split = next(index for index, byte in enumerate(body) if byte >= 0x80)
        box["log"].write_bytes(original + body[: split + 1])
        _recover(box, SUCCESS)
        assert spawn_count(box["marker"]) == 2, box["store"].capture_row(box["capture"])
        docs = _docs(box["store"])
        assert "UTF8CTRL" in docs and "TAILUTF" not in docs
        box["log"].write_bytes(original + complete)
        row = box["store"].capture_row(box["capture"])
        if row["status"] == "pending":
            _fire(box["store"], box["capture"])
            _engine(box)._process(box["capture"])
        docs = _docs(box["store"])
        assert "UTF8CTRL" in docs and "TAILUTF" in docs
        assert docs.count("UTF8CTRL") == 1
        assert spawn_count(box["marker"]) == 3
        assert unfinished_captures(box["store"]) == []
    finally:
        _close(box)


@pytest.mark.parametrize("parser_name", PARSERS_AND_IDS)
def test_replaced_source_keeps_the_gap_without_a_recovery_model(tmp_path, parser_name):
    box = _open_world(tmp_path, parser_name)
    try:
        _deadline_capture(box, "REPLACECTRL original range")
        write_transcript(
            box["parser_name"], box["log"], box["session"], ["REPLACECTRL rewritten"], box["when"]
        )
        _recover(box, SUCCESS)
        row = box["store"].capture_row(box["capture"])
        assert row["status"] == "failed", row
        assert "gap retained" in row["detail"]
        assert spawn_count(box["marker"]) == 1
        assert "REPLACECTRL" not in _docs(box["store"])
        assert box["store"].coverage_gaps()
    finally:
        _close(box)


@pytest.mark.parametrize("parser_name", PARSERS_AND_IDS)
def test_second_failure_does_not_get_a_third_model_call(tmp_path, parser_name):
    box = _open_world(tmp_path, parser_name)
    try:
        _deadline_capture(box, "SECONDCTRL doomed range")
        append_record(
            box["parser_name"], box["log"], box["session"], "TAILCTRL still continues", box["when"] + 5
        )
        _recover(box, DEADLINE)
        row = box["store"].capture_row(box["capture"])
        assert row["status"] == "failed", row
        assert "gap retained" in row["detail"]
        assert spawn_count(box["marker"]) == 2
        _fire(box["store"], box["capture"])
        _runner(box["runner"], box["marker"], SUCCESS)
        _engine(box)._process(box["capture"])
        assert spawn_count(box["marker"]) == 2
        other = _engine(box).capture(
            session_id=box["session"],
            turn_id="t2",
            transcript_path=str(box["log"]),
            summary="",
        )
        assert other.get("duplicate") is not True
        _engine(box)._process(other["id"])
        assert "TAILCTRL" in _docs(box["store"])
        assert "SECONDCTRL" not in _docs(box["store"])
        assert spawn_count(box["marker"]) == 3
        assert any(item["status"] == "failed" for item in regions(box["store"]))
    finally:
        _close(box)


@pytest.mark.parametrize("parser_name", PARSERS_AND_IDS)
def test_killing_the_recovery_process_consumes_the_only_retry(tmp_path, parser_name):
    box = _open_world(tmp_path, parser_name)
    script = tmp_path / "recover_child.py"
    script.write_text(CHILD)
    kill_marker = tmp_path / "kill-spawns"
    retry_marker = tmp_path / "retry-spawns"
    try:
        _deadline_capture(box, "KILLCTRL original range")
        assert box["region"]["finish"] - box["region"]["start"] < 1024
        append_record(
            box["parser_name"], box["log"], box["session"], "TAILCTRL after kill", box["when"] + 5
        )
        root = box["store"].root.parent
        domain = box["store"].domain
        box["store"].close()
        env = os.environ.copy()
        env.update(
            {
                "GROK_CORE_REPO": str(REPO),
                "PARSER": str(parser_path(parser_name)),
                "STORE_ROOT": str(root),
                "STORE_DOMAIN": domain,
                "SETTINGS": str(box["settings"]),
                "ADMISSION": str(box["admission"]),
                "CAPTURE": box["capture"],
                "KILL_MARKER": str(kill_marker),
            }
        )
        proc = subprocess.run(
            [sys.executable, str(script), "recover"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            start_new_session=True,
            check=False,
        )
        # POSIX reports the signal as a negative code. Windows TerminateProcess
        # reports SIGTERM as a positive exit code. Either way the process did
        # not finish the recovery itself.
        expected = signal.SIGTERM if os.name == "nt" else -signal.SIGKILL
        assert proc.returncode == expected, (
            proc.returncode, proc.stdout, proc.stderr, spawn_count(kill_marker)
        )
        assert spawn_count(kill_marker) == 1
        box["store"] = Store(root, domain)
        box["marker"] = retry_marker
        engine = _engine(box)
        engine.budget.recover_interrupted()
        _runner(box["runner"], retry_marker, SUCCESS)
        _fire(box["store"], box["capture"])
        engine._process(box["capture"])
        row = box["store"].capture_row(box["capture"])
        assert row["status"] == "failed", row
        assert "gap retained" in row["detail"]
        assert spawn_count(retry_marker) == 0
        assert "KILLCTRL" not in _docs(box["store"])
        other = engine.capture(
            session_id=box["session"],
            turn_id="t2",
            transcript_path=str(box["log"]),
            summary="",
        )
        engine._process(other["id"])
        assert "TAILCTRL" in _docs(box["store"])
        assert spawn_count(retry_marker) == 1
        assert any(item["status"] == "failed" for item in regions(box["store"]))
    finally:
        if box["store"] is not None:
            try:
                _close(box)
            except Exception:
                pass
