"""A real synthetic writer cannot outlive its temporary profile's owner."""
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import threading
import time

import pytest

from mindie_knowledge.loop import process, summary_scratch as scratch
from mindie_knowledge.loop.service_context import ContextError, validate


@pytest.fixture(autouse=True)
def clear_owner_environment(monkeypatch):
    for key in (scratch.ENVIRONMENT_KEY, "MINDIE_MAINTENANCE_GROUP",
                "MINDIE_MAINTENANCE_OWNER"):
        monkeypatch.delenv(key, raising=False)


def test_direct_profile_has_no_deferred_cleanup_owner():
    path, managed = scratch.create_profile(prefix="mindie-direct-test-")
    try:
        assert not managed
        assert Path(path).is_dir()
    finally:
        shutil.rmtree(path)


@pytest.mark.parametrize("declaration", [
    None, "{}", "[]", "not-json",
    json.dumps(dict(schema=1, owner=1, path="/")),
    json.dumps(dict(schema=True, owner=1, path="/")),
])
def test_incomplete_or_foreign_owner_fails_before_a_profile_is_created(monkeypatch, declaration):
    monkeypatch.setenv("MINDIE_MAINTENANCE_GROUP", "1")
    monkeypatch.setenv("MINDIE_MAINTENANCE_OWNER", str(os.getppid()))
    if declaration is not None:
        monkeypatch.setenv(scratch.ENVIRONMENT_KEY, declaration)
    with pytest.raises(scratch.ScratchOwnershipError) as raised:
        scratch.create_profile(prefix="must-not-create-")
    assert raised.value.mindie_category == "configuration"


def test_scratch_declaration_cannot_be_user_launch_configuration():
    for declaration in ({"environment": [scratch.ENVIRONMENT_KEY]},
                        {"paths": {scratch.ENVIRONMENT_KEY: str(Path.cwd())}}):
        with pytest.raises(ContextError):
            validate({"service_launch_context": declaration})


def test_result_callback_precedes_tree_and_profile_cleanup():
    writer = (
        "import sys,time; from pathlib import Path; "
        "p=Path(sys.argv[1]); i=0\n"
        "while True:\n"
        " p.write_text(str(i)); i+=1; time.sleep(.01)\n"
    )
    command = (
        "import json,subprocess,sys,time; from pathlib import Path; "
        "from mindie_knowledge.loop.summary_scratch import create_profile; "
        "profile,managed=create_profile(prefix='native-'); p=Path(profile); "
        f"child=subprocess.Popen([sys.executable,'-c',{writer!r},str(p/'writing')]); "
        "\nwhile not (p/'writing').exists(): time.sleep(.01)\n"
        "print(json.dumps(dict(profile=profile,managed=managed)),flush=True)\n"
    )
    saved, cleanup = [], {}

    def record(raw):
        value = json.loads(raw)
        profile = Path(value["profile"])
        assert value["managed"]
        assert profile.is_dir()
        if os.name == "posix":
            assert stat.S_IMODE(profile.parent.stat().st_mode) == 0o700
        old = (profile / "writing").read_text()
        deadline = time.monotonic() + 2
        while (profile / "writing").read_text() == old and time.monotonic() < deadline:
            time.sleep(.01)
        assert (profile / "writing").read_text() != old
        saved.append(value)

    raw = process.bounded_run([sys.executable, "-c", command], "", max_output=4096,
                              timeout=10, on_result=record, cleanup_receipt=cleanup)
    assert json.loads(raw) == saved[0]
    assert not cleanup.get("cleanup_failed", False)
    assert not Path(saved[0]["profile"]).parent.exists()


@pytest.mark.parametrize("failure", ["cancel", "callback"])
def test_failed_operation_still_closes_tree_before_removing_private_root(tmp_path, failure):
    observed = tmp_path / "created.json"
    command = (
        "import json,time; from pathlib import Path; "
        "from mindie_knowledge.loop.summary_scratch import create_profile; "
        "profile,_=create_profile(prefix='native-'); "
        f"Path({str(observed)!r}).write_text(json.dumps(profile)); "
        + ('print("invalid",flush=True); ' if failure == "callback" else "")
        + "time.sleep(30)"
    )
    cancel = threading.Event()
    def cancel_after_start():
        until = time.monotonic() + 5
        while not observed.exists() and time.monotonic() < until:
            time.sleep(.01)
        cancel.set()
    trigger = threading.Thread(target=cancel_after_start) if failure == "cancel" else None
    if trigger:
        trigger.start()
    def record(raw):
        if failure == "cancel":
            return
        raise ValueError("original result error")
    try:
        with pytest.raises(process.MaintenanceCancelled if failure == "cancel" else ValueError):
            process.bounded_run([sys.executable, "-c", command], "", max_output=4096,
                                timeout=10, cancel=cancel,
                                on_result=record,
                                cleanup_receipt={})
    finally:
        if trigger:
            trigger.join(timeout=5)
    assert not Path(json.loads(observed.read_text())).parent.exists()


def test_scratch_cleanup_failure_preserves_result_and_known_usage(monkeypatch):
    saved, cleanup, retained = [], {}, []
    original = process.shutil.rmtree
    def fail(path):
        retained.append(Path(path))
        raise PermissionError("PRIVATE_PATH_CANARY")
    monkeypatch.setattr(process.shutil, "rmtree", fail)
    command = [sys.executable, "-c",
               'print(\'{"status":"returned","usage":{"input_tokens":7}}\',flush=True)']
    try:
        raw = process.bounded_run(command, "", max_output=4096,
                                  on_result=lambda raw: saved.append(json.loads(raw)),
                                  cleanup_receipt=cleanup)
        assert json.loads(raw) == saved[0]
        assert saved[0]["usage"]["input_tokens"] == 7
        assert cleanup == {"cleanup_failed": True}
        assert retained[0].is_dir()
    finally:
        for path in retained:
            original(path)


def test_start_failure_removes_only_new_scratch_and_remains_uncalled(monkeypatch):
    roots = []
    create = scratch.create_root
    def capture():
        root = create()
        roots.append(root)
        return root
    monkeypatch.setattr(scratch, "create_root", capture)
    with pytest.raises(OSError) as raised:
        process.bounded_run(["missing-mindie-fixture-command"], "", max_output=4096,
                            on_result=lambda _: None)
    assert raised.value.mindie_execution == "not_started"
    assert not roots[0].exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows owned Job contract")
def test_foreign_named_job_is_not_scratch_authority(monkeypatch):
    from mindie_knowledge.windows_process import spawn_owned, terminate_owned
    root = scratch.create_root()
    child = spawn_owned([sys.executable, "-c", "import time;time.sleep(30)"],
                        job_name=scratch.job_name(root))
    try:
        value = json.loads(scratch.declaration(root))
        value["owner"] = os.getppid()
        monkeypatch.setenv("MINDIE_MAINTENANCE_GROUP", "1")
        monkeypatch.setenv("MINDIE_MAINTENANCE_OWNER", str(os.getppid()))
        monkeypatch.setenv(scratch.ENVIRONMENT_KEY, json.dumps(value))
        with pytest.raises(scratch.ScratchOwnershipError):
            scratch.create_profile(prefix="must-not-create-")
        assert list(root.iterdir()) == []
        assert child.poll() is None
    finally:
        terminate_owned(child, wait=True)
        child.wait(timeout=5)
        shutil.rmtree(root)


@pytest.mark.skipif(os.name != "nt", reason="Windows owned Job contract")
def test_existing_job_identity_is_rejected_without_changing_its_owner():
    from mindie_knowledge.windows_process import spawn_owned, terminate_owned
    root = scratch.create_root()
    command = [sys.executable, "-c", "import time;time.sleep(30)"]
    child = spawn_owned(command, job_name=scratch.job_name(root))
    try:
        with pytest.raises(OSError, match="identity already exists"):
            spawn_owned(command, job_name=scratch.job_name(root))
        assert child.poll() is None
    finally:
        terminate_owned(child, wait=True)
        child.wait(timeout=5)
        shutil.rmtree(root)
