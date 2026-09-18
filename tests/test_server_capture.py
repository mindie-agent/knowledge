"""Markdown capture: title+content only, candidate layer only."""

from __future__ import annotations

import pathlib
import hashlib
import json
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures" / "server"))

import support  # noqa: E402
from mindie_knowledge.local.backend import MemoryBackend
from mindie_knowledge.local.reconcile import reconcile_markdown
from mindie_knowledge.markdown import meta_path, save_document
from mindie_knowledge.server.capture import CaptureRefused, CaptureRejected, capture, delete
from mindie_knowledge.server.query import query


def _config(tmp: str):
    config = support.build_config(candidate=tmp)
    config.retrieval = MemoryBackend()
    return config


def test_catalog_locates_legacy_title_and_update_reads_current_original(tmp_path, monkeypatch):
    from mindie_knowledge.catalog import refresh_catalog
    from mindie_knowledge.server.layers import load_config

    root = tmp_path / "candidate"
    root.mkdir()
    config = load_config({"backend": "memory", "state_root": str(tmp_path / "state"),
                          "layers": {"candidate": str(root), "shared": {"enabled": False},
                                     "project": {"enabled": False}}}, env={})
    for number in range(80):
        (root / f"unrelated-{number}.md").write_text(f"# Unrelated {number}\n\nEarlier condition.", encoding="utf-8")
    legacy = root / "manual-legacy-name.md"
    legacy.write_text("# Existing legacy title\n\nOriginal evidence.", encoding="utf-8")
    assert refresh_catalog(config)["status"] == "ready"
    meta_path(legacy).write_text(json.dumps({"conditions": {"soc": "A3"}}), encoding="utf-8")
    original, reads = pathlib.Path.open, []

    def counted(path, *args, **kwargs):
        if path.name.startswith("unrelated-"):
            reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", counted)
    updated = capture(title="Existing legacy title", content="Updated observation, still uncertain.", config=config, index=False)
    assert updated["path"] == str(legacy) and updated["document"]["conditions"] == {"soc": "A3"}
    assert updated["title_lookup"]["method"] == "catalog" and reads == []
    assert len(list(root.glob("*.md"))) == 81


def test_cold_capture_keeps_legacy_title_update_without_setup(tmp_path):
    from mindie_knowledge.server.layers import load_config

    legacy = tmp_path / "legacy.md"
    legacy.write_text("# Original title\n\nEarlier evidence.", encoding="utf-8")
    config = load_config({"backend": "memory", "state_root": str(tmp_path / "state"),
                          "layers": {"candidate": str(tmp_path)}}, env={})
    updated = capture(title="Original title", content="Retained new evidence.", config=config, index=False)
    assert updated["path"] == str(legacy)
    assert updated["title_lookup"] == {"method": "bounded_scan", "incomplete": False}
    assert not config.state_root.exists()


class CaptureMarkdown(unittest.TestCase):
    def test_title_and_content_are_enough(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            payload = capture(
                title="一次图模式启动失败的排查经验",
                content="当时遇到了启动失败。检查后发现 metadata 未复用，修复后启动成功。",
                config=config,
            )
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["layer"], "candidate")
            self.assertTrue(pathlib.Path(payload["path"]).is_file())
            self.assertIn("metadata", pathlib.Path(payload["path"]).read_text(encoding="utf-8"))
            found = query(config, text="图模式启动失败").to_dict()
            self.assertEqual(1, found["count"])
            self.assertEqual(payload["uri"], found["results"][0]["uri"])

    def test_unknown_conditions_are_omitted_known_are_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            payload = capture(
                title="HCCL hang",
                content="Collective stalled after rank map mismatch.",
                conditions={"soc": "Ascend910B4", "cann": "unknown", "vllm": ""},
                source={"session": "abc"},
                config=config,
            )
            document = payload["document"]
            self.assertEqual({"soc": "Ascend910B4"}, document["conditions"])
            self.assertEqual({"session": "abc"}, document["source"])

    def test_missing_title_or_content_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            with self.assertRaises(CaptureRejected):
                capture(title="", content="body", config=config)
            with self.assertRaises(CaptureRejected):
                capture(title="title", content="  ", config=config)

    def test_non_candidate_layer_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            for layer in ("shared", "project", "verified"):
                with self.assertRaises(CaptureRefused):
                    capture(title="x", content="y", layer=layer, config=config)

    def test_read_only_candidate_is_not_written(self) -> None:
        from mindie_knowledge.server.layers import load_config
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "read-only"
            config = load_config({"layers": {"candidate": {"root": str(target), "read_only": True}}}, env={})
            with self.assertRaises(CaptureRefused):
                capture(title="Reference", content="Must not write here.", config=config, index=False)
            self.assertFalse(target.exists())

    def test_delete_removes_file_and_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            payload = capture(title="temp note", content="delete me please", config=config)
            delete(payload["uri"], config=config)
            self.assertFalse(pathlib.Path(payload["path"]).exists())
            found = query(config, text="delete me please").to_dict()
            self.assertEqual([], found["results"])

    def test_distinct_chinese_titles_keep_separate_documents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            first = capture(title="图模式失败", content="graph compile failed", config=config)
            second = capture(title="通信超时", content="rpc timed out", config=config)
            updated = capture(title="图模式失败", content="graph compile failed again", config=config)
            self.assertNotEqual(first["path"], second["path"])
            self.assertEqual(first["path"], updated["path"])
            self.assertTrue(pathlib.Path(first["path"]).is_file())
            self.assertIn("图模式失败", pathlib.Path(first["path"]).read_text(encoding="utf-8"))
            self.assertIn("again", pathlib.Path(first["path"]).read_text(encoding="utf-8"))
            self.assertIn("通信超时", pathlib.Path(second["path"]).read_text(encoding="utf-8"))
            self.assertEqual("图模式失败", query(config, text="graph compile").to_dict()["results"][0]["title"])

    def test_indexed_snapshot_is_not_relabelled_after_concurrent_edit(self) -> None:
        for newline in (b"\n", b"\r\n"):
            for concurrent_edit in (False, True):
                with self.subTest(newline=newline, concurrent_edit=concurrent_edit), tempfile.TemporaryDirectory() as tmp:
                    config = _config(tmp)
                    original_bytes = None
                    saved_path = None
                    replacement = b"# Concurrent observation\n\nA later edit must be indexed.\n"

                    def save_with_newlines(*args, **kwargs):
                        nonlocal original_bytes, saved_path
                        document = save_document(*args, **kwargs)
                        saved_path = document.path
                        original_bytes = saved_path.read_bytes().replace(b"\n", newline)
                        saved_path.write_bytes(original_bytes)
                        return document

                    upsert = config.retrieval.upsert

                    def edit_during_upsert(uri, content, *, layer, wait=True):
                        upsert(uri, content, layer=layer, wait=wait)
                        if concurrent_edit:
                            saved_path.write_bytes(replacement)

                    with mock.patch("mindie_knowledge.server.capture.save_document", side_effect=save_with_newlines), \
                            mock.patch.object(config.retrieval, "upsert", side_effect=edit_during_upsert):
                        captured = capture(title="Concurrent observation", content="Original indexed observation.", config=config)

                    ledger = json.loads((config.state_root / "markdown-index.json").read_text(encoding="utf-8"))
                    self.assertEqual(hashlib.sha256(original_bytes).hexdigest(), ledger["documents"][captured["ref"]]["sha256"])
                    self.assertTrue(config.retrieval.check_document(captured["ref"], original_bytes.decode("utf-8").replace("\r\n", "\n")))
                    reconciled = reconcile_markdown(config, layers=["candidate"])
                    # A changed file is fixed by the next ordinary pass, while
                    # an unchanged CRLF file must not incur another embedding.
                    self.assertEqual(int(concurrent_edit), reconciled.upserted)
                    if concurrent_edit:
                        self.assertTrue(config.retrieval.check_document(captured["ref"], replacement.decode("utf-8")))

    def test_dry_run_does_not_write_or_delete_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            original = capture(
                title="dry run existing",
                content="keep this original text",
                conditions={"soc": "Ascend910B4"},
                source={"session": "s1"},
                config=config,
            )
            path = pathlib.Path(original["path"])
            sidecar = meta_path(path)
            before_md = path.read_bytes()
            before_meta = sidecar.read_bytes()
            indexed_before = dict(config.retrieval.documents)

            with mock.patch("mindie_knowledge.server.capture.backend_for_config") as backend_factory:
                proposed = capture(
                    title="dry run existing",
                    content="replacement must not land",
                    dry_run=True,
                    config=config,
                )
                backend_factory.assert_not_called()

            self.assertTrue(proposed["ok"])
            self.assertTrue(proposed["dry_run"])
            self.assertEqual("skipped", proposed["index"])
            self.assertTrue(proposed["would_update"])
            self.assertEqual(before_md, path.read_bytes())
            self.assertEqual(before_meta, sidecar.read_bytes())
            self.assertEqual(indexed_before, config.retrieval.documents)

            fresh = capture(title="brand new dry run", content="never write this", dry_run=True, config=config)
            self.assertFalse(pathlib.Path(fresh["path"]).exists())
            self.assertFalse(fresh["would_update"])
            self.assertEqual([path], list(pathlib.Path(tmp).glob("*.md")))
