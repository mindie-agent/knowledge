"""Opt-in live OpenViking loopback path (MINDIE_KNOWLEDGE_LIVE_OV=1)."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from mindie_knowledge.server.capture import capture, delete
from mindie_knowledge.server.layers import load_config
from mindie_knowledge.server.query import explain, query
from mindie_knowledge.local.reconcile import reconcile_markdown
from mindie_knowledge.maintenance import maintain


def _openviking_ready() -> bool:
    if os.environ.get("MINDIE_KNOWLEDGE_LIVE_OV") != "1":
        return False
    try:
        import openviking_sdk  # noqa: F401
    except ImportError:
        return False
    from mindie_knowledge.local.instance import which

    return which("openviking-server") is not None


@unittest.skipUnless(_openviking_ready(), "set MINDIE_KNOWLEDGE_LIVE_OV=1 with openviking-server installed")
class LiveOpenViking(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        candidate = root / "candidate"
        project = root / "project"
        candidate.mkdir()
        project.mkdir()
        state = root / "instance"
        self.project = project
        self.config = load_config(
            {
                "backend": "openviking",
                "shared_sync": {"enabled": False},
                "state_root": str(state),
                "layers": {
                    "shared": {"enabled": False},
                    "project": {"root": str(project)},
                    "candidate": {"root": str(candidate)},
                },
            },
            env={},
            base_dir=root,
        )

    def tearDown(self) -> None:
        from mindie_knowledge.local.instance import instance_for_config

        instance_for_config(self.config).stop()

    def test_capture_query_update_delete_and_restart(self) -> None:
        first = capture(
            title="graph replay mismatch",
            content="Eager passed but ACL graph replay diverged on padding metadata.",
            config=self.config,
        )
        self.assertTrue(first["ok"], first)
        self.assertEqual("ready", first["index"], first)
        found = query(self.config, text="graph replay padding").to_dict()
        self.assertGreaterEqual(found["count"], 1, found)
        self.assertFalse(found.get("unavailable"))
        body = explain(self.config, first["uri"])
        self.assertTrue(body["found"])
        self.assertIn("padding metadata", body["content"])

        updated = capture(
            title="graph replay mismatch",
            content="Updated: the mismatch was a reused slot mapping, not padding.",
            config=self.config,
        )
        self.assertEqual(first["slug"], updated["slug"])
        again = explain(self.config, updated["uri"])
        self.assertIn("slot mapping", again["content"])

        from mindie_knowledge.local.instance import instance_for_config

        instance_for_config(self.config).stop()
        self.assertTrue(query(self.config, text="slot mapping").unavailable)
        prepared = maintain(self.config, verify=True)
        self.assertTrue(prepared["ready"], prepared)
        restarted = query(self.config, text="slot mapping").to_dict()
        self.assertGreaterEqual(restarted["count"], 1, restarted)

        delete(updated["uri"], config=self.config)
        missing = query(self.config, text="slot mapping").to_dict()
        self.assertEqual([], missing["results"])

    def test_project_add_edit_delete_and_pending_recovery(self) -> None:
        note = self.project / "project-note.md"
        note.write_text(
            "# Project graph knowledge\n\nProject graph padding uses a unique canary named projectquartz.\n",
            encoding="utf-8",
        )
        prepared = maintain(self.config, verify=True)
        self.assertTrue(prepared["ready"], prepared)
        found = query(
            self.config, text="project graph padding projectquartz", layers=["project"]
        ).to_dict()
        self.assertGreaterEqual(found["count"], 1, found)
        self.assertFalse(found.get("unavailable"))
        self.assertFalse(found.get("degraded"))
        self.assertTrue(explain(self.config, str(note), layers=["project"]).get("found"))

        note.write_text(
            "# Project graph knowledge\n\nUpdated project padding canary named projectonyx.\n",
            encoding="utf-8",
        )
        self.assertTrue(reconcile_markdown(self.config).ok)
        edited = query(self.config, text="projectonyx", layers=["project"]).to_dict()
        self.assertGreaterEqual(edited["count"], 1, edited)
        body = explain(self.config, str(note), layers=["project"])
        self.assertIn("projectonyx", body["content"])
        self.assertNotIn("projectquartz", body["content"])

        note.unlink()
        self.assertTrue(reconcile_markdown(self.config).ok)
        removed = query(self.config, text="projectonyx", layers=["project"]).to_dict()
        self.assertEqual(0, removed["count"], removed)

        pending_path = Path(self.config.mount("candidate").roots[0]) / "offline-pending.md"
        pending_path.write_text(
            "# Offline pending\n\nPending recovery canary named pendingonyx.\n",
            encoding="utf-8",
        )
        self.assertTrue(reconcile_markdown(self.config).ok)
        recovered = query(self.config, text="pendingonyx", layers=["candidate"]).to_dict()
        self.assertGreaterEqual(recovered["count"], 1, recovered)
