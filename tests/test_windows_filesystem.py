"""Model-free filesystem regression, also runnable with native unittest."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mindie_knowledge.filesystem import (
    _windows_extended, _windows_logical, filesystem_path, git_path, sqlite_file_uri,
)
from mindie_knowledge.gitread import with_windows_longpaths
from mindie_knowledge.loop.diagnostics import _ro
from mindie_knowledge.loop.documents import make_entry
from mindie_knowledge.loop.feed import Feed
from mindie_knowledge.loop.store import Store
from mindie_knowledge.materials import MaterialStore
from mindie_knowledge.publication_contract import make_contract, render_contract
from mindie_knowledge.state_layout import DATABASE, state_root
from package_fixture import write_package

EVIDENCE = []


class WindowsPathSpellingTests(unittest.TestCase):
    def test_extended_paths_preserve_drive_unc_and_special_characters(self):
        for logical, extended in [
            (r"C:\root\space #100%\body.md", r"\\?\C:\root\space #100%\body.md"),
            (r"\\server\share\body.md", r"\\?\UNC\server\share\body.md"),
            ("C:/root/old/../\u77e5\u8bc6/body.md", "\\\\?\\C:\\root\\\u77e5\u8bc6\\body.md"),
        ]:
            self.assertEqual(_windows_extended(logical), extended)
            self.assertEqual(_windows_extended(extended), extended)
            self.assertEqual(_windows_extended(_windows_logical(extended)), extended)

    def test_sqlite_uri_encodes_filename_syntax(self):
        with tempfile.TemporaryDirectory(prefix="knowledge-uri-") as directory:
            path = filesystem_path(directory) / "space #100% \u77e5\u8bc6.sqlite3"
            import sqlite3
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE receipt(value TEXT)")
                db.execute("INSERT INTO receipt VALUES ('preserved')")
                db.commit()
            with closing(sqlite3.connect(sqlite_file_uri(path) + "?mode=rw", uri=True)) as db:
                self.assertEqual(db.execute("SELECT value FROM receipt").fetchone()[0], "preserved")
            with closing(_ro(path)) as db:
                self.assertEqual(db.execute("SELECT value FROM receipt").fetchone()[0], "preserved")


class LongFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.logical = Path(tempfile.mkdtemp(prefix="knowledge-longpath-"))
        self.root = filesystem_path(self.logical)

    def tearDown(self):
        def remove_readonly(function, path, exc):
            # Git creates read-only pack files on Windows. This only removes
            # this test's disposable repository after all handles are closed.
            if not isinstance(exc[1], PermissionError):
                raise exc[1]
            os.chmod(path, stat.S_IWRITE)
            function(path)
        shutil.rmtree(self.root, onerror=remove_readonly)
        self.assertFalse(self.root.exists())

    def test_deep_existing_store_preserves_material_and_uncertain_receipt(self):
        deep = self.logical.joinpath(*(["segment-" + "x" * 45] * 5), "space #100% \u77e5\u8bc6")
        self.assertGreater(len(str(deep)), 260)
        with closing(Store(deep, "demo")) as store:
            store.create_draft(kind="experience", title="Longpath sentinel", summary="Synthetic",
                               content="Synthetic longpath sentinel body; no external execution.")
            with store.db:
                store.db.execute("INSERT INTO material_summary_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                 ("attempt", "task", "batch", "body", "input", "policy", "[]",
                                  "outcome_unknown", '{"paid_result":"retained"}', "interrupted", 1, 2))
            hit = store.query("Longpath sentinel")["results"][0]
            navigation = store.explain(hit["ref"])
            block = store.explain(navigation.get("first_block_ref") or hit["ref"])
            material = {str(path.relative_to(store.root)): path.read_bytes()
                        for path in store.root.rglob("*.md")}
            selected = store.root
        self.assertEqual(state_root(deep, "demo"), selected)
        with closing(_ro(selected / DATABASE)) as db:
            self.assertEqual(db.execute("SELECT status FROM material_summary_attempts").fetchone()[0],
                             "outcome_unknown")
        with closing(Store(deep, "demo")) as store:
            self.assertEqual(store.root, selected)
            self.assertTrue(store.query("Longpath sentinel")["results"])
            self.assertEqual(store.explain(block["ref"]), block)
            receipt = store.db.execute("SELECT status,response FROM material_summary_attempts").fetchone()
            self.assertEqual(tuple(receipt), ("outcome_unknown", '{"paid_result":"retained"}'))
            self.assertEqual({str(path.relative_to(store.root)): path.read_bytes()
                              for path in store.root.rglob("*.md")}, material)
        self.assertEqual(json.loads((filesystem_path(deep) / "demo/state-layout.json").read_text())["format"], 1)
        EVIDENCE.append(dict(case="deep-store", database_path_length=len(git_path(selected / DATABASE)),
                             body_sha256=block["sha256"], uncertain_receipt_preserved=True))

    def test_feed_deep_packages_sync_read_restart_and_retire(self):
        # Git roots stay at ordinary install depth; hashed task descendants
        # cross MAX_PATH even at the default product root.
        repo = self.root / "source space \u77e5\u8bc6"
        repo.mkdir()
        (repo / ".gitattributes").write_bytes(b"* -text\n")
        (repo / "publication-contract.json").write_bytes(
            render_contract(make_contract("demo", "a" * 40)).encode("utf-8"))
        env = dict(os.environ, **with_windows_longpaths({}))

        def git(*args):
            return subprocess.check_output(["git", "-C", git_path(repo), *args],
                                           env=env, text=True, encoding="utf-8").strip()

        git("init", "-q", "-b", "main")
        git("config", "user.name", "synthetic")
        git("config", "user.email", "synthetic@example.invalid")
        git("config", "core.autocrlf", "false")

        def publish(suffix):
            tasks = repo / "tasks"
            if tasks.exists():
                shutil.rmtree(tasks)
            doc = make_entry(entry_id="1" * 64, domain="demo", kind="experience",
                             title="Longpath feed " + suffix, summary="Synthetic fixture",
                             content="Synthetic longpath feed body " + suffix)
            with closing(MaterialStore(self.root / ("package-" + suffix), "demo")) as materials:
                bound = materials.put_document(doc)
                package = materials.export_task(doc["entry_id"], revision=bound["revision"])
            write_package(repo, package)
            git("add", "-A")
            git("commit", "-qm", "synthetic fixture")
            return git("rev-parse", "HEAD")

        first = publish("first")
        from mindie_knowledge.community import gitops
        from mindie_knowledge.community.common import Deadline
        deadline = Deadline()
        writer = gitops.ensure_clone(Path(git_path(repo)).as_uri(), self.root / "publisher",
                                    deadline, env=with_windows_longpaths({}))
        gitops.checkout_new(writer, "synthetic-local-only", "origin/main", deadline)
        relative = "tasks/" + "2" * 64 + "/blocks/" + "block-" + "b" * 100 + ".md"
        paths = gitops.apply_files(writer, [dict(path=relative, content="Synthetic publisher readback\n")])
        committed = gitops.stage_and_commit(writer, paths, "synthetic local commit", deadline)
        self.assertIsNotNone(committed)
        self.assertEqual(gitops.ls_tree(writer, committed, deadline)[relative], "100644")
        self.assertEqual(gitops.show_file(writer, committed, relative, deadline), "Synthetic publisher readback\n")
        self.assertEqual(gitops.current_head(writer, deadline), committed)
        config = dict(repository="synthetic/knowledge", domain="demo", ref="main", url=git_path(repo))
        staged_lengths = []
        original_stage = Feed._stage_tree_docs

        def stage(feed, commit, entries, deadline):
            result = original_stage(feed, commit, entries, deadline)
            staged_lengths.extend(len(git_path(path)) for path in feed._staging_dir(commit).rglob("*.md"))
            return result

        with closing(Store(self.logical / "data", "demo")) as store:
            feed = Feed(store, config)
            with patch.object(Feed, "_stage_tree_docs", stage):
                receipt = feed.sync()
            self.assertEqual(receipt["status"], "synced", receipt)
            self.assertEqual(receipt["commit"], first)
            self.assertGreater(max(staged_lengths), 260)
            hit = store.query("Longpath feed")["results"][0]
            navigation = store.explain(hit["ref"])
            block = store.explain(navigation.get("first_block_ref") or hit["ref"])
            body_path = store.materials.root / "tasks" / block["entry_id"] / "blocks" / (block["block_id"] + ".md")
            self.assertEqual(hashlib.sha256(body_path.read_bytes()).hexdigest(), block["sha256"])
            self.assertIn("first", block["content"])
            self.assertEqual(feed.sync()["status"], "unchanged")
            self.assertFalse((feed.dir / "staging").exists())
            stored = store.feed_get("published-commit")
        with closing(Store(self.logical / "data", "demo")) as store:
            feed = Feed(store, config)
            self.assertEqual(store.feed_get("published-commit"), stored)
            self.assertEqual(store.explain(block["ref"]), block)
            self.assertTrue(store.query("Longpath feed")["results"])
            second = publish("revised")
            receipt = feed.sync()
            self.assertEqual(receipt["status"], "synced", receipt)
            self.assertEqual(receipt["commit"], second)
            self.assertFalse((feed.dir / "staging").exists())
            with self.assertRaises(ValueError):
                store.explain(block["ref"])
            self.assertEqual(store.db.execute("SELECT count(*) FROM revisions").fetchone()[0], 1)
            EVIDENCE.append(dict(case="feed", git_root_path_length=len(git_path(feed.repo)),
                                 staged_path_length=max(staged_lengths),
                                 body_path_length=len(git_path(body_path)), body_sha256=block["sha256"],
                                 first_commit=first, current_commit=second, current_entries=1,
                                 publisher_local_commit=committed,
                                 publisher_body_path_length=len(git_path(writer / relative)),
                                 restart_readback=True, staging_cleaned=True, old_revision_retired=True))


if __name__ == "__main__":
    unittest.main()
