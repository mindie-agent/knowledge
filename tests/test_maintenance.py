"""Readiness belongs to installation and MCP lifecycle, never a query."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mindie_knowledge.distribution.sync import SwitchLock
from mindie_knowledge.local.backend import MemoryBackend, UnavailableBackend
from mindie_knowledge.maintenance import MaintenanceWorker, main, maintain, project_config
from mindie_knowledge.server.layers import load_config
from mindie_knowledge.server.query import query


class Maintenance(unittest.TestCase):
    def test_legacy_prepared_upgrade_refreshes_catalog_in_the_same_unchanged_pass(self):
        from distribution.helpers import FakeClient, embedding_info, make_release_dir
        from mindie_knowledge.catalog import get_catalog_document
        from mindie_knowledge.distribution.sync import DistributionState, check_and_sync, current_shared
        from mindie_knowledge.maintenance import refresh_references

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            for index in range(91):
                (root / "notes" / f"note-{index}.md").write_text(f"# Local {index}\n\nRecorded fact.\n", encoding="utf-8")
            bootstrap = root / "bootstrap"
            bootstrap.mkdir()
            config = load_config({"backend": "memory", "state_root": str(config.state_root),
                                  "layers": {"shared": str(bootstrap), "project": str(root / "notes"),
                                             "candidate": str(root / "candidate")},
                                  "shared_sync": {"enabled": True}, "publishing": {"enabled": False}}, env={})
            config.retrieval = MemoryBackend()
            release = make_release_dir(root / "release")
            client = FakeClient()
            self.assertEqual("switched", check_and_sync(config.state_root, str(release),
                             embedding_info=embedding_info(), client=client).status)
            state = DistributionState(config.state_root)
            legacy = state.read_current()
            # Model an already imported pre-reference release, retaining its valid vectors.
            for key in ("prepared_root", "prepared_manifest_sha256", "references_sha256", "metadata_status"):
                legacy.pop(key)
            state.write_current(legacy)

            def upgrade(*args, **kwargs):
                initial = json.loads((config.state_root / "maintenance.json").read_text())
                self.assertEqual(92, initial["catalog"]["documents"])
                self.assertIsNone(initial["catalog"]["shared_identity"]["prepared_root"])
                result = check_and_sync(config.state_root, str(release), embedding_info=embedding_info(),
                                        client=client, verify=True)
                self.assertEqual("unchanged", result.status)
                # A local edit during this same transport still needs its owned vector update.
                (root / "notes" / "fact.md").write_text("# Edited during legacy upgrade\n\nCurrent fact.\n", encoding="utf-8")
                return {"status": "ok", "sync": result.to_dict()}

            with patch("mindie_knowledge.publishing.run_once", side_effect=upgrade), \
                    patch.object(client, "import_ovpack", side_effect=AssertionError("valid legacy vectors reimported")), \
                    patch("mindie_knowledge.maintenance.refresh_references", wraps=refresh_references) as refresh:
                result = maintain(config, verify=True)
            self.assertTrue(result["ready"], result)
            self.assertEqual(2, refresh.call_count)
            self.assertEqual(94, result["catalog"]["documents"])
            active = current_shared(config.state_root)
            for key in ("source_git_sha", "root_uri", "prepared_root", "prepared_manifest_sha256"):
                self.assertEqual(active[key], result["catalog"]["shared_identity"][key])
            self.assertEqual(result["catalog"]["snapshot"], result["local_snapshot"])
            self.assertIsNotNone(get_catalog_document(config, active["root_uri"] + "/alpha.md"))
            self.assertEqual(92, len(config.retrieval.documents), "imported shared vectors have another owner")
            self.assertTrue(any("Current fact." in document["content"] for document in config.retrieval.documents.values()))

            with patch("mindie_knowledge.publishing.run_once", return_value={"sync": {"status": "unchanged"}}), \
                    patch("mindie_knowledge.distribution.references.prepared_shared_documents", side_effect=AssertionError("unchanged prepared bodies reread")), \
                    patch("mindie_knowledge.local.reconcile._scan_documents", side_effect=AssertionError("unchanged vector full scan")), \
                    patch("mindie_knowledge.maintenance.refresh_references", wraps=refresh_references) as refresh:
                unchanged = maintain(config, force=True)
            self.assertTrue(unchanged["ready"], unchanged)
            self.assertEqual(1, refresh.call_count)
            self.assertEqual(0, unchanged["catalog"]["read_bytes"])
            self.assertTrue(unchanged["local"]["reused"])

    def test_missing_or_corrupt_shared_pointer_preserves_catalog_and_reports_partial(self):
        from dataclasses import replace
        from mindie_knowledge.catalog import get_catalog_document, refresh_catalog
        from mindie_knowledge.maintenance import refresh_references
        from mindie_knowledge.markdown import load_document
        for damaged in (False, True):
            with self.subTest(damaged=damaged), tempfile.TemporaryDirectory() as tmp:
                config = self.config(Path(tmp))
                self.assertTrue(maintain(config, verify=True)["ready"])
                uri = "viking://resources/shared/v123456789abc/fact.md"
                doc = replace(load_document(Path(tmp) / "notes" / "fact.md", layer="shared"), uri=uri)
                refresh_catalog(config, extra_documents=[doc])
                identity = {"root_uri": "viking://resources/shared/v123456789abc"}
                if damaged:
                    pointer = config.state_root / "distribution" / "current.json"
                    pointer.parent.mkdir(parents=True, exist_ok=True)
                    pointer.write_text("{damaged", encoding="utf-8")
                result = refresh_references(config, {"shared_identity": identity}, verify=True)
                self.assertEqual("partial", result["status"])
                self.assertEqual(identity, result["shared_identity"])
                self.assertIsNotNone(get_catalog_document(config, uri))
                receipt = config.state_root / "maintenance.json"
                previous = json.loads(receipt.read_text())
                previous["catalog"] = result
                receipt.write_text(json.dumps(previous), encoding="utf-8")
                retried = maintain(config, force=True)
                self.assertFalse(retried["ready"])
                self.assertEqual("partial", retried["catalog"]["status"])
                self.assertTrue(query(config, text="maintenancecanary").degraded)

    def test_unchanged_pass_reuses_vectors_and_changed_delta_reads_only_changed_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            self.assertTrue(maintain(config, verify=True)["ready"])
            with patch("mindie_knowledge.local.reconcile._scan_documents", side_effect=AssertionError("full scan")):
                again = maintain(config, force=True)
                self.assertTrue(again["local"]["reused"])
                self.assertEqual(0, again["catalog"]["read_bytes"])
                (Path(tmp) / "notes" / "fact.md").write_text("# Updated\n\nDelta fact\n", encoding="utf-8")
                changed = maintain(config, force=True)
                self.assertTrue(changed["ready"], changed)
                self.assertEqual(1, changed["local"]["upserted"])
                (Path(tmp) / "notes" / "fact.md").unlink()
                deleted = maintain(config, force=True)
                self.assertEqual(1, deleted["local"]["deleted"])
                self.assertFalse(config.retrieval.documents)

    def test_explicit_catalog_refresh_and_failed_write_do_not_lose_vector_changes(self):
        from mindie_knowledge.catalog import refresh_catalog
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            self.assertTrue(maintain(config, verify=True)["ready"])
            note = Path(tmp) / "notes" / "fact.md"
            note.write_text("# Changed\n\nNew independent refresh\n", encoding="utf-8")
            refresh_catalog(config)
            with patch.object(config.retrieval, "upsert", side_effect=RuntimeError("offline")):
                self.assertFalse(maintain(config, force=True)["local_ready"])
            retried = maintain(config, force=True)
            self.assertTrue(retried["ready"], retried)
            self.assertEqual(1, retried["local"]["upserted"])
            self.assertIn("New independent refresh", next(iter(config.retrieval.documents.values()))["content"])

    def test_recreated_catalog_generation_cannot_hide_changes_or_missing_ledger(self):
        from mindie_knowledge.catalog import catalog_path
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            self.assertTrue(maintain(config, verify=True)["ready"])
            catalog_path(config).unlink()
            (Path(tmp) / "notes" / "fact.md").write_text("# Rebuilt\n\nNew body\n", encoding="utf-8")
            rebuilt = maintain(config, force=True)
            self.assertTrue(rebuilt["ready"], rebuilt)
            self.assertEqual(1, rebuilt["local"]["upserted"])
            (config.state_root / "markdown-index.json").unlink()
            config.retrieval.documents.clear()
            self.assertEqual(1, maintain(config, force=True)["local"]["upserted"])

    def test_release_transport_local_edit_is_reconciled_before_snapshot_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            self.assertTrue(maintain(config, verify=True)["ready"])
            def switched(*args, **kwargs):
                (Path(tmp) / "notes" / "fact.md").write_text("# Changed during download\n\nKeep current local facts\n", encoding="utf-8")
                return {"sync": {"status": "switched"}}
            with patch("mindie_knowledge.publishing.run_once", side_effect=switched), \
                    patch("mindie_knowledge.local.reconcile._scan_documents", side_effect=AssertionError("unexpected full scan")):
                changed = maintain(config, force=True)
            self.assertTrue(changed["ready"], changed)
            self.assertEqual(changed["catalog"]["snapshot"], changed["local_snapshot"])
            self.assertIn("Keep current local facts", next(iter(config.retrieval.documents.values()))["content"])
            with patch("mindie_knowledge.local.reconcile._scan_documents", side_effect=AssertionError("unexpected next full scan")):
                self.assertTrue(maintain(config, force=True)["local"]["reused"])

    def config(self, root):
        notes = root / "notes"
        notes.mkdir()
        (notes / "fact.md").write_text("# Recorded\n\nmaintenancecanary, still uncertain.\n", encoding="utf-8")
        config = load_config({"backend": "memory", "state_root": str(root / "state"),
                              "layers": {"shared": {"enabled": False}, "project": str(notes), "candidate": str(root / "candidate")},
                              "shared_sync": {"enabled": False}, "publishing": {"enabled": False}}, env={})
        config.retrieval = MemoryBackend()
        return config

    def test_local_readiness_does_not_require_publishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            self.assertEqual(["lexical"], query(config, text="maintenancecanary").results[0]["retrieval"])
            with patch("mindie_knowledge.publishing.run_once", return_value={"status": "disabled"}) as shared:
                result = maintain(config, verify=True)
            self.assertTrue(result["ready"], result)
            shared.assert_called_once_with(config, force=False, verify=True)
            self.assertEqual(1, len(query(config, text="maintenancecanary").results))

    def test_loss_is_repaired_and_source_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            original = (root / "notes" / "fact.md").read_bytes()
            with patch("mindie_knowledge.publishing.run_once", return_value={"status": "disabled"}):
                maintain(config, verify=True)
                config.retrieval.documents.clear()
                repaired = maintain(config, verify=True)
            self.assertTrue(repaired["ready"], repaired)
            self.assertEqual(1, len(query(config, text="maintenancecanary").results))
            self.assertEqual(original, (root / "notes" / "fact.md").read_bytes())

    def test_down_engine_is_pending_and_retried_without_query_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            config.retrieval = UnavailableBackend("offline")
            self.assertFalse(maintain(config, verify=True)["ready"])
            with patch.object(config.retrieval, "available", side_effect=AssertionError("started inline")):
                self.assertTrue(query(config, text="maintenancecanary").unavailable)
            config.retrieval = MemoryBackend()
            with patch("mindie_knowledge.publishing.run_once", return_value={"status": "disabled"}):
                self.assertTrue(maintain(config, verify=True)["ready"])

    def test_parallel_connection_does_not_run_a_second_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            lock = SwitchLock(config.state_root / "maintenance.lock")
            lock.acquire()
            try:
                with patch("mindie_knowledge.maintenance.reconcile_markdown") as reconcile:
                    self.assertEqual("busy", maintain(config)["status"])
                    reconcile.assert_not_called()
            finally:
                lock.release()

    def test_failed_shared_sync_keeps_local_retrieval_and_marks_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            with patch("mindie_knowledge.publishing.run_once", return_value={"status": "partial", "sync": {"status": "offline"}}):
                result = maintain(config, verify=True)
            self.assertFalse(result["ready"])
            payload = query(config, text="maintenancecanary")
            self.assertTrue(payload.degraded)
            self.assertFalse(payload.unavailable)
            self.assertEqual(1, len(payload.results))

    def test_new_shared_model_rebuilds_local_vectors_before_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            model = {"revision": "before"}
            config.retrieval.index_fingerprint = lambda: dict(model)
            def import_new_model(*args, **kwargs):
                model["revision"] = "after"
                return {"status": "ok", "sync": {"status": "switched"}}
            with patch("mindie_knowledge.publishing.run_once", side_effect=import_new_model), \
                    patch.object(config.retrieval, "upsert", wraps=config.retrieval.upsert) as writes:
                result = maintain(config, verify=True)
            self.assertTrue(result["ready"], result)
            self.assertEqual(2, writes.call_count)
            ledger = json.loads((config.state_root / "markdown-index.json").read_text(encoding="utf-8"))
            self.assertEqual({"revision": "after"}, next(iter(ledger["documents"].values()))["index_fingerprint"])

    def test_worker_runs_even_when_contributions_are_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            with patch("mindie_knowledge.maintenance.maintain") as tick:
                worker = MaintenanceWorker(config)
                worker.start()
                worker.stop()
            tick.assert_called()
            self.assertEqual({"force": False}, tick.call_args.kwargs)

    def test_new_connection_reuses_fresh_preparation_and_event_still_forces_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            self.assertTrue(maintain(config, verify=True)["ready"])
            worker = MaintenanceWorker(config)
            with patch.object(worker.wakeup, "wait", side_effect=lambda _: worker.closed.set()), \
                    patch("mindie_knowledge.maintenance.reconcile_markdown") as reconcile:
                worker._run()
                reconcile.assert_not_called()
            worker.closed.clear()
            worker.request()
            with patch.object(worker.wakeup, "wait", side_effect=lambda _: worker.closed.set()), \
                    patch("mindie_knowledge.maintenance.maintain", wraps=maintain) as tick:
                worker._run()
            tick.assert_called_once_with(config, force=True)

    def test_query_without_readiness_record_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            self.assertTrue(query(config, text="maintenancecanary").degraded)

    def test_new_connection_reuses_fresh_audit_but_expiry_repairs_index_loss(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            with patch("mindie_knowledge.publishing.run_once", return_value={"status": "disabled"}):
                ready = maintain(config, verify=True)
                self.assertTrue(ready["ready"])
                config.retrieval.documents.clear()
                worker = MaintenanceWorker(config)
                completed = threading.Event()
                def first_pass(*args, **kwargs):
                    result = maintain(*args, **kwargs)
                    worker.closed.set()
                    completed.set()
                    return result
                with patch("mindie_knowledge.maintenance.maintain", side_effect=first_pass):
                    worker.start()
                    self.assertTrue(completed.wait(3))
                    worker.stop()
                # Reconnection is no longer a forced full audit. The fresh
                # receipt remains reusable until its explicit audit deadline.
                self.assertEqual({}, config.retrieval.documents)
                self.assertEqual(["lexical"], query(config, text="maintenancecanary").results[0]["retrieval"])
                receipt = json.loads((config.state_root / "maintenance.json").read_text())
                with patch("mindie_knowledge.maintenance.time.time", return_value=receipt["next_verify"] + 1):
                    self.assertTrue(maintain(config)["ready"])
                self.assertEqual(1, len(query(config, text="maintenancecanary").results))

    def test_new_connection_rechecks_index_loss_when_shared_verification_is_due(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            with patch("mindie_knowledge.publishing.run_once", return_value={"status": "disabled"}):
                ready = maintain(config, verify=True)
                self.assertTrue(ready["ready"])
                config.retrieval.documents.clear()
                worker = MaintenanceWorker(config)
                with patch.object(worker.wakeup, "wait", side_effect=lambda _: worker.closed.set()), \
                        patch("mindie_knowledge.maintenance.time.time", return_value=ready["next_verify"] + 1):
                    worker._run()
                self.assertEqual(1, len(query(config, text="maintenancecanary").results))

    def test_fresh_deadlines_skip_backend_work_and_expired_audit_overrides_next_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            with patch("mindie_knowledge.publishing.run_once", return_value={"status": "disabled"}):
                result = maintain(config, verify=True)
                with patch("mindie_knowledge.maintenance.backend_for_config", side_effect=AssertionError("unneeded backend")):
                    self.assertEqual(result, maintain(config))
                # Even an inconsistent/future next_check cannot postpone an
                # already due explicit audit of the saved vectors.
                result["next_check"] = result["next_verify"] + 100
                (config.state_root / "maintenance.json").write_text(json.dumps(result))
                config.retrieval.documents.clear()
                with patch("mindie_knowledge.maintenance.time.time", return_value=result["next_verify"] + 1):
                    repaired = maintain(config)
                self.assertTrue(repaired["ready"])
                self.assertEqual(1, len(query(config, text="maintenancecanary").results))

    def test_project_prepare_preserves_config_and_does_not_enable_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = project_config(root)
            self.assertFalse(config.publishing["enabled"])
            self.assertTrue(config.shared_sync["enabled"])
            self.assertEqual(((root / ".agents" / "knowledge").resolve(),), config.mount("project").roots)
            payload = json.loads(config.config_path.read_text(encoding="utf-8"))
            payload["publishing"] = {"enabled": True, "repository": "existing/repo", "fork": "existing/fork"}
            config.config_path.write_text(json.dumps(payload), encoding="utf-8")
            again = project_config(root)
            self.assertEqual(payload["publishing"], again.publishing)

    def test_prepare_cli_emits_readiness_without_starting_on_help(self):
        with patch("mindie_knowledge.maintenance.maintain") as work, redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["--help"])
            work.assert_not_called()
        with tempfile.TemporaryDirectory() as tmp:
            for ready in (False, True):
                output = io.StringIO()
                with patch("mindie_knowledge.maintenance.maintain", return_value={"ready": ready, "status": "ready" if ready else "pending"}), redirect_stdout(output):
                    rc = main(["--project", tmp])
                self.assertEqual(0 if ready else 1, rc)
                self.assertEqual(ready, json.loads(output.getvalue())["ready"])
