"""Git executable resolution: fixed order, honest failure, pinned spawns."""

import os
import shutil
from types import SimpleNamespace

import pytest

from mindie_knowledge import gitbin
from mindie_knowledge.gitbin import GitUnavailableError, resolve_git


@pytest.fixture
def no_pin(monkeypatch):
    monkeypatch.delenv(gitbin.ENV_NAME, raising=False)


def test_explicit_pin_wins_over_everything(tmp_path, monkeypatch, no_pin):
    pinned = tmp_path / "pinned-git"
    pinned.write_text("")
    monkeypatch.setenv(gitbin.ENV_NAME, str(pinned))
    assert resolve_git() == str(pinned)


def test_pin_naming_a_missing_file_fails_visibly_and_never_falls_through(tmp_path, monkeypatch, no_pin):
    monkeypatch.setenv(gitbin.ENV_NAME, str(tmp_path / "absent"))
    with pytest.raises(GitUnavailableError, match=gitbin.ENV_NAME):
        resolve_git()


def test_path_git_is_the_default_resolution(monkeypatch, no_pin):
    monkeypatch.setattr(gitbin.shutil, "which", lambda name: "/usr/bin/git")
    assert resolve_git() == "/usr/bin/git"


def test_windows_standard_locations_are_the_last_resort(tmp_path, monkeypatch, no_pin):
    standard = tmp_path / "Git" / "cmd" / "git.exe"
    standard.parent.mkdir(parents=True)
    standard.write_text("")
    monkeypatch.setattr(gitbin.shutil, "which", lambda name: None)
    monkeypatch.setattr(gitbin, "os", SimpleNamespace(name="nt", environ=os.environ))
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "absent"))
    assert resolve_git() == str(standard)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "absent"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path))
    assert resolve_git() == str(standard)


def test_missing_git_fails_with_the_remedy(monkeypatch, no_pin):
    monkeypatch.setattr(gitbin.shutil, "which", lambda name: None)
    monkeypatch.setattr(gitbin, "os", SimpleNamespace(name="nt", environ=os.environ))
    monkeypatch.setenv("ProgramFiles", "C:\\absent")
    monkeypatch.setenv("ProgramFiles(x86)", "C:\\absent-x86")
    with pytest.raises(GitUnavailableError, match="MINDIE_GIT_BIN.*install Git for Windows"):
        resolve_git()
    monkeypatch.setattr(gitbin, "os", SimpleNamespace(name="posix", environ=os.environ))
    with pytest.raises(GitUnavailableError, match="set MINDIE_GIT_BIN"):
        resolve_git()


def test_community_missing_git_keeps_the_transient_unavailable_class(monkeypatch, no_pin):
    from mindie_knowledge.community.common import git_executable, TransientError
    monkeypatch.setattr(gitbin.shutil, "which", lambda name: None)
    monkeypatch.setattr(gitbin, "os", SimpleNamespace(name="posix", environ=os.environ))
    with pytest.raises(TransientError) as caught:
        git_executable()
    assert caught.value.status == "unavailable"
    assert gitbin.ENV_NAME in str(caught.value)


def _feed_instance(tmp_path):
    from mindie_knowledge.loop import feed as feed_mod
    instance = object.__new__(feed_mod.Feed)
    instance.repo = tmp_path / "repo"
    instance._cancel = None
    return instance, feed_mod


class _Completed:
    timed_out = False
    code = 0
    out = b""
    err_text = ""


def test_feed_fetch_spawn_uses_the_pinned_git(tmp_path, monkeypatch):
    pinned = tmp_path / "pinned-git"
    pinned.write_text("")
    monkeypatch.setenv(gitbin.ENV_NAME, str(pinned))
    instance, feed_mod = _feed_instance(tmp_path)
    calls = []
    monkeypatch.setattr(feed_mod, "run_argv",
                        lambda argv, **kwargs: (calls.append(argv), _Completed())[1])
    instance._git("fetch", "--quiet", "origin", "main")
    assert calls[0][0] == str(pinned)
    assert calls[0][3:] == ["fetch", "--quiet", "origin", "main"]


def test_feed_fetch_spawn_defaults_to_path_git(tmp_path, monkeypatch):
    monkeypatch.delenv(gitbin.ENV_NAME, raising=False)
    instance, feed_mod = _feed_instance(tmp_path)
    calls = []
    monkeypatch.setattr(feed_mod, "run_argv",
                        lambda argv, **kwargs: (calls.append(argv), _Completed())[1])
    instance._git("fetch", "--quiet", "origin", "main")
    assert calls[0][0] == shutil.which("git")


def test_feed_missing_git_surfaces_unavailable_with_the_remedy(tmp_path, monkeypatch):
    monkeypatch.delenv(gitbin.ENV_NAME, raising=False)
    monkeypatch.setattr(gitbin.shutil, "which", lambda name: None)
    monkeypatch.setattr(gitbin, "os", SimpleNamespace(name="posix", environ=os.environ))
    instance, _feed_mod = _feed_instance(tmp_path)
    with pytest.raises(OSError) as caught:
        instance._git("fetch", "--quiet", "origin", "main")
    assert "bounded git fetch failed" in str(caught.value)
    assert gitbin.ENV_NAME in str(caught.value)


def test_ensure_git_on_path_prepends_pinned_git_dir_for_gh(tmp_path, monkeypatch):
    from mindie_knowledge.gitbin import ensure_git_on_path
    git_dir = tmp_path / "git-bash" / "bin"
    git_dir.mkdir(parents=True)
    pinned = git_dir / "git.exe"
    pinned.write_text("")
    monkeypatch.setenv(gitbin.ENV_NAME, str(pinned))
    monkeypatch.setenv("PATH", r"C:\Windows\System32")
    env = ensure_git_on_path({"GH_TOKEN": "tok"})
    assert env["GH_TOKEN"] == "tok"
    assert env["PATH"].split(os.pathsep)[0] == str(git_dir)
