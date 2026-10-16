"""Dependency limits reject Git roots before effects, not material paths."""
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from mindie_knowledge import git_layout as layout
from mindie_knowledge.filesystem import filesystem_path
from mindie_knowledge.community import gitops
from mindie_knowledge.community.common import CommunityError
from mindie_knowledge.loop.feed import Feed
from mindie_knowledge.loop.store import Store


def windows_path(size):
    return "C:\\" + "x" * (size - 3)


def test_actual_git_directory_byte_boundary_and_private_error(monkeypatch):
    monkeypatch.setattr(layout, "WINDOWS", True)
    layout.validate_git_directory(windows_path(220), bare=True, role="feed")
    with pytest.raises(layout.UnsupportedGitLayout) as raised:
        layout.validate_git_directory(windows_path(221), bare=True, role="feed")
    error = raised.value
    assert error.path_bytes == 221 and error.limit_bytes == 220
    assert error.role == "feed"
    assert error.mindie_execution == "not_started"
    assert error.external_write_attempted is False
    assert error.code in str(error)
    assert "execution=not_started" in str(error)
    assert windows_path(221) not in str(error)


def test_extended_spelling_has_same_boundary_and_utf8_is_not_character_count(monkeypatch):
    monkeypatch.setattr(layout, "WINDOWS", True)
    for prefix in ("", "\\\\?\\"):
        layout.validate_git_directory(prefix + windows_path(220), bare=True, role="feed")
        with pytest.raises(layout.UnsupportedGitLayout) as raised:
            layout.validate_git_directory(prefix + "C:\\" + "\u4e2d" * 73, bare=True, role="feed")
        assert raised.value.path_bytes == 222
    unc = "\\\\server\\share\\" + "x" * 210
    sizes = []
    for path in (unc, "\\\\?\\UNC\\" + unc[2:]):
        with pytest.raises(layout.UnsupportedGitLayout) as raised:
            layout.validate_git_directory(path, bare=True, role="feed")
        sizes.append(raised.value.path_bytes)
    assert sizes[0] == sizes[1]


def test_publication_counts_dotgit_and_nonwindows_does_not_restrict(monkeypatch):
    monkeypatch.setattr(layout, "WINDOWS", True)
    layout.validate_git_directory(windows_path(215), bare=False, role="publication")
    with pytest.raises(layout.UnsupportedGitLayout) as raised:
        layout.validate_git_directory(windows_path(216), bare=False, role="publication")
    assert raised.value.path_bytes == 221
    monkeypatch.setattr(layout, "WINDOWS", False)
    layout.validate_git_directory(windows_path(400), bare=False, role="publication")


def test_feed_refuses_before_creating_cache_and_preserves_existing_state(tmp_path, monkeypatch):
    root = filesystem_path(tmp_path / ("a" * 90) / ("b" * 90))
    root.mkdir(parents=True)
    receipt = root / "existing-receipt.json"
    receipt.write_text("retained")
    before = sorted(path.name for path in root.iterdir())
    monkeypatch.setattr(layout, "WINDOWS", True)
    with pytest.raises(layout.UnsupportedGitLayout, match="feed"):
        Feed(SimpleNamespace(root=root, domain="test"), dict(domain="test", repository="org/feed"))
    assert sorted(path.name for path in root.iterdir()) == before
    assert receipt.read_text() == "retained"


@pytest.mark.parametrize("existing", [False, True])
def test_publication_refuses_before_clone_or_fetch_and_preserves_state(tmp_path, monkeypatch, existing):
    parent = filesystem_path(tmp_path / ("a" * 90) / ("b" * 90))
    work = parent / "publication"
    if existing:
        (work / ".git").mkdir(parents=True)
        (work / ".git" / "config").write_text("retained")
    monkeypatch.setattr(layout, "WINDOWS", True)
    def unexpected(*args, **kwargs):
        pytest.fail("unsupported layout must not invoke Git")
    monkeypatch.setattr(gitops, "_git", unexpected)
    with pytest.raises(CommunityError, match="publication") as raised:
        gitops.ensure_clone("https://example.invalid/unused", work, None)
    assert raised.value.status == "unavailable"
    assert isinstance(raised.value.__cause__, layout.UnsupportedGitLayout)
    assert not hasattr(raised.value, "external_write_attempted")
    if existing:
        assert (work / ".git" / "config").read_text() == "retained"
    else:
        assert not parent.exists()


@pytest.mark.parametrize("operation", ["reconcile_batch", "inspect_batch"])
def test_reconciliation_layout_failure_keeps_prior_publication_unknown(
        tmp_path, monkeypatch, operation):
    from mindie_knowledge.community import publish
    from mindie_knowledge.community.ledger import Ledger

    state = filesystem_path(tmp_path / ("a" * 90) / ("b" * 90))
    batch, revision, head = "original", "a" * 64, "b" * 40
    branch, url = "mindie-contrib/test/original", "https://example.invalid/pull/1"
    with closing(Ledger(state)) as ledger:
        ledger.record_intent(batch_id=batch, revision=revision, domain="test",
                             repository="org/feed", branch=branch)
        ledger.record_step(batch, revision, "git:intended-head", head)
        ledger.record_step(batch, revision, "git:pushed", head)
        ledger.finish_publication(batch, revision, status="unknown",
                                  detail="PR response lost", head_sha=head,
                                  pr_number=1, pr_url=url)
        original = ledger.get_publication(batch, revision)
        original_steps = ledger.steps_for(batch, revision)
    pr = dict(number=1, state="open", html_url=url,
              head=dict(ref=branch, sha="c" * 40))
    reads = []
    def lookup(*args):
        reads.append(args)
        return pr
    def unexpected(*args, **kwargs):
        pytest.fail("unavailable ancestry must not invoke Git or remote writes")
    transport = SimpleNamespace(
        get_pull_request=lookup, find_pull_requests=unexpected,
        create_pull_request=unexpected, update_pull_request=unexpected)
    monkeypatch.setattr(layout, "WINDOWS", True)
    monkeypatch.setattr(publish, "sharing_enabled", lambda settings: True)
    monkeypatch.setattr(gitops, "_git", unexpected)
    result = getattr(publish, operation)(
        batch, dict(repository="org/feed"), state, transport=transport)
    assert len(reads) == 1
    assert result["status"] == "unknown" and result["head_sha"] == head
    assert result["pr_url"] == url
    assert "unsupported_windows_git_root" in result["detail"]
    assert "git_operation_execution=not_started" in result["detail"]
    assert str(state) not in result["detail"]
    assert "external_write_attempted" not in result
    with closing(Ledger(state)) as ledger:
        after = ledger.get_publication(batch, revision)
        for field in ("batch_id", "revision", "domain", "repository", "branch",
                      "pr_number", "pr_url", "head_sha", "status", "created"):
            assert after[field] == original[field]
        assert ledger.steps_for(batch, revision)[:len(original_steps)] == original_steps
    assert not (state / "git").exists()


def test_config_preflight_rejects_before_layout_creation(tmp_path, monkeypatch):
    root = filesystem_path(tmp_path / ("a" * 90) / ("b" * 90))
    monkeypatch.setattr(layout, "WINDOWS", True)
    with pytest.raises(layout.UnsupportedGitLayout, match="feed"):
        layout.validate_config_feed_layout(dict(
            root=str(root), domain="test", feeds=[dict(
                domain="test", repository="org/feed", ref="main", prefix="/catalog/")]))
    assert not root.exists()


def test_config_preflight_preserves_existing_layout_failure(tmp_path, monkeypatch):
    from mindie_knowledge import state_layout
    cause = ValueError("existing state requires migration")
    def incompatible(*args):
        raise cause
    monkeypatch.setattr(layout, "WINDOWS", True)
    monkeypatch.setattr(state_layout, "state_root", incompatible)
    with pytest.raises(ValueError) as raised:
        layout.validate_config_feed_layout(dict(
            root=str(tmp_path), domain="test", feeds=[dict(domain="test", repository="org/feed")]))
    assert raised.value is cause


def test_no_feed_keeps_real_deep_local_store_supported(tmp_path, monkeypatch):
    root = filesystem_path(tmp_path / ("a" * 90) / ("b" * 90))
    monkeypatch.setattr(layout, "WINDOWS", True)
    layout.validate_config_feed_layout(dict(root=str(root), domain="test"))
    with closing(Store(root, "test")) as store:
        store.feed_set("retained-local-data", {"value": "unchanged"})
    with closing(Store(root, "test")) as store:
        assert store.feed_get("retained-local-data") == {"value": "unchanged"}
