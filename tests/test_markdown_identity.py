"""Collision-resistant Markdown identity without user-supplied ids."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mindie_knowledge.markdown import (
    document_slug,
    load_document,
    save_document,
    slugify,
)


class DocumentIdentity(unittest.TestCase):
    def test_distinct_chinese_titles_do_not_share_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = save_document(root, layer="candidate", title="图模式失败", content="graph compile")
            second = save_document(root, layer="candidate", title="通信超时", content="rpc timeout")
            self.assertNotEqual(first.path, second.path)
            self.assertNotEqual(first.uri, second.uri)
            self.assertNotEqual(slugify("图模式失败"), "图模式失败")
            self.assertEqual("captured-entry", slugify("图模式失败"))
            self.assertTrue(first.path.is_file())
            self.assertTrue(second.path.is_file())
            self.assertEqual("图模式失败", load_document(first.path, layer="candidate", root=root).title)
            self.assertEqual("通信超时", load_document(second.path, layer="candidate", root=root).title)

    def test_same_title_updates_the_same_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = save_document(root, layer="candidate", title="图模式失败", content="first body")
            again = save_document(
                root,
                layer="candidate",
                title="图模式失败",
                content="second body",
                path=first.path,
            )
            self.assertEqual(first.path, again.path)
            self.assertEqual("second body", load_document(first.path, layer="candidate", root=root).content)
            self.assertEqual(1, len(list(root.glob("*.md"))))

    def test_ascii_normalization_collision_still_splits(self) -> None:
        self.assertEqual(slugify("Hello World!!!"), slugify("Hello World"))
        self.assertNotEqual(document_slug("Hello World!!!"), document_slug("Hello World"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = save_document(root, layer="candidate", title="Hello World!!!", content="one")
            second = save_document(root, layer="candidate", title="Hello World", content="two")
            self.assertNotEqual(first.path, second.path)
            self.assertEqual("one", load_document(first.path, layer="candidate", root=root).content)

    def test_nested_same_basename_gets_distinct_uris(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            left = root / "a" / "note.md"
            right = root / "b" / "note.md"
            left.parent.mkdir()
            right.parent.mkdir()
            left.write_text("# Left note\n\nfrom a\n", encoding="utf-8")
            right.write_text("# Right note\n\nfrom b\n", encoding="utf-8")
            loaded_left = load_document(left, layer="project", root=root)
            loaded_right = load_document(right, layer="project", root=root)
            self.assertNotEqual(loaded_left.uri, loaded_right.uri)
            self.assertEqual("viking://resources/project/a/note.md", loaded_left.uri)
            self.assertEqual("viking://resources/project/b/note.md", loaded_right.uri)

    def test_existing_stem_file_stays_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "captured-entry.md"
            path.write_text("# 图模式失败\n\nold stored body\n", encoding="utf-8")
            sidecar = path.with_suffix(".meta.json")
            sidecar.write_text(
                json.dumps(
                    {
                        "slug": "captured-entry",
                        "title": "图模式失败",
                        "layer": "candidate",
                        "uri": "viking://resources/candidate/captured-entry.md",
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_document(path, layer="candidate", root=root)
            self.assertEqual("图模式失败", loaded.title)
            self.assertEqual("viking://resources/candidate/captured-entry.md", loaded.uri)
            self.assertEqual("old stored body", loaded.content)


def test_updating_prose_keeps_recorded_context_without_requiring_metadata(tmp_path):
    from mindie_knowledge.markdown import save_document, meta_path
    import json

    original = save_document(tmp_path, layer="candidate", title="Reference", content="Observed once.",
                             source={"run": "old evidence"}, conditions={"soc": "A3"},
                             evidence="Existing trace excerpt")
    updated = save_document(tmp_path, layer="candidate", title="Reference", content="Still uncertain.")
    assert updated.path == original.path
    assert updated.source == original.source
    assert updated.conditions == original.conditions
    assert updated.evidence == original.evidence
    assert "status" not in json.loads(meta_path(updated.path).read_text(encoding="utf-8"))
