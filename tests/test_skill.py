"""The packaged skill is readable without a backend and installs independently."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from mindie_knowledge.cli import main
from mindie_knowledge.skill import install_skill, skill_files


class KnowledgeSkillTests(unittest.TestCase):
    def test_read_does_not_require_project_config_or_backend(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["skill"]), 0)
        self.assertEqual(stdout.getvalue(), skill_files()["SKILL.md"].decode("utf-8"))

    def test_install_supports_native_paths_and_preserves_unrelated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "中文 skills"
            target = install_skill(directory)
            extra = target / "local-note.md"
            extra.write_text("user note", encoding="utf-8")
            self.assertEqual(install_skill(directory), target)
            self.assertEqual(extra.read_text(), "user note")
            for name, contents in skill_files().items():
                self.assertEqual((target / name).read_bytes(), contents)

    def test_changed_copy_is_not_partly_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            target = install_skill(directory)
            interface = target / "agents/openai.yaml"
            interface.write_text("custom metadata", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                install_skill(directory)
            self.assertEqual(interface.read_text(), "custom metadata")
            install_skill(directory, force=True)
            self.assertEqual(interface.read_bytes(), skill_files()["agents/openai.yaml"])


if __name__ == "__main__":
    unittest.main()
