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

    # An explicit empty map used to fall through to GIT_ENV.
    monkeypatch.setattr(os, "name", "posix")
    empty = gitops._resolve_git_env({})
    assert empty["GIT_CONFIG_COUNT"] == "3"
    assert empty["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert empty["GIT_CONFIG_VALUE_1"] == "!gh auth git-credential"
    assert empty["GIT_CONFIG_KEY_2"] == "core.hooksPath"
    assert empty["GIT_CONFIG_VALUE_2"] == os.devnull
    assert gitops.GIT_ENV["GIT_CONFIG_COUNT"] == "3"

    monkeypatch.setattr(os, "name", "nt")
    empty_win = gitops._resolve_git_env({})
    assert empty_win["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert empty_win["GIT_CONFIG_KEY_2"] == "core.hooksPath"
    assert empty_win["GIT_CONFIG_VALUE_2"] == os.devnull
    assert empty_win["GIT_CONFIG_KEY_3"] == "core.longpaths"
    assert empty_win["GIT_CONFIG_VALUE_3"] == "true"
    assert gitops.GIT_ENV is gitops._GIT_BASE
    assert "core.longpaths" not in gitops.GIT_ENV.values()


def test_existing_longpaths_false_is_forced_true_without_a_new_slot(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    src = {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": "/dev/null",
        "GIT_CONFIG_KEY_1": "Core.LongPaths",
        "GIT_CONFIG_VALUE_1": "false",
    }
    original = dict(src)
    out = with_windows_longpaths(src)
    assert src == original
    assert out["GIT_CONFIG_COUNT"] == "2"
    assert out["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert out["GIT_CONFIG_VALUE_0"] == "/dev/null"
    assert out["GIT_CONFIG_KEY_1"] == "Core.LongPaths"
    assert out["GIT_CONFIG_VALUE_1"] == "true"
    assert with_windows_longpaths(out)["GIT_CONFIG_COUNT"] == "2"

    both = {
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "core.longpaths",
        "GIT_CONFIG_VALUE_0": "true",
        "GIT_CONFIG_KEY_1": "core.hooksPath",
        "GIT_CONFIG_VALUE_1": "/dev/null",
        "GIT_CONFIG_KEY_2": "CORE.LONGPATHS",
        "GIT_CONFIG_VALUE_2": "false",
    }
    forced = with_windows_longpaths(both)
    assert forced["GIT_CONFIG_COUNT"] == "3"
    assert forced["GIT_CONFIG_VALUE_0"] == "true"
    assert forced["GIT_CONFIG_VALUE_2"] == "true"
    assert forced["GIT_CONFIG_KEY_1"] == "core.hooksPath"
    assert forced["GIT_CONFIG_KEY_2"] == "CORE.LONGPATHS"
    assert list(forced) == list(with_windows_longpaths(forced))


def test_malformed_git_config_count_is_not_rewritten(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    src = {
        "GIT_CONFIG_COUNT": "bad",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": "/dev/null",
    }
    original = dict(src)
    try:
        with_windows_longpaths(src)
    except ValueError:
        pass
    else:
        raise AssertionError("malformed GIT_CONFIG_COUNT was accepted")
    assert src == original

    negative = dict(src, GIT_CONFIG_COUNT="-1")
    before = dict(negative)
    try:
        with_windows_longpaths(negative)
    except ValueError:
        pass
    else:
        raise AssertionError("negative GIT_CONFIG_COUNT was accepted")
    assert negative == before
