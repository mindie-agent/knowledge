"""Recovery ordering that wall-clock arguments do not cover.

One case kills the first model and uses Engine.start, which is the
crash-window path. The other pauses the loser after reserve fails and
before keep_gap, lets the winner commit, then releases the loser. A
successful region must stay succeeded.
"""

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store

from conftest import make_admission, write_settings
from lane_support import REPO, SESSIONS, entry_documents, load_parser, reap, transcript_path, write_transcript

PARSER = "kimi"
SESSION = SESSIONS[PARSER]
BLOCKING_MODEL = textwrap.dedent(
    """\
    import os, time
    from pathlib import Path
    Path(os.environ["SPAWNED"]).write_text("x\\n")
    deadline = time.time() + 30
    while not Path(os.environ["RELEASE"]).exists():
        if time.time() > deadline:
            raise SystemExit("model was not released")
        time.sleep(0.02)
    raise SystemExit(0)
    """
)
SUCCESS_MODEL = textwrap.dedent(
    """\
    import json, os, sys, time
    from pathlib import Path
    Path(os.environ["SPAWNED"]).write_text(
        (Path(os.environ["SPAWNED"]).read_text() if Path(os.environ["SPAWNED"]).exists() else "")
        + "x\\n"
    )
    deadline = time.time() + 30
    while not Path(os.environ["WINNER_GO"]).exists():
        if time.time() > deadline:
            raise SystemExit("winner model was not released")
        time.sleep(0.02)
    payload = json.load(sys.stdin)
    text = payload.get("increment", "")[:400]
    print(json.dumps({"entries":[{"entry_id":None,"title":"Observed",
        "summary":"interleaving","content":text,"conditions":{}}]}))
    """
)
LOSER_FORBIDDEN_MODEL = textwrap.dedent(
    """\
    import os, sys
    from pathlib import Path
    Path(os.environ["LOSER_SPAWNED"]).write_text("spawned\\n")
    raise SystemExit("loser spawned a model")
    """
)


def _open_box(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    settings = tmp_path / "community.json"
    write_settings(settings, enabled=True, roots=[project])
    admission = make_admission(tmp_path, project_root=project, session=SESSION)
    store = Store(tmp_path / "store", "test")
    parser = load_parser(PARSER)
    when = time.time() + 180
    log = transcript_path(PARSER, tmp_path / "logs", SESSION)
    write_transcript(PARSER, log, SESSION, ["RACECTRL original range"], when)
    return {
        "tmp": tmp_path,
        "settings": settings,
        "admission": admission,
        "store": store,
        "parser": parser,
        "log": log,
        "root": store.root.parent,
        "domain": store.domain,
    }


def _engine(box, runner):
    return Engine(
        box["store"],
        agent_command=[sys.executable, str(runner)],
        settings_path=box["settings"],
        admission=Admission(box["admission"]),
        transcript_adapter=box["parser"],
    )


def _deadline(box, runner):
    runner.write_text("import sys\nsys.exit(124)\n")
    engine = _engine(box, runner)
    captured = engine.capture(
        session_id=SESSION,
        turn_id="t-race",
        transcript_path=str(box["log"]),
        summary="",
    )
    assert captured["status"] == "queued", captured
    engine._process(captured["id"])
    row = box["store"].capture_row(captured["id"])
    assert row["status"] == "pending", row
    assert (box["store"].continuation_reason(captured["id"]) or "").startswith("gap-recovery:")
    box["capture"] = captured["id"]
    return captured["id"]


def _docs(store):
    return "\n".join(entry_documents(store))


def test_start_schedules_one_recovery_after_the_first_model_is_killed(tmp_path):
    box = _open_box(tmp_path)
    runner = tmp_path / "block.py"
    runner.write_text(BLOCKING_MODEL)
    spawned = tmp_path / "spawned"
    release = tmp_path / "release"
    script = tmp_path / "child.py"
    script.write_text(_process_script())
    proc = None
    starter = None
    try:
        fresh = _engine(box, runner).capture(
            session_id=SESSION,
            turn_id="t-kill-first",
            transcript_path=str(box["log"]),
            summary="",
        )
        assert fresh["status"] == "queued", fresh
        box["store"].close()
        env = os.environ.copy()
        env.update(
            {
                "GROK_CORE_REPO": str(REPO),
                "PARSER": box["parser"].__file__,
                "STORE_ROOT": str(box["root"]),
                "STORE_DOMAIN": box["domain"],
                "SETTINGS": str(box["settings"]),
                "ADMISSION": str(box["admission"]),
                "RUNNER": str(runner),
                "CAPTURE": fresh["id"],
                "SPAWNED": str(spawned),
                "RELEASE": str(release),
            }
        )
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            cwd=tmp_path,
            env=env,
            start_new_session=True,
        )
        deadline = time.time() + 20
        while not spawned.exists():
            if proc.poll() is not None or time.time() > deadline:
                out = proc.poll()
                pytest.fail(f"first model did not start (exit {out})")
            time.sleep(0.02)
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        box["store"] = Store(box["root"], box["domain"])
        success = tmp_path / "success.py"
        success.write_text(
            "import json,sys\n"
            "payload=json.load(sys.stdin)\n"
            "text=payload.get('increment','')[:400]\n"
            "print(json.dumps({'entries':[{'entry_id':None,'title':'Observed',"
            "'summary':'crash window','content':text,'conditions':{}}]}))\n"
        )
        starter = _engine(box, success)
        starter.start()
        starter.stop.set()
        starter.thread.join(timeout=5)
        starter.outbox_thread.join(timeout=5)
        assert not starter.thread.is_alive()
        assert not starter.outbox_thread.is_alive()
        reason = box["store"].continuation_reason(fresh["id"]) or ""
        assert reason.startswith("gap-recovery:"), reason
        box["store"].db.execute(
            "UPDATE continuations SET due=0, eligible=1 WHERE capture_id=?",
            (fresh["id"],),
        )
        box["store"].db.commit()
        # A later service, not the stopping one, runs the scheduled recovery.
        engine = _engine(box, success)
        engine._process(fresh["id"])
        row = box["store"].capture_row(fresh["id"])
        reason = box["store"].continuation_reason(fresh["id"])
        with box["store"].lock:
            regions = [
                dict(item)
                for item in box["store"].db.execute(
                    "SELECT status, recovery, detail FROM regions"
                )
            ]
        assert row["status"] == "organized", {
            "status": row["status"],
            "detail": row["detail"],
            "reason": reason,
            "regions": regions,
        }
        assert "RACECTRL" in _docs(box["store"])
        assert _docs(box["store"]).count("RACECTRL") == 1
    finally:
        reap(proc, group=True)
        if starter is not None:
            starter.stop.set()
            starter.thread.join(timeout=2)
            starter.outbox_thread.join(timeout=2)
        try:
            box["store"].close()
        except Exception:
            pass


def test_late_keep_gap_must_not_overwrite_a_committed_recovery(tmp_path):
    box = _open_box(tmp_path)
    deadline_runner = tmp_path / "deadline.py"
    winner_proc = None
    loser_proc = None
    winner_go = None
    loser_go = None
    try:
        _deadline(box, deadline_runner)
        box["store"].close()
        winner_model = tmp_path / "winner_model.py"
        winner_model.write_text(SUCCESS_MODEL)
        loser_model = tmp_path / "loser_model.py"
        loser_model.write_text(LOSER_FORBIDDEN_MODEL)
        winner = tmp_path / "winner.py"
        loser = tmp_path / "loser.py"
        winner.write_text(_worker_script())
        loser.write_text(_worker_script(pause_after_reserve_loss=True))
        spawned = tmp_path / "spawned"
        winner_go = tmp_path / "winner-go"
        loser_at = tmp_path / "loser-at-gap"
        loser_go = tmp_path / "loser-go"
        loser_spawned = tmp_path / "loser-spawned"
        base = {
            "GROK_CORE_REPO": str(REPO),
            "PARSER": box["parser"].__file__,
            "STORE_ROOT": str(box["root"]),
            "STORE_DOMAIN": box["domain"],
            "SETTINGS": str(box["settings"]),
            "ADMISSION": str(box["admission"]),
            "CAPTURE": box["capture"],
            "SPAWNED": str(spawned),
            "WINNER_GO": str(winner_go),
            "LOSER_AT_GAP": str(loser_at),
            "LOSER_GO": str(loser_go),
            "LOSER_SPAWNED": str(loser_spawned),
        }
        winner_env = os.environ.copy()
        winner_env.update(base)
        winner_env["RUNNER"] = str(winner_model)
        loser_env = os.environ.copy()
        loser_env.update(base)
        loser_env["RUNNER"] = str(loser_model)
        winner_proc = subprocess.Popen(
            [sys.executable, str(winner)],
            cwd=tmp_path,
            env=winner_env,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.time() + 20
        while spawned.read_text().count("x") < 1 if spawned.exists() else True:
            if winner_proc.poll() is not None or time.time() > deadline:
                out, err = winner_proc.communicate(timeout=1)
                pytest.fail(f"winner did not reach the model ({winner_proc.returncode}) {out} {err}")
            time.sleep(0.02)
        loser_proc = subprocess.Popen(
            [sys.executable, str(loser)],
            cwd=tmp_path,
            env=loser_env,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.time() + 20
        while not loser_at.exists():
            if loser_proc.poll() is not None or time.time() > deadline:
                out, err = loser_proc.communicate(timeout=1)
                pytest.fail(
                    f"loser did not pause after losing reserve "
                    f"({loser_proc.returncode}) {out} {err}"
                )
            time.sleep(0.02)
        winner_go.write_text("go\n")
        w_out, w_err = winner_proc.communicate(timeout=20)
        assert winner_proc.returncode == 0, (w_out, w_err)
        # Winner has committed. Only then may the loser write.
        loser_go.write_text("go\n")
        l_out, l_err = loser_proc.communicate(timeout=20)
        assert loser_proc.returncode == 0, (l_out, l_err)
        assert not loser_spawned.exists()
        assert spawned.read_text().count("x") == 1
        box["store"] = Store(box["root"], box["domain"])
        row = box["store"].capture_row(box["capture"])
        assert row["status"] == "organized", {
            "status": row["status"],
            "detail": row["detail"],
        }
        with box["store"].lock:
            regions = [
                dict(item)
                for item in box["store"].db.execute("SELECT status, detail FROM regions")
            ]
        assert regions
        assert all(item["status"] == "succeeded" for item in regions), regions
        assert _docs(box["store"]).count("RACECTRL") == 1
    finally:
        # Release any waiter first so a blocked model can exit, then kill
        # the group if it is still alive. Assertion failures must not leave it.
        for flag in (winner_go, loser_go):
            if flag is not None and not flag.exists():
                try:
                    flag.write_text("stop\n")
                except OSError:
                    pass
        reap(winner_proc, group=True)
        reap(loser_proc, group=True)
        try:
            box["store"].close()
        except Exception:
            pass


def test_reap_kills_children_after_the_leader_exits(tmp_path):
    """The leader exits first. Its 60s child stays in that process group."""
    child_pid_path = tmp_path / "child.pid"
    child_alive = tmp_path / "child.alive"
    parent_script = tmp_path / "leader.py"
    parent_script.write_text(
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        "proc = subprocess.Popen(\n"
        "    [sys.executable, '-c',\n"
        "     'import os, signal, time, pathlib; signal.signal(signal.SIGHUP, signal.SIG_IGN); pathlib.Path(os.environ[\"CHILD_ALIVE\"]).write_text(\"1\"); time.sleep(60)'],\n"
        "    env=os.environ.copy(),\n"
        ")\n"
        "Path(os.environ['CHILD_PID']).write_text(str(proc.pid))\n"
    )
    env = os.environ.copy()
    env["CHILD_PID"] = str(child_pid_path)
    env["CHILD_ALIVE"] = str(child_alive)
    leader = subprocess.Popen(
        [sys.executable, str(parent_script)],
        env=env,
        start_new_session=True,
    )
    try:
        deadline = time.time() + 10
        while not (
            child_pid_path.exists()
            and child_alive.exists()
            and leader.poll() is not None
        ):
            if time.time() > deadline:
                pytest.fail(
                    f"leader did not exit ahead of its child (poll={leader.poll()})"
                )
            time.sleep(0.02)
        child_pid = int(child_pid_path.read_text())
        os.kill(child_pid, 0)
        assert leader.returncode == 0
        reap(leader, group=True)
        for _ in range(50):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            pytest.fail(f"child {child_pid} still alive after reap of a dead leader")
    finally:
        reap(leader, group=True)


def _process_script():
    return textwrap.dedent(
        """\
        import os, sys
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
            agent_command=[sys.executable, os.environ["RUNNER"]],
            settings_path=os.environ["SETTINGS"],
            admission=Admission(os.environ["ADMISSION"]),
            transcript_adapter=module,
        )
        engine._process(os.environ["CAPTURE"])
        store.close()
        """
    )


def _worker_script(*, pause_after_reserve_loss=False):
    pause = "True" if pause_after_reserve_loss else "False"
    return textwrap.dedent(
        f"""\
        import os, sys, time
        from pathlib import Path
        sys.path.insert(0, os.environ["GROK_CORE_REPO"])
        import importlib.util
        from mindie_knowledge.loop.activation import Admission
        from mindie_knowledge.loop.budget import BudgetExceeded
        from mindie_knowledge.loop.engine import Engine
        from mindie_knowledge.loop.store import Store
        spec = importlib.util.spec_from_file_location("frozen_parser", os.environ["PARSER"])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        store = Store(os.environ["STORE_ROOT"], os.environ["STORE_DOMAIN"])
        engine = Engine(
            store,
            agent_command=[sys.executable, os.environ["RUNNER"]],
            settings_path=os.environ["SETTINGS"],
            admission=Admission(os.environ["ADMISSION"]),
            transcript_adapter=module,
        )
        if {pause}:
            real_reserve = engine.budget.reserve
            def reserve(*args, **kwargs):
                try:
                    return real_reserve(*args, **kwargs)
                except BudgetExceeded:
                    # After this reserve lost, before the engine writes any
                    # recovery outcome. Not tied to a particular store method.
                    Path(os.environ["LOSER_AT_GAP"]).write_text("reserve-failed")
                    deadline = time.time() + 30
                    while not Path(os.environ["LOSER_GO"]).exists():
                        if time.time() > deadline:
                            raise SystemExit("loser was not released")
                        time.sleep(0.02)
                    raise
            engine.budget.reserve = reserve
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
