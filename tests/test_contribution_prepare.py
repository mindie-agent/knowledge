"""Public copy, candidate immutability, pending records, capture non-blocking."""

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

from contribution.support import ordinary_md  # noqa: E402
from mindie_knowledge.contribution.documents import (  # noqa: E402
    DIGEST_PREFIX,
    MarkdownDocument,
    content_digest,
    require_git_sha,
)
from mindie_knowledge.contribution.errors import IdentityError  # noqa: E402
from mindie_knowledge.contribution.pending import STATUS_AWAITING, STATUS_BLOCKED, load_pending  # noqa: E402
from mindie_knowledge.contribution.public import prepare_public_copy  # noqa: E402
from mindie_knowledge.contribution.submit import after_capture, prepare_candidate  # noqa: E402


LEAK = "Contact the box at 10.20.30.40 during the outage."
ORDINARY = ordinary_md(
    "一次图模式启动失败的排查经验",
    "当时遇到了图模式启动失败。检查后发现依赖版本不一致，采用固定版本后启动成功。"
    "只在当时环境验证过，其他版本尚未确认。",
)


class Documents(unittest.TestCase):
    def test_title_and_body_are_the_only_required_fields(self):
        doc = MarkdownDocument.from_text(ORDINARY)
        self.assertEqual(doc.title, "一次图模式启动失败的排查经验")
        self.assertIn("当时遇到了", doc.body)
        self.assertTrue(doc.digest.startswith(DIGEST_PREFIX))

    def test_digest_cannot_stand_in_for_git_identity(self):
        digest = content_digest("t", "body text")
        with self.assertRaises(IdentityError):
            require_git_sha(digest)
        with self.assertRaises(IdentityError):
            require_git_sha(digest.removeprefix(DIGEST_PREFIX))


class PublicCopy(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def test_candidate_is_unchanged_and_leaks_are_masked(self):
        candidate = self.tmp / "candidate.md"
        text = ordinary_md("内部地址", LEAK)
        candidate.write_text(text, encoding="utf-8")
        copy = prepare_public_copy(text, public_root=self.tmp / "public")
        self.assertFalse(copy.blocked)
        self.assertNotIn("10.20.30.40", copy.text)
        self.assertIn("[redacted:ipv4-address]", copy.text)
        self.assertIn("内部地址", copy.text)
        self.assertEqual(candidate.read_text(encoding="utf-8"), text)
        self.assertTrue(copy.path and copy.path.is_file())

    def test_missing_title_blocks_export_only(self):
        copy = prepare_public_copy("just a body with no heading\n")
        # first line becomes title; empty body is rejected
        copy = prepare_public_copy("# only-title\n\n")
        self.assertTrue(copy.blocked)


class PendingAndCapture(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.state = self.tmp / "state"
        self.public = self.tmp / "public"
        self.candidate = self.tmp / "note.md"
        self.candidate.write_text(ORDINARY, encoding="utf-8")

    def test_same_content_reuses_pending_record(self):
        first = prepare_candidate(self.candidate, state_root=self.state, public_root=self.public)
        second = prepare_candidate(self.candidate, state_root=self.state, public_root=self.public)
        self.assertEqual(first.content_digest, second.content_digest)
        self.assertEqual(
            load_pending(self.state, first.content_digest).content_digest,
            first.content_digest,
        )
        self.assertEqual(self.candidate.read_text(encoding="utf-8"), ORDINARY)

    def test_after_capture_never_blocks_and_leaves_pending_without_transport(self):
        result = after_capture(
            self.candidate,
            state_root=self.state,
            public_root=self.public,
        )
        self.assertFalse(result["blocked_capture"])
        pending = result["pending"]
        self.assertEqual(pending["status"], STATUS_AWAITING)
        self.assertIn("transport", pending["last_error"])

    def test_redaction_block_does_not_touch_candidate(self):
        # Title-only document cannot be published; candidate stays.
        self.candidate.write_text("# 标题\n\n", encoding="utf-8")
        result = after_capture(self.candidate, state_root=self.state, public_root=self.public)
        self.assertFalse(result["blocked_capture"])
        self.assertEqual(result["pending"]["status"], STATUS_BLOCKED)
        self.assertEqual(self.candidate.read_text(encoding="utf-8"), "# 标题\n\n")


if __name__ == "__main__":
    unittest.main()
