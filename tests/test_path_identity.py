"""Path aliases must not change scope, task admission, or transcript cursors."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import transcript_double as parser
from mindie_knowledge.filesystem import (
    filesystem_path, git_path, path_aliases, path_identity, path_is_within, resolved_path,
)
from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.engine import transcript_cursor, TranscriptCursorError
from mindie_knowledge.loop.history import plan
from mindie_knowledge.loop.settings import load, transition


class Cursors:
    def __init__(self, rows):
        self.rows = rows
        self.lookups = []

    def cursor(self, key):
        self.lookups.append(key)
        return self.rows.get(key)


class PathIdentityTests(unittest.TestCase):
    def setUp(self):
        self.root = filesystem_path(tempfile.mkdtemp(prefix="mindie-path-identity-"))
        self.addCleanup(lambda: shutil.rmtree(self.root))
        self.source = self.root / "source.jsonl"
        rows = [dict(type="session_meta", payload=dict(id="task"))]
        rows.extend(dict(timestamp="2026-10-15T00:00:00Z", type="response_item",
                         payload=dict(type="message", role="user", content=[
                             dict(type="input_text", text="Synthetic record " + str(i))]))
                    for i in range(10))
        self.source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def cursor(self, finish=100):
        return dict(identity=parser.identify(self.source).serialize(), finish=finish)

    def test_one_proven_legacy_cursor_keeps_key_and_zero_is_not_missing(self):
        for finish in (0, 100):
            row = self.cursor(finish)
            store = Cursors({"legacy": row})
            with patch("mindie_knowledge.loop.engine.path_aliases",
                       return_value=("canonical", "legacy")):
                key, saved = transcript_cursor(store, self.source, parser, parser.identify(self.source))
            self.assertEqual((key, saved), ("legacy", row))
            self.assertEqual(store.lookups, ["canonical", "legacy"])

    def test_conflicting_aliases_never_select_a_cursor_or_reset(self):
        rows = {"canonical": self.cursor(100), "legacy": self.cursor(0)}
        store = Cursors(rows)
        with patch("mindie_knowledge.loop.engine.path_aliases",
                   return_value=("canonical", "legacy")):
            with self.assertRaises(TranscriptCursorError) as caught:
                transcript_cursor(store, self.source, parser, parser.identify(self.source))
        self.assertEqual(caught.exception.code, "transcript_cursor_alias_conflict")
        self.assertEqual(store.rows, rows)

    def test_existing_unverifiable_cursor_is_not_first_use(self):
        row = self.cursor()
        cases = [
            ({"identity": "{broken", "finish": 100}, parser.identify(self.source),
             "persisted_identity_invalid"),
            (row, None, "transcript_cursor_identity_mismatch"),
        ]
        self.source.write_bytes(b"changed source" * 100)
        cases.append((row, parser.identify(self.source), "transcript_cursor_identity_mismatch"))
        for saved, identity, code in cases:
            with self.subTest(code=code), patch("mindie_knowledge.loop.engine.path_aliases",
                                               return_value=("canonical", "legacy")):
                with self.assertRaises(TranscriptCursorError) as caught:
                    transcript_cursor(Cursors({"legacy": saved}), self.source, parser, identity)
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(transcript_cursor(Cursors({}), self.source, parser,
                                          parser.identify(self.source))[1], None)

    def test_component_containment_rejects_sibling_prefix_and_symlink_escape(self):
        scope = self.root / "scope"
        scope.mkdir()
        sibling = self.root / "scope-other"
        sibling.mkdir()
        self.assertFalse(path_is_within(sibling, scope))
        self.assertFalse(path_is_within(scope / "../scope-other", scope))
        try:
            (scope / "link").symlink_to(sibling, target_is_directory=True)
        except OSError as exc:
            if os.name == "nt":
                self.skipTest("directory symlinks unavailable: " + type(exc).__name__)
            raise
        self.assertFalse(path_is_within(scope / "link", scope))

    def test_missing_source_is_never_a_successful_first_use(self):
        self.source.unlink()
        self.assertIsNone(parser.identify(self.source))
        with self.assertRaises(TranscriptCursorError) as caught:
            transcript_cursor(Cursors({}), self.source, parser, None)
        self.assertEqual(caught.exception.code, "transcript_unavailable")

    @unittest.skipUnless(os.name == "nt", "native Windows path aliases")
    def test_aliases_reuse_cursor_and_preserve_admission_generation(self):
        ordinary = git_path(self.source)
        aliases = [ordinary, str(self.source), ordinary.swapcase()]
        self.assertEqual(len({path_identity(value) for value in aliases}), 1)
        saved = self.cursor(100)
        store = Cursors({ordinary: saved})
        for value in aliases:
            key, cursor = transcript_cursor(store, value, parser, parser.identify(value))
            self.assertEqual((key, cursor), (ordinary, saved))
        self.assertLessEqual(len(path_aliases(ordinary)), 4)
        project = self.root / "project"
        project.mkdir()
        admission = Admission(self.root / "admission.sqlite3")
        first = admission.activate("task", project_root=git_path(project), not_before=123)
        again = admission.activate("task", project_root=str(project), not_before=456)
        self.assertEqual(first, again)
        previous = dict(schema="mindie-community-config/1", enabled=True,
                        generation="keep", enabled_at=123, repository="owner/repository",
                        branch="main", project_roots=[git_path(project)], idle_seconds=300)
        requested = dict(previous, project_roots=[str(project)])
        updated = transition(previous, requested)
        self.assertEqual(updated["generation"], "keep")
        self.assertEqual(updated["enabled_at"], 123)
        self.assertEqual(updated["project_roots"], previous["project_roots"])
        settings_path = self.root / "community.json"
        settings_path.write_text(json.dumps(previous))
        self.assertTrue(load(settings_path).in_scope(str(project)))

    @unittest.skipUnless(os.name == "nt", "native Windows path aliases")
    def test_history_alias_preserves_manifest_and_reads_deep_source(self):
        output = self.root / "plan"
        first = plan(str(self.source), "task", output, parser=parser)
        original = first["source"]
        resumed = plan(git_path(self.source), "task", output, parser=parser)
        self.assertEqual(resumed["source"], original)
        self.assertEqual(resumed["cursor"], first["cursor"])
        deep = self.root.joinpath(*(["segment-" + "x" * 48] * 5), "task.jsonl")
        deep.parent.mkdir(parents=True)
        deep.write_bytes(self.source.read_bytes())
        result = plan(git_path(deep), "task", self.root / "deep-plan", parser=parser)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["cursor"], deep.stat().st_size)
        self.assertTrue(str(resolved_path(git_path(deep))).startswith("\\\\?\\"))

    @unittest.skipUnless(os.name == "nt", "native Windows path semantics")
    def test_unc_namespace_case_and_junction_scope(self):
        values = [r"\\?\UNC\SERVER\Share\Dir", r"\\?\unc\server\share\dir",
                  r"\\server\share\dir"]
        keys = []
        for value in values:
            with patch("mindie_knowledge.filesystem.resolved_path", return_value=Path(value)):
                keys.append(path_identity(value))
        self.assertEqual(len(set(keys)), 1)
        scope = self.root / "scope"
        outside = self.root / "outside"
        scope.mkdir()
        outside.mkdir()
        link = scope / "junction"
        result = subprocess.run(
            ["cmd.exe", "/u", "/c", "mklink", "/J", git_path(link), git_path(outside)],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout.decode("utf-16le", errors="replace"))
        self.addCleanup(link.rmdir)
        self.assertFalse(path_is_within(link, scope))
