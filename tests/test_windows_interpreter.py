"""Interpreter identity at the scheduler boundary, including issue #115."""
import os
from pathlib import Path
import shutil
import sys

import pytest

from mindie_knowledge.loop import windows_service as host
from mindie_knowledge.windows_interpreter import require_windowless
from windows_launcher_fixture import write_launcher


@pytest.mark.parametrize("magic", [0x10B, 0x20B], ids=["PE32", "PE32+"])
def test_gui_pe_is_accepted_but_console_launcher_is_rejected(tmp_path, magic):
    launcher = write_launcher(tmp_path / "pythonw.exe", magic=magic)
    require_windowless(launcher)
    write_launcher(launcher, magic=magic, subsystem=3)
    with pytest.raises(host.ServiceHostError) as caught:
        require_windowless(launcher)
    assert (caught.value.stage, caught.value.code, caught.value.uncertain) == (
        "interpreter", "windowless_interpreter_invalid", False)


def test_invalid_new_entry_never_reaches_task_registration(tmp_path, monkeypatch):
    import json
    config = dict(root=str(tmp_path / "data"), domain="test")
    config_path = tmp_path / "engine.json"
    config_path.write_text(json.dumps(config))
    launcher = write_launcher(tmp_path / "pythonw.exe", subsystem=3)
    monkeypatch.setattr(sys, "executable", str(launcher.with_name("python.exe")))
    monkeypatch.setattr(host, "_private", lambda root: root.mkdir(parents=True) or "S-1-5-21-123")
    monkeypatch.setattr(host, "_task", lambda *_: pytest.fail("invalid interpreter reached the manager"))
    with pytest.raises(host.ServiceHostError, match="interpreter:windowless_interpreter_invalid"):
        host.request_start(config_path, config)
    root = host._paths(config)
    assert not (root / "launch.json").exists()
    assert not (root / "operation.json").exists()
    assert not list(root.glob("*.context"))


@pytest.mark.parametrize("contents", [None, b"", b"not a PE image"])
def test_missing_or_broken_environment_has_an_interpreter_error(tmp_path, contents):
    launcher = tmp_path / "pythonw.exe"
    if contents is not None:
        launcher.write_bytes(contents)
    with pytest.raises(host.ServiceHostError) as caught:
        require_windowless(launcher)
    assert caught.value.stage == "interpreter"
    assert caught.value.code == ("windowless_interpreter_missing" if contents is None
                                 else "windowless_interpreter_invalid")


def test_interpreter_read_timeout_is_not_downgraded(tmp_path, monkeypatch):
    def timed_out(*args, **kwargs):
        raise TimeoutError("interpreter read timed out")
    monkeypatch.setattr(Path, "open", timed_out)
    with pytest.raises(TimeoutError):
        require_windowless(tmp_path / "pythonw.exe")


@pytest.mark.skipif(os.name != "nt", reason="real Windows Python launchers")
def test_real_console_executable_named_pythonw_is_rejected(tmp_path):
    require_windowless(Path(sys.executable).with_name("pythonw.exe"))
    impostor = tmp_path / "pythonw.exe"
    shutil.copyfile(sys.executable, impostor)
    with pytest.raises(host.ServiceHostError, match="windowless_interpreter_invalid"):
        require_windowless(impostor)
