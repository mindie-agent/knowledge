"""Idempotent fork branch/PR submit, offline and auth recovery."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "tests") not in sys.path:
    sys.path.insert(0, str(REPO / "tests"))

from contribution.support import (  # noqa: E402
    OWNER_REPO,
    FakeContributionGitHub,
    init_git_repo,
    ordinary_md,
)
from mindie_knowledge.contribution.pending import STATUS_AWAITING, STATUS_PR_OPEN  # noqa: E402
from mindie_knowledge.contribution.submit import (  # noqa: E402
    SubmitConfig,
    after_capture,
    prepare_candidate,
    submit_pending,
)

TEXT = ordinary_md(
    "一次图模式启动失败的排查经验",
    "当时遇到了图模式启动失败，固定依赖版本后恢复。只在当时环境验证过。",
)


class Submit(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.state = self.tmp / "state"
        self.public = self.tmp / "public"
        self.candidate = self.tmp / "note.md"
        self.candidate.write_text(TEXT, encoding="utf-8")
        self.repo = self.tmp / "fork"
        self.base = init_git_repo(self.repo)
        self.config = SubmitConfig(upstream=OWNER_REPO, fork=OWNER_REPO)

    def test_retry_reuses_the_same_pull_request(self):
        record = prepare_candidate(self.candidate, state_root=self.state, public_root=self.public)
        github = FakeContributionGitHub()
        first = submit_pending(
            record,
            state_root=self.state,
            public_root=self.public,
            git_repo=self.repo,
            github=github,
            config=self.config,
        )
        self.assertEqual(first.status, STATUS_PR_OPEN)
        self.assertEqual(first.pr_number, 1)
        second = submit_pending(
            first,
            state_root=self.state,
            public_root=self.public,
            git_repo=self.repo,
            github=github,
            config=self.config,
        )
        self.assertEqual(second.pr_number, 1)
        creates = [item for item in github.calls if item[0] == "POST" and str(item[1]).endswith("/pulls")]
        self.assertEqual(len(creates), 1)

    def test_offline_leaves_recoverable_pending_then_retry_opens_pr(self):
        record = prepare_candidate(self.candidate, state_root=self.state, public_root=self.public)
        github = FakeContributionGitHub()
        github.network_down = True
        offline = submit_pending(
            record,
            state_root=self.state,
            public_root=self.public,
            git_repo=self.repo,
            github=github,
            config=self.config,
        )
        self.assertEqual(offline.status, STATUS_AWAITING)
        self.assertIn("network", offline.last_error.lower())
        github.network_down = False
        recovered = submit_pending(
            offline,
            state_root=self.state,
            public_root=self.public,
            git_repo=self.repo,
            github=github,
            config=self.config,
        )
        self.assertEqual(recovered.status, STATUS_PR_OPEN)
        self.assertIsNotNone(recovered.pr_number)

    def test_auth_failure_is_explicit_and_does_not_block_capture(self):
        github = FakeContributionGitHub()
        github.auth_fail = True
        result = after_capture(
            self.candidate,
            state_root=self.state,
            public_root=self.public,
            git_repo=self.repo,
            github=github,
            config=self.config,
        )
        self.assertFalse(result["blocked_capture"])
        self.assertEqual(result["pending"]["status"], STATUS_AWAITING)
        self.assertIn("permission", result["pending"]["last_error"].lower())


if __name__ == "__main__":
    unittest.main()
