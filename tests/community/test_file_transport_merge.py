"""Real dev merges preserve package bytes and expose each Git outcome."""

import pytest

from mindie_knowledge.community import submit_batch
from mindie_knowledge.community.common import CommunityError, Deadline, ProcessResult, UnknownOutcome
from mindie_knowledge.community import transport as transport_module

from .conftest import entry_file, git, make_batch, make_entry


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
