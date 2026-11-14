"""Windows CP936/user-scoped Git and WSL2 DrvFS/non-systemd regressions."""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from mindie_knowledge import _host_exec, gitbin, release_http
from mindie_knowledge.loop import service_host, systemd_service


def _fake_pe_bytes() -> bytes:
    buf = bytearray(256)
    buf[0:2] = b"MZ"
    buf[0x3C:0x40] = (0x80).to_bytes(4, "little")
    buf[0x80:0x84] = b"PE\x00\x00"
    return bytes(buf)


@pytest.mark.skipif(os.name == "nt", reason="POSIX/WSL2 DrvFS and PE shadow exclusion")
def test_which_native_rejects_pe_drvfs_and_symlink_dotdot_bypass(tmp_path, monkeypatch):
    drvfs_root = tmp_path / "mnt_c"
    drvfs_sub = drvfs_root / "tools" / "sub"
    drvfs_sub.mkdir(parents=True)
    drvfs_shim = drvfs_root / "tools" / "gh"
    drvfs_shim.write_text("#!/bin/sh\necho bad\n", encoding="utf-8")
    drvfs_shim.chmod(0o755)

    pe_dir = tmp_path / "pe_bin"
    pe_dir.mkdir()
    pe_git = pe_dir / "git"
    pe_git.write_bytes(_fake_pe_bytes())
    pe_git.chmod(0o755)

    safe_dir = tmp_path / "safe"
    safe_dir.mkdir()
    (safe_dir / "link").symlink_to(drvfs_sub)

    native_dir = tmp_path / "native"
    native_dir.mkdir()
    for name in ("gh", "git"):
        p = native_dir / name
        p.write_text("#!/bin/sh\necho native-tok\n", encoding="utf-8")
        p.chmod(0o755)

    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"22 1 8:2 / / rw,relatime - ext4 /dev/sdb rw\n"
        f"99 22 0:42 / {drvfs_root.resolve()} rw,noatime - 9p C:\\134 rw,aname=drvfs;metadata\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(_host_exec, "_MOUNTINFO_PATH", str(mountinfo))

    tricky_path = f"{safe_dir}/link/..:{pe_dir}:{native_dir}"
    assert _host_exec.is_wsl_drvfs(f"{safe_dir}/link/../gh") is True
    assert _host_exec.which_native("gh", path=tricky_path) == str(native_dir / "gh")
    assert _host_exec.which_native("git", path=tricky_path) == str(native_dir / "git")

    monkeypatch.delenv(gitbin.ENV_NAME, raising=False)
    monkeypatch.setenv("PATH", tricky_path)
    assert gitbin.resolve_git() == str(native_dir / "git")

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    release_http.token.cache_clear()
    try:
        assert release_http.token() == "native-tok"
    finally:
        release_http.token.cache_clear()


@pytest.mark.skipif(os.name == "nt", reason="POSIX stat/lstat TimeoutError propagation")
def test_host_exec_and_service_host_propagate_timeout_error(tmp_path, monkeypatch):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_host_exec.os, "stat", lambda p: (_ for _ in ()).throw(TimeoutError("stat_boom")))
        with pytest.raises(TimeoutError, match="stat_boom"):
            _host_exec.which_native("git", path=str(tmp_path))

    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("22 1 8:2 / / rw,relatime - ext4 /dev/sdb rw\n", encoding="utf-8")
    monkeypatch.setattr(_host_exec, "_MOUNTINFO_PATH", str(mountinfo))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_host_exec.os, "lstat", lambda p, *a, **kw: (_ for _ in ()).throw(TimeoutError("lstat_boom")))
        with pytest.raises(TimeoutError, match="lstat_boom"):
            _host_exec.is_wsl_drvfs(tmp_path / "x")


@pytest.mark.skipif(os.name == "nt", reason="Linux systemd user-bus detection and launch-record guard")
def test_linux_non_systemd_backend_selection_and_launch_record_preservation(tmp_path, monkeypatch):
    monkeypatch.setattr(service_host.sys, "platform", "linux")
    monkeypatch.setattr(service_host.os, "name", "posix")
    cfg = {"root": str(tmp_path / "data"), "domain": "vllm-ascend"}

    # Missing user bus and no existing launch.json -> None (uses direct spawn_service)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "no-runtime"))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={tmp_path / 'no-bus'}")
    assert service_host.linux_systemd_user_bus_available() is False
    assert service_host.platform_backend() is None
    assert service_host.backend_for_config(cfg) is None

    # Pre-existing .systemd-service/launch.json MUST never be bypassed even if user bus disappears
    launch = Path(cfg["root"]) / cfg["domain"] / ".systemd-service" / "launch.json"
    launch.parent.mkdir(parents=True)
    launch.write_text(json.dumps({"attempt": "a" * 32}), encoding="utf-8")
    assert systemd_service.has_launch_record(cfg) is True
    assert service_host.backend_for_config(cfg) is systemd_service


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="AF_UNIX bus socket check on Linux")
def test_linux_user_runtime_dir_detects_owned_bus_socket(tmp_path, monkeypatch):
    rt = tmp_path / "rt"
    rt.mkdir(mode=0o700)
    sock_path = rt / "bus"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(sock_path))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(rt))
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        assert service_host.linux_user_runtime_dir() == str(rt)
    finally:
        srv.close()


def test_component_runtime_inventory_handles_utf8_under_isolated_mode(tmp_path):
    from mindie_knowledge import component_runtime

    ver = component_runtime.read_versions(sys.executable, ["mindie-knowledge"])
    assert ver == {"mindie-knowledge": "0.13.18"}
