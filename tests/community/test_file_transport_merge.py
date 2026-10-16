"""Real dev merges preserve package bytes and expose each Git outcome."""

import subprocess

import pytest

from mindie_knowledge.community import submit_batch
from mindie_knowledge.community.common import CommunityError, Deadline, ProcessResult, UnknownOutcome
from mindie_knowledge.community import transport as transport_module
from mindie_knowledge.filesystem import filesystem_path
from mindie_knowledge.git_layout import MAX_GIT_DIRECTORY_BYTES, validate_git_directory

from .conftest import entry_file, git, make_batch, make_entry


def test_deep_file_transport_merges_then_reuses_mirror_and_worktree(
    tmp_path, settings, remote_url,
):
    """Native Windows namespace paths reach Python, ordinary paths reach Git."""
    # The merge worktree has the longest actual gitdir, including its .git.
    # Long package descendants are supported; an oversized Git root is not.
    repo = settings["repository"].replace("/", "_")
    fixed = tmp_path / "state-" / "merge-work" / repo / ".git"
    padding = MAX_GIT_DIRECTORY_BYTES - len(str(fixed).encode("utf-8"))
    assert padding >= 1, "test parent cannot hold the supported Git layout"
    state_dir = tmp_path / ("state-" + "x" * padding)
    for directory, bare in ((state_dir / "git" / repo, False),
                            (state_dir / "merge-work" / repo, False),
                            (state_dir / "mirrors" / repo, True)):
        actual = directory if bare else directory / ".git"
        assert len(str(actual).encode("utf-8")) <= MAX_GIT_DIRECTORY_BYTES
        validate_git_directory(directory, bare=bare, role="test-precondition")
    transport = transport_module.FileTransport(
        state_dir / "dev-github.json",
        {settings["repository"]: str(filesystem_path(remote_url))},
    )
    for number in (1, 2):
        batch = make_batch(f"deep-{number}", [
            entry_file(make_entry(entry_id=f"entry-{number}",
                                  content=f"Complete public observation {number}.")),
        ])
        assert max(len(str(state_dir / "git" / "acme_npu-knowledge" / item["path"]))
                   for item in batch["files"]) > 260
        submitted = submit_batch(batch, settings, state_dir, transport=transport)
        assert submitted["status"] == "submitted", submitted.get("detail")
        merged = transport.merge_pull_request(
            settings["repository"], number, sha=submitted["head_sha"],
            method="squash", deadline=Deadline(120, 100),
        )
        assert merged["merged"] is True
        assert git(["--git-dir", remote_url, "rev-parse", "refs/heads/main"]) == merged["sha"]
        for item in batch["files"]:
            actual = subprocess.run(
                ["git", "-c", "core.longpaths=true", "--git-dir", remote_url,
                 "show", "refs/heads/main:" + item["path"]],
                capture_output=True, check=True,
            )
            assert actual.stdout == item["content"].encode("utf-8")
        recorded = transport.get_pull_request(settings["repository"], number, Deadline(120, 100))
        assert recorded["state"] == "closed" and recorded["merged"] is True


@pytest.mark.parametrize("operation", ["fetch", "rev-parse", "push"])
def test_dev_merge_failure_never_records_false_success(
    settings, state_dir, transport, remote_url, monkeypatch, operation,
):
    def deadline():
        return Deadline(120, 100)

    first = submit_batch(make_batch("dev-first", [entry_file(make_entry())]),
                         settings, state_dir, transport=transport)
    transport.merge_pull_request(settings["repository"], 1, sha=first["head_sha"],
                                 method="squash", deadline=deadline())
    second = submit_batch(make_batch("dev-second", [entry_file(make_entry(entry_id="entry-2"))]),
                          settings, state_dir, transport=transport)
    assert second["status"] == "submitted"
    before = git(["ls-remote", remote_url, "refs/heads/main"])
    work = state_dir / "merge-work" / settings["repository"].replace("/", "_")
    real_run = transport_module.run_argv
    pushes = []

    def fail_step(argv, **kwargs):
        if str(work) in argv and "push" in argv:
            pushes.append(list(argv))
        if str(work) in argv and operation in argv:
            if operation == "push":
                # The write happened, but its response was lost.
                result = real_run(argv, **kwargs)
                assert result.code == 0
                return ProcessResult(-1, b"", b"synthetic lost push response", True)
            return ProcessResult(128, b"", b"synthetic dev Git failure", False)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(transport_module, "run_argv", fail_step)
    error = UnknownOutcome if operation == "push" else CommunityError
    with pytest.raises(error, match=f"dev merge {operation}"):
        transport.merge_pull_request(settings["repository"], 2, sha=second["head_sha"],
                                     method="squash", deadline=deadline())
    pr = transport.get_pull_request(settings["repository"], 2, deadline())
    assert pr["state"] == "open" and not pr["merged"]
    after = git(["ls-remote", remote_url, "refs/heads/main"])
    if operation == "push":
        assert len(pushes) == 1 and after != before
    else:
        assert pushes == [] and after == before
