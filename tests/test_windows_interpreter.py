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


@pytest.mark.parametrize("magic", [0x10B, 0x20B], ids=["PE32", "PE32+"])
def test_pyvenv_cfg_gui_base_pythonw_passes(tmp_path, magic):
    base_dir = tmp_path / "base-python"
    base_dir.mkdir()
    write_launcher(base_dir / "pythonw.exe", magic=magic, subsystem=2)
    venv_dir = tmp_path / ".venv"
    scripts_dir = venv_dir / "Scripts"
    scripts_dir.mkdir(parents=True)
    venv_launcher = write_launcher(scripts_dir / "pythonw.exe", magic=magic, subsystem=2)
    (venv_dir / "pyvenv.cfg").write_text(f"home = {base_dir}\nversion_info = 3.13\n", encoding="utf-8", newline="\n")
    require_windowless(venv_launcher)


def test_pyvenv_cfg_console_base_pythonw_is_rejected(tmp_path):
    base_dir = tmp_path / "base-python"
    base_dir.mkdir()
    write_launcher(base_dir / "pythonw.exe", subsystem=3)
    venv_dir = tmp_path / ".venv"
    scripts_dir = venv_dir / "Scripts"
    scripts_dir.mkdir(parents=True)
    venv_launcher = write_launcher(scripts_dir / "pythonw.exe", subsystem=2)
    (venv_dir / "pyvenv.cfg").write_text(f"home = {base_dir}\n", encoding="utf-8", newline="\n")
    with pytest.raises(host.ServiceHostError) as caught:
        require_windowless(venv_launcher)
    assert (caught.value.stage, caught.value.code, caught.value.uncertain) == (
        "interpreter", "windowless_interpreter_invalid", False,
    )


def test_pyvenv_cfg_missing_base_pythonw_is_rejected(tmp_path):
    base_dir = tmp_path / "missing-base-python"
    base_dir.mkdir()
    venv_dir = tmp_path / ".venv"
    scripts_dir = venv_dir / "Scripts"
    scripts_dir.mkdir(parents=True)
    venv_launcher = write_launcher(scripts_dir / "pythonw.exe", subsystem=2)
    (venv_dir / "pyvenv.cfg").write_text(f"home = {base_dir}\n", encoding="utf-8", newline="\n")
    with pytest.raises(host.ServiceHostError) as caught:
        require_windowless(venv_launcher)
    assert (caught.value.stage, caught.value.code, caught.value.uncertain) == (
        "interpreter", "windowless_interpreter_missing", False,
    )


def test_pyvenv_cfg_malformed_is_rejected(tmp_path):
    venv_dir = tmp_path / ".venv"
    scripts_dir = venv_dir / "Scripts"
    scripts_dir.mkdir(parents=True)
    venv_launcher = write_launcher(scripts_dir / "pythonw.exe", subsystem=2)
    (venv_dir / "pyvenv.cfg").write_text("[broken\nnot_a_pair\n", encoding="utf-8", newline="\n")
    with pytest.raises(host.ServiceHostError) as caught:
        require_windowless(venv_launcher)
    assert (caught.value.stage, caught.value.code, caught.value.uncertain) == (
        "interpreter", "windowless_interpreter_invalid", False,
    )


def test_pyvenv_cfg_sibling_precedence_and_pe_offset_cap(tmp_path):
    import struct

    gui_base = tmp_path / "gui-base"
    gui_base.mkdir()
    write_launcher(gui_base / "pythonw.exe", subsystem=2)
    cui_base = tmp_path / "cui-base"
    cui_base.mkdir()
    write_launcher(cui_base / "pythonw.exe", subsystem=3)

    venv_dir = tmp_path / ".venv"
    scripts_dir = venv_dir / "Scripts"
    scripts_dir.mkdir(parents=True)
    venv_launcher = write_launcher(scripts_dir / "pythonw.exe", subsystem=2)

    # Sibling Scripts/pyvenv.cfg takes precedence over parent .venv/pyvenv.cfg (PC/venvlauncher.c:184).
    (venv_dir / "pyvenv.cfg").write_text(f"home = {gui_base}\n", encoding="utf-8", newline="\n")
    (scripts_dir / "pyvenv.cfg").write_text(f"home = {cui_base}\n", encoding="utf-8", newline="\n")
    with pytest.raises(host.ServiceHostError) as caught:
        require_windowless(venv_launcher)
    assert caught.value.code == "windowless_interpreter_invalid"

    (venv_dir / "pyvenv.cfg").write_text(f"home = {cui_base}\n", encoding="utf-8", newline="\n")
    (scripts_dir / "pyvenv.cfg").write_text(f"home = {gui_base}\n", encoding="utf-8", newline="\n")
    require_windowless(venv_launcher)

    # e_lfanew > 16 MiB is rejected as invalid PE header offset.
    oversized = bytearray(write_launcher(tmp_path / "oversized-offset.exe", subsystem=2).read_bytes())
    struct.pack_into("<I", oversized, 60, 16 * 1024 * 1024 + 4)
    bad_pe = tmp_path / "oversized-offset.exe"
    bad_pe.write_bytes(bytes(oversized))
    with pytest.raises(host.ServiceHostError) as caught_offset:
        require_windowless(bad_pe)
    assert caught_offset.value.code == "windowless_interpreter_invalid"
