"""HTTP classification, Retry-After, and show_file bounds.

Git and the gh executable are local doubles. Nothing here contacts GitHub.
"""

from email.utils import formatdate
import os
from pathlib import Path
import sys
import time

import pytest

import json
from urllib.parse import parse_qs, urlsplit

from mindie_knowledge.community import gitops, transport
from mindie_knowledge.community.batch import contribution_branch
from mindie_knowledge.community.common import CommunityError, Deadline, finite_epoch

from .conftest import entry_file, git, make_batch, make_entry

# Extensionless shebang children are a POSIX process mechanism. They are not
# Windows process coverage.
POSIX_CHILD = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX process-tree mechanism; Windows needs real acceptance",
)


def test_show_file_preserves_legal_body_above_256kib(tmp_path):
    git(["init", "-q"], cwd=tmp_path)
    body = "Local fixture body.\n" * 20000
    assert len(body.encode()) > 256 * 1024
    (tmp_path / "large.md").write_text(body)
    git(["add", "large.md"], cwd=tmp_path)
    git(["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-q", "-m", "Local legal blob"], cwd=tmp_path)
    head = git(["rev-parse", "HEAD"], cwd=tmp_path)
    assert gitops.show_file(tmp_path, head, "large.md", Deadline(30, 10)) == body
    assert gitops.show_file(tmp_path, head, "missing.md", Deadline(30, 10)) is None
    assert gitops.show_file(tmp_path, "b" * 40, "large.md", Deadline(30, 10)) is None


@POSIX_CHILD
def test_show_file_timeout_is_unavailable_not_missing(tmp_path, monkeypatch):
    shim = tmp_path / "git"
    shim.write_text("#!" + sys.executable + "\nimport time\ntime.sleep(3)\n")
    shim.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    started = time.monotonic()
    with pytest.raises(CommunityError) as caught:
        gitops.show_file(tmp_path, "a" * 40, "blob.md", Deadline(1, 10))
    assert caught.value.status == "unavailable"
    assert time.monotonic() - started < 2.5


def test_finite_epoch_rejects_bool_and_non_finite():
    assert finite_epoch(True) is None
    assert finite_epoch(float("nan")) is None
    assert finite_epoch(float("inf")) is None
    assert finite_epoch(90) == 90.0


@POSIX_CHILD
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("form", ["seconds", "http-date", "invalid"])
@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
def test_retry_after_header_is_optional_epoch(tmp_path, monkeypatch, method, form, line_ending):
    now = time.time()
    value = {
        "seconds": "90",
        "http-date": formatdate(now + 90, usegmt=True),
        "invalid": "NaN",
    }[form]
    headers = line_ending.join([
        "HTTP/2.0 429 Too Many Requests",
        "Retry-After: " + value,
        "Content-Type: application/json",
        "",
        "",
    ])
    shim = tmp_path / "gh"
    shim.write_text(
        "#!" + sys.executable + "\n"
        "import sys\n"
        "if '--include' in sys.argv or '-i' in sys.argv:\n"
        "    sys.stdout.write(" + repr(headers) + ")\n"
        "print('{\"message\":\"API rate limit exceeded\"}')\n"
        "print('gh: API rate limit exceeded (HTTP 429)', file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    shim.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    api = transport.GhTransport({"repository": "review-owner/knowledge"})
    with pytest.raises(CommunityError) as caught:
        api._api(method, "/repos/review-owner/knowledge/pulls", Deadline(5, 10))
    retry_at = caught.value.retry_at
    if form == "invalid":
        assert retry_at is None
    else:
        assert isinstance(retry_at, float)
        assert now + 88 <= retry_at <= time.time() + 91
    assert caught.value.status == ("unavailable" if method == "GET" else "unknown")


@POSIX_CHILD
@pytest.mark.parametrize("method,case,expected", [
    ("GET", "auth", "failed"),
    ("POST", "auth", "failed"),
    ("GET", "rate", "unavailable"),
    ("GET", "server", "unavailable"),
    ("POST", "server", "unknown"),
    ("GET", "network", "unavailable"),
    ("POST", "network", "unknown"),
    ("GET", "timeout", "unavailable"),
    ("POST", "timeout", "unknown"),
])
def test_http_method_aware_classification(tmp_path, monkeypatch, method, case, expected):
    shim = tmp_path / "gh"
    shim.write_text(
        "#!" + sys.executable + "\n"
        "import os, sys, time\n"
        "case = os.environ['GH_FIXTURE_CASE']\n"
        "if case == 'timeout':\n"
        "    time.sleep(3)\n"
        "    sys.exit(1)\n"
        "messages = {'auth': 'Resource not accessible by integration',"
        " 'rate': 'API rate limit exceeded', 'server': 'Service Unavailable'}\n"
        "codes = {'auth': 403, 'rate': 403, 'server': 502}\n"
        "if case in codes:\n"
        "    print('{\"message\":\"%s\"}' % messages[case])\n"
        "    print('gh: ' + messages[case] + ' (HTTP ' + str(codes[case]) + ')', file=sys.stderr)\n"
        "else:\n"
        "    print('Post \"https://api.github.com/graphql\": dial tcp: lookup api.github.com: no such host', file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    shim.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("GH_FIXTURE_CASE", case)
    api = transport.GhTransport({
        "repository": "review-owner/knowledge",
        "fork": "review-contributor/knowledge",
    })
    api.op_seconds = 0.15 if case == "timeout" else 3
    with pytest.raises(CommunityError) as caught:
        api._api(method, "/repos/review-owner/knowledge/pulls", Deadline(5, 5))
    assert caught.value.status == expected


@POSIX_CHILD
def test_fork_lookup_uses_contributor_owner(tmp_path, monkeypatch):
    recorded = tmp_path / "argv.json"
    shim = tmp_path / "gh"
    shim.write_text(
        "#!" + sys.executable + "\n"
        "import json, sys\n"
        "open(" + repr(str(recorded)) + ", 'w').write(json.dumps(sys.argv[1:]))\n"
        "print('[]')\n"
    )
    shim.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    api = transport.GhTransport({
        "repository": "review-owner/knowledge",
        "fork": "review-contributor/knowledge",
    })
    branch = contribution_branch("review", "fork-review")
    api.find_pull_requests("review-owner/knowledge", head_branch=branch, deadline=Deadline(5, 5))
    argv = json.loads(recorded.read_text())
    head = parse_qs(urlsplit(argv[-1]).query)["head"][0]
    assert head == f"review-contributor:{branch}"


def test_fetch_pr_head_preserves_transient_retry(monkeypatch):
    retry_at = time.time() + 90

    def fail(*args, **kwargs):
        raise transport.TransientError("Controlled fetch unavailable", retry_at=retry_at)

    monkeypatch.setattr(gitops, "fetch_ref", fail)
    with pytest.raises(CommunityError) as caught:
        gitops.fetch_pr_head(Path("."), 7, "ignored-branch", Deadline(5, 5))
    assert isinstance(caught.value, transport.TransientError)
    assert caught.value.status == "unavailable"
    assert caught.value.retry_at == retry_at


def test_find_pull_requests_rejects_unreadable_success(monkeypatch):
    api = transport.GhTransport({
        "repository": "acme/npu-knowledge",
        "fork": "contributor/npu-knowledge",
    })

    for payload in ({"message": "not a PR list"}, None, [{}], [{"number": 1}]):
        def unreadable(method, path, deadline, body=None, payload=payload):
            return payload

        monkeypatch.setattr(api, "_api", unreadable)
        with pytest.raises(CommunityError) as caught:
            api.find_pull_requests(
                "acme/npu-knowledge", head_branch="mindie-contrib/npu/batch", deadline=Deadline(5, 5)
            )
        assert caught.value.status == "unavailable"


def test_find_pull_requests_accepts_an_empty_list(monkeypatch):
    api = transport.GhTransport({"repository": "acme/npu-knowledge"})

    def empty(method, path, deadline, body=None):
        return []

    monkeypatch.setattr(api, "_api", empty)
    assert api.find_pull_requests(
        "acme/npu-knowledge", head_branch="mindie-contrib/npu/batch", deadline=Deadline(5, 5)
    ) == []
    usable = {"number": 1, "state": "open", "head": {"ref": "mindie-contrib/npu/batch", "sha": "a" * 40}}

    def one(method, path, deadline, body=None):
        return [usable]

    monkeypatch.setattr(api, "_api", one)
    found = api.find_pull_requests(
        "acme/npu-knowledge", head_branch="mindie-contrib/npu/batch", deadline=Deadline(5, 5)
    )
    assert found[0]["number"] == 1
    assert found[0]["head"]["ref"] == "mindie-contrib/npu/batch"
    assert found[0]["merged"] is False


@pytest.mark.parametrize("fault", ["missing_ref", "mismatched_sha", "missing_api_sha"])
def test_open_update_does_not_write_without_proven_head(settings, state_dir, transport, remote_url, fault):
    from mindie_knowledge.community import publish
    from mindie_knowledge.community.transport import FileTransport

    doc = make_entry()
    first = publish.submit_batch(
        make_batch("batch-proof", [entry_file(doc)]), settings, state_dir, transport=transport
    )
    assert first["status"] == "submitted", first
    branch = "mindie-contrib/npu/batch-proof"
    before = git(["ls-remote", remote_url, f"refs/heads/{branch}"]).split()[0]
    api = transport
    if fault == "missing_ref":
        git(["--git-dir", remote_url, "update-ref", "-d", "refs/pull/1/head"])
    elif fault == "mismatched_sha":
        main = git(["ls-remote", remote_url, "refs/heads/main"]).split()[0]
        git(["--git-dir", remote_url, "update-ref", "refs/pull/1/head", main])
    else:
        class HiddenSha(FileTransport):
            def get_pull_request(self, repo, number, deadline):
                pr = super().get_pull_request(repo, number, deadline)
                head = dict(pr.get("head") or {})
                head.pop("sha", None)
                pr = dict(pr)
                pr["head"] = head
                return pr

        api = HiddenSha(transport.path, transport.remotes)
    later = make_entry(content="A later observation that must not land.")
    batch = make_batch(
        "batch-proof",
        [dict(entry_file(later), base_sha256=entry_file(doc)["sha256"])],
    )
    result = publish.submit_batch(batch, settings, state_dir, transport=api)
    after = git(["ls-remote", remote_url, f"refs/heads/{branch}"]).split()[0]
    assert result["status"] not in ("submitted", "updated", "unchanged"), result
    assert after == before
    if fault == "missing_ref":
        return
    assert result["status"] == "unavailable"
    if fault == "mismatched_sha":
        git(["--git-dir", remote_url, "update-ref", "refs/pull/1/head", before])
    recovered = publish.submit_batch(batch, settings, state_dir, transport=transport)
    moved = git(["ls-remote", remote_url, f"refs/heads/{branch}"]).split()[0]
    assert recovered["status"] == "updated", recovered
    assert moved != before


def test_restore_follows_api_pr_state(tmp_path):
    """API state chooses the body. A leftover branch is not a source."""
    from mindie_knowledge.community.batch import batch_revision, contribution_branch
    from mindie_knowledge.community.transport import FileTransport
    from mindie_knowledge.loop import settings as loop_settings
    from mindie_knowledge.loop.engine import Engine, RestoreUnavailable
    from mindie_knowledge.loop.store import Store

    repo = "acme/npu-knowledge"
    bare = tmp_path / "remote.git"
    work = tmp_path / "seed"
    git(["init", "--bare", "-b", "main", str(bare)])
    git(["clone", str(bare), str(work)])
    doc = make_entry(content="Body that must not be resurrected.")
    rendered = entry_file(doc)
    (work / "cases").mkdir()
    (work / rendered["path"]).write_text(rendered["content"], encoding="utf-8")
    git(["add", "-A"], cwd=work)
    git(["-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-m", "add"], cwd=work)
    kept = git(["rev-parse", "HEAD"], cwd=work)
    branch = contribution_branch("npu", "batch-restore")
    git(["push", "origin", "HEAD:refs/heads/main"], cwd=work)
    git(["push", "origin", f"HEAD:refs/heads/{branch}"], cwd=work)

    def attempt(mode, number, api_sha):
        config = loop_settings.write(
            tmp_path / f"{mode}.json", enabled=True, repository=repo,
            project_roots=[tmp_path], transport="file",
            dev_remotes={repo: str(bare)},
        )
        store = Store(tmp_path / f"store-{mode}", "npu")
        try:
            local = store.create_draft(
                kind="experience", title=doc["title"], summary=doc["summary"],
                content=doc["content"], entry_id=doc["entry_id"],
                owner="c" * 64, generation=config.generation,
            )
            payload = make_batch("batch-restore", [rendered])
            payload["entry_refs"] = [store.ref(doc["entry_id"], local["revision"])]
            payload["revision"] = batch_revision(
                payload["files"], "npu", None, payload["entry_refs"]
            )
            store.create_batch(
                batch_id="batch-restore", revision=payload["revision"], batch=payload,
                entry_ids=[doc["entry_id"]], vote_keys=[], generation=config.generation,
            )
            store.mark_batch(
                "batch-restore", "submitted",
                pr_url=f"https://example.invalid/{repo}/pull/{number}", head_sha=kept,
            )
            store.compact_confirmed("batch-restore")
            merged = mode == "merged"
            FileTransport(store.root / "outbox" / "dev-github.json", {repo: str(bare)}).seed(
                repo,
                pulls={
                    str(number): {
                        "number": number,
                        "state": "open" if mode == "open" else "closed",
                        "merged": merged,
                        "html_url": f"https://example.invalid/{repo}/pull/{number}",
                        "head": {
                            "ref": branch,
                            "sha": api_sha,
                            "repo": {"full_name": repo},
                        },
                        "base": {"ref": "main"},
                    }
                },
            )
            engine = Engine(store, settings_path=config.path)
            try:
                restored = engine._restore_sent_draft(doc["entry_id"], config.generation)
            except RestoreUnavailable:
                restored = False
            assert restored is False, mode
            assert store._row(doc["entry_id"])["draft_revision"] is None
        finally:
            store.close()

    # Closed unmerged while main still has the body. Reading main would restore.
    attempt("closed", 4, kept)
    # Open mismatch: API sha is not refs/pull/5/head. Main still has the body,
    # and the PR ref does too. Either read would resurrect it. The branch is
    # removed first so the dev double cannot replace the API sha with the tip.
    other = git(["-c", "user.name=t", "-c", "user.email=t@example.invalid",
                 "commit-tree", kept + "^{tree}", "-m", "different"], cwd=work)
    git(["push", "origin", ":refs/heads/" + branch], cwd=work)
    git(["--git-dir", str(bare), "update-ref", "refs/pull/5/head", kept])
    attempt("open", 5, other)
    # Merged withdrawal: main no longer has the file; the branch does.
    git(["push", "origin", f"{kept}:refs/heads/{branch}"], cwd=work)
    (work / rendered["path"]).unlink()
    git(["add", "-A"], cwd=work)
    git(["-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-m", "withdraw"], cwd=work)
    git(["push", "origin", "HEAD:refs/heads/main"], cwd=work)
    attempt("merged", 6, kept)


# Recorded public list shape: state closed, merged_at set, no merged field.
# Detail for the same PR carries merged=true. Do not fetch GitHub again.
_RECORDED_LIST_PR = {
    "number": 17,
    "state": "closed",
    "merged_at": "2026-09-24T04:38:23Z",
    "head": {
        "sha": "2bf72f20f6648ee6008d6b4229b82dc0db8060be",
        "ref": "mindie-contrib/vllm-ascend/batch-f34d0d7b772e072975c9ef29",
        "repo": {"full_name": "mindie-agent/knowledge-vllm-ascend"},
    },
    "html_url": "https://github.com/mindie-agent/knowledge-vllm-ascend/pull/17",
}


def test_recorded_list_shape_sets_merged_from_merged_at(tmp_path, monkeypatch):
    from mindie_knowledge.community import publish
    from mindie_knowledge.community.ledger import Ledger

    item = dict(_RECORDED_LIST_PR)
    assert "merged" not in item
    repo = item["head"]["repo"]["full_name"]
    ref = item["head"]["ref"]
    sha = item["head"]["sha"]
    api = transport.GhTransport({"repository": repo, "enabled": True})
    monkeypatch.setattr(api, "_api", lambda *args, **kwargs: [item])
    found = api.find_pull_requests(repo, head_branch=ref, deadline=Deadline(5, 5))
    assert found[0]["merged"] is True
    assert "merged" not in item

    state = tmp_path / "ledger"
    ledger = Ledger(state)
    try:
        ledger.record_intent(
            batch_id="batch-shape", revision="b" * 64, domain="vllm-ascend",
            repository=repo, branch=ref,
        )
        ledger.record_step("batch-shape", "b" * 64, "git:pushed", sha)
        ledger.finish_publication(
            "batch-shape", "b" * 64, status="unknown",
            detail="response lost", head_sha=sha,
        )
    finally:
        ledger.close()
    receipt = publish.reconcile_batch(
        "batch-shape", {"enabled": True, "repository": repo, "branch": "main"},
        state, transport=api,
    )
    assert receipt["status"] == "submitted", receipt

    closed = dict(item, number=18, merged_at=None)
    closed.pop("merged", None)
    monkeypatch.setattr(api, "_api", lambda *args, **kwargs: [closed])
    unmerged = api.find_pull_requests(repo, head_branch=ref, deadline=Deadline(5, 5))
    assert unmerged[0]["merged"] is False


def test_unreadable_detail_and_unknown_state_are_unavailable(monkeypatch):
    repo = "mindie-agent/knowledge-vllm-ascend"
    api = transport.GhTransport({"repository": repo})
    monkeypatch.setattr(api, "_api", lambda *args, **kwargs: {})
    with pytest.raises(CommunityError) as caught:
        api.get_pull_request(repo, 17, Deadline(5, 5))
    assert caught.value.status == "unavailable"

    bad = dict(_RECORDED_LIST_PR, state="not-a-pr-state", merged=True)
    monkeypatch.setattr(api, "_api", lambda *args, **kwargs: bad)
    with pytest.raises(CommunityError) as caught:
        api.get_pull_request(repo, 17, Deadline(5, 5))
    assert caught.value.status == "unavailable"
    monkeypatch.setattr(api, "_api", lambda *args, **kwargs: [bad])
    with pytest.raises(CommunityError) as caught:
        api.find_pull_requests(repo, head_branch=bad["head"]["ref"], deadline=Deadline(5, 5))
    assert caught.value.status == "unavailable"


def test_stage_and_commit_many_paths_exceeding_argv_limit(tmp_path):
    """Real git add of more path bytes than the OS argument limit."""
    work = tmp_path / "repo"
    git(["init", "-q", "-b", "main", str(work)])
    (work / "README").write_text("seed\n", encoding="utf-8")
    git(["add", "README"], cwd=work)
    git(["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-q", "-m", "seed"], cwd=work)
    seeded = git(["rev-parse", "HEAD"], cwd=work)
    assert gitops.stage_and_commit(work, [], "nothing", Deadline(30, 10)) == seeded

    stem = "n" * 240
    paths = [f"wide/{i:04d}-{stem}" for i in range(4300)]
    assert sum(len(path.encode()) + 1 for path in paths) > 1_048_576
    paths.append("wide/name with space.txt")
    for path in paths:
        target = work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
    committed = gitops.stage_and_commit(work, paths, "many paths", Deadline(60, 10))
    assert committed and committed != seeded
    listed = set(git(["ls-files"], cwd=work).splitlines())
    assert set(paths) <= listed
    assert "wide/name with space.txt" in listed
