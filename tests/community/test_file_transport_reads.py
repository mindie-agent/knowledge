"""Dev remote read failures must not authorize a stale-head local merge."""

import pytest

from mindie_knowledge.community import submit_batch
from mindie_knowledge.community.common import (
    CommunityError, Deadline, ProcessResult, TransientError,
)
from mindie_knowledge.community import transport as transport_module

from .conftest import entry_file, git, make_batch, make_entry


@pytest.mark.parametrize("timed_out", [False, True])
def test_tip_read_failure_preserves_pr_and_never_pushes(
    settings, state_dir, transport, remote_url, monkeypatch, timed_out,
):
    submitted = submit_batch(
        make_batch("read-failure", [entry_file(make_entry())]),
        settings, state_dir, transport=transport,
    )
    assert submitted["status"] == "submitted", submitted.get("detail")
    state_before = transport.path.read_bytes()
    remote_before = git(["ls-remote", remote_url])
    original = transport_module.run_argv
    pushes = []

    def fail_read(argv, **kwargs):
        if "push" in argv:
            pushes.append(list(argv))
        if "ls-remote" in argv:
            return ProcessResult(
                -1 if timed_out else 128, b"", b"synthetic dev read failure", timed_out,
            )
        return original(argv, **kwargs)

    monkeypatch.setattr(transport_module, "run_argv", fail_read)
    error = TransientError if timed_out else CommunityError
    with pytest.raises(error, match="dev remote tip"):
        transport.merge_pull_request(
            settings["repository"], 1, sha=submitted["head_sha"],
            method="squash", deadline=Deadline(120, 100),
        )
    assert transport.path.read_bytes() == state_before
    assert git(["ls-remote", remote_url]) == remote_before
    assert pushes == []


def test_successful_empty_tip_read_is_still_absent(settings, transport):
    assert transport._tip(
        settings["repository"], "branch-that-does-not-exist", Deadline(120, 100),
    ) is None
