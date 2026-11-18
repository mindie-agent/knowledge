"""Anonymous public HTTPS feed reads: no ``gh``, no credential helper, no prompt."""

from __future__ import annotations

import json
import os
from contextlib import closing
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mindie_knowledge.loop.feed import Feed, anonymous_public_read
from mindie_knowledge.loop.store import Store

CANONICAL = "https://github.com/mindie-agent/knowledge-vllm-ascend.git"
OPTIONS = ("credential.helper=", "core.askPass=", "http.extraHeader=")


def _git(argv, cwd=None, env=None):
    result = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, env=env, check=False,
    )
    assert result.returncode == 0, (argv, result.stderr)
    return result


def _remote_repo(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    _git(["git", "init", "--quiet", "--initial-branch=main", str(work)], env=env)
    (work / "README.md").write_text("# Public feed fixture\n", encoding="utf-8")
    _git(["git", "-C", str(work), "add", "README.md"], env=env)
    _git(["git", "-C", str(work), "commit", "-qm", "initial"], env=env)
    remote = tmp_path / "remote.git"
    _git(["git", "clone", "--quiet", "--bare", str(work), str(remote)], env=env)
    return remote


def _wrapper(tmp_path, remote, log):
    real_git = shutil.which("git")
    assert real_git
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    py_script = bin_dir / "git_shim.py"
    source = f"""import json, os, subprocess, sys
args = sys.argv[1:]
with open({json.dumps(str(log))}, "a", encoding="utf-8") as stream:
    stream.write(json.dumps({{
        "argv": args,
        "terminal_prompt": os.environ.get("GIT_TERMINAL_PROMPT"),
        "git_askpass": os.environ.get("GIT_ASKPASS"),
        "ssh_askpass": os.environ.get("SSH_ASKPASS"),
        "https_proxy": os.environ.get("HTTPS_PROXY"),
        "ssl_cert_file": os.environ.get("SSL_CERT_FILE"),
        "git_ssl_ca_info": os.environ.get("GIT_SSL_CAINFO"),
    }}) + "\\n")
args = [{json.dumps(Path(remote).as_uri())} if a == {json.dumps(CANONICAL)} else a for a in args]
flags = {{"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}} if os.name == "nt" else {{}}
raise SystemExit(subprocess.run([{json.dumps(real_git)}, *args], check=False, **flags).returncode)
"""
    py_script.write_text(source, encoding="utf-8", newline="\n")
    if os.name == "nt":
        cmd = bin_dir / "git.cmd"
        cmd.write_text(f'@echo off\n"{sys.executable}" "{py_script}" %*\n', encoding="utf-8", newline="\n")
        return cmd
    script = bin_dir / "git"
    script.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8", newline="\n")
    script.chmod(0o755)
    return script


def test_anonymous_public_read_recognizes_only_canonical_corpora():
    assert anonymous_public_read(CANONICAL) is True
    assert anonymous_public_read("https://github.com/mindie-agent/knowledge.git") is True
    assert anonymous_public_read("https://github.com/mindie-agent/knowledge-mindie") is True
    assert anonymous_public_read("https://github.com/other-org/knowledge-vllm-ascend.git") is False
    assert anonymous_public_read("https://gitcode.com/mindie-agent/knowledge.git") is False
    assert anonymous_public_read("file:///tmp/knowledge.git") is False


def test_public_clone_and_fetch_succeed_anonymously_with_broken_credentials(tmp_path, monkeypatch):
    remote = _remote_repo(tmp_path)
    log = tmp_path / "git.log"
    wrapper = _wrapper(tmp_path, remote, log)

    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        '[credential]\n\thelper = "!exit 99"\n', encoding="utf-8", newline="\n",
    )
    gh_marker = tmp_path / "gh-was-called"
    if os.name == "nt":
        gh = wrapper.parent / "gh.cmd"
        gh.write_text(f'@echo off\ntype nul > "{gh_marker}"\nexit /b 99\n', encoding="utf-8", newline="\n")
    else:
        gh = wrapper.parent / "gh"
        quoted = "'" + str(gh_marker).replace("'", "'\"'\"'") + "'"
        gh.write_text(
            "#!/bin/sh\n: > " + quoted + "\nexit 99\n",
            encoding="utf-8", newline="\n",
        )
        gh.chmod(0o755)
    (tmp_path / "ca.pem").write_text("synthetic CA\n", encoding="utf-8")

    monkeypatch.setenv("MINDIE_GIT_BIN", str(wrapper))
    monkeypatch.setenv("PATH", str(wrapper.parent) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HTTPS_PROXY", "http://enterprise-proxy.invalid:8080")
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "ca.pem"))
    monkeypatch.setenv("GIT_SSL_CAINFO", str(tmp_path / "ca.pem"))

    with closing(Store(tmp_path / "state", "vllm-ascend")) as store:
        feed = Feed(store, {
            "domain": store.domain,
            "repository": "mindie-agent/knowledge-vllm-ascend",
            "url": CANONICAL,
            "ref": "main",
        })
        deadline = time.monotonic() + 120
        feed._clone(deadline=deadline)
        assert (Path(feed.repo) / "HEAD").is_file()
        feed._git("fetch", "--quiet", "--depth=1", "--no-tags", "origin", "main",
                  deadline=time.monotonic() + 120)

    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    clone = next(record for record in records if "clone" in record["argv"])
    fetch = next(record for record in records if "fetch" in record["argv"])
    for record in (clone, fetch):
        argv = record["argv"]
        for option in OPTIONS:
            assert option in argv
            assert argv[argv.index(option) - 1] == "-c"
        assert record["terminal_prompt"] == "0"
        assert record["git_askpass"] == "", (
            "GIT_ASKPASS must be pinned empty; Git checks it before core.askPass"
        )
        assert record["ssh_askpass"] == "", (
            "SSH_ASKPASS must be pinned empty for anonymous public reads"
        )
        assert record["https_proxy"] == "http://enterprise-proxy.invalid:8080"
        assert record["ssl_cert_file"].endswith("ca.pem")
        assert record["git_ssl_ca_info"].endswith("ca.pem")
    assert not gh_marker.exists(), "public feed read must never invoke gh"
