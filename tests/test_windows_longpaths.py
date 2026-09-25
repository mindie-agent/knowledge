"""Command-local Windows Git config. Not a Windows acceptance run."""

import os

from mindie_knowledge.gitread import with_windows_longpaths
from mindie_knowledge.community import gitops
from mindie_knowledge.loop.feed import _feed_git_env


def test_longpaths_is_command_local_and_windows_only(monkeypatch):
    base = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "credential.helper",
        "GIT_CONFIG_VALUE_1": "!gh auth git-credential",
        "GIT_CONFIG_KEY_2": "core.hooksPath",
        "GIT_CONFIG_VALUE_2": os.devnull,
    }
    monkeypatch.setattr(os, "name", "posix")
    plain = with_windows_longpaths(base)
    assert plain["GIT_CONFIG_COUNT"] == "3"
    assert plain["GIT_CONFIG_VALUE_2"] == os.devnull
    assert "core.longpaths" not in plain.values()
    assert base["GIT_CONFIG_COUNT"] == "3"

    monkeypatch.setattr(os, "name", "nt")
    windows = with_windows_longpaths(base)
    assert windows["GIT_CONFIG_COUNT"] == "4"
    assert windows["GIT_CONFIG_KEY_2"] == "core.hooksPath"
    assert windows["GIT_CONFIG_VALUE_1"] == "!gh auth git-credential"
    assert windows["GIT_CONFIG_KEY_3"] == "core.longpaths"
    assert windows["GIT_CONFIG_VALUE_3"] == "true"
    assert with_windows_longpaths(windows)["GIT_CONFIG_COUNT"] == "4"

    clone_env = _feed_git_env()
    assert clone_env["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert clone_env["GIT_CONFIG_VALUE_0"] == os.devnull
    assert clone_env["GIT_CONFIG_KEY_1"] == "core.longpaths"
    assert clone_env["GIT_CONFIG_VALUE_1"] == "true"

    gitops_env = gitops._resolve_git_env(None)
    assert gitops_env["GIT_CONFIG_KEY_2"] == "core.hooksPath"
    assert gitops_env["GIT_CONFIG_KEY_3"] == "core.longpaths"
    assert gitops_env["GIT_CONFIG_VALUE_3"] == "true"
