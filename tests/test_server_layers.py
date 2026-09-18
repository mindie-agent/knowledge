"""Mount configuration and labelling for the three layers.

The property under test is that absence is a reported state, not an error and
not a silent empty set. A caller that cannot tell "this layer was not mounted"
from "this layer had nothing to say" will read a gap as a fact.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures" / "server"))

import support  # noqa: E402
from mindie_knowledge.corpus import corpus_root  # noqa: E402
from unittest import mock
from mindie_knowledge.server.layers import (  # noqa: E402
    LAYERS,
    SOURCE_REPO,
    ConfigError,
    default_shared_roots,
    load_config,
    resolve_shared_from_corpus,
    shared_source,
)


class LayerMounting(unittest.TestCase):
    def test_all_three_layers_mount_and_are_labelled(self):
        config = support.build_config()
        self.assertEqual(["shared", "project", "candidate"], config.available_layers())
        self.assertEqual({}, config.absent_layers())
        self.assertFalse(config.describe()["degraded"])

    def test_shared_is_read_only_even_if_configuration_asks_otherwise(self):
        # Shared reference material is read-only through the capture service.
        config = load_config(
            {"layers": {"shared": {"roots": ["shared"], "read_only": False}}},
            env={},
            base_dir=support.FIXTURES,
        )
        self.assertTrue(config.mount("shared").read_only)
        self.assertFalse(config.mount("candidate").read_only)

    def test_unconfigured_project_layer_is_absent_not_an_error(self):
        config = load_config({"layers": {"shared": {"roots": ["shared"]}}}, env={}, base_dir=support.FIXTURES)
        self.assertEqual("shared", config.available_layers()[0])
        self.assertIn("project", config.absent_layers())
        self.assertIn("not configured", config.absent_layers()["project"])

    def test_missing_path_is_absent_with_the_path_in_the_reason(self):
        config = support.build_config(shared="missing")
        self.assertNotIn("shared", config.available_layers())
        self.assertIn("does not exist", config.absent_layers()["shared"])
        self.assertTrue(config.mount("shared").configured)

    def test_missing_candidate_root_is_an_empty_present_layer(self):
        config = support.build_config(candidate="missing")
        mount = config.mount("candidate")
        self.assertTrue(mount.present)
        self.assertTrue(mount.configured)
        self.assertNotIn("candidate", config.absent_layers())
        self.assertFalse(config.degraded(["candidate"]))

    def test_unreadable_candidate_root_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = pathlib.Path(tmp) / "not-a-directory"
            blocker.write_text("nope\n", encoding="utf-8")
            config = support.build_config(candidate=blocker)
            self.assertFalse(config.mount("candidate").present)
            self.assertIn("not a directory", config.absent_layers()["candidate"])

    def test_no_shared_cache_still_yields_the_local_layers(self):
        config = support.build_config(shared="missing")
        self.assertEqual(["project", "candidate"], config.available_layers())
        self.assertIn("shared", config.absent_layers())

    def test_disabled_layer_reports_why(self):
        config = support.build_config(candidate=False)
        self.assertEqual("disabled in configuration", config.absent_layers()["candidate"])

    def test_service_config_describe_lists_every_layer(self):
        described = support.build_config().describe()
        self.assertEqual(set(LAYERS), set(described["layers"]))
        self.assertNotIn("policy", described)
        self.assertEqual(SOURCE_REPO, described["source_repo"])
        self.assertIn("source_ref", described)


class EnvironmentOverrides(unittest.TestCase):
    def test_explicit_state_overrides_discovered_workspace_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary).resolve()
            path = root / ".mindie-local" / "knowledge" / "service.json"
            path.parent.mkdir(parents=True)
            configured_state = root / "configured-instance"
            explicit_state = root / "attached-task" / "instance"
            path.write_text(json.dumps({"state_root": str(configured_state)}), encoding="utf-8")
            with mock.patch("mindie_knowledge.server.layers.Path.cwd", return_value=root):
                configured = load_config(env={})
                overridden = load_config(env={"MINDIE_KNOWLEDGE_STATE": str(explicit_state)})
            self.assertEqual(path, configured.config_path)
            self.assertEqual(path, overridden.config_path)
            self.assertEqual(configured_state, configured.state_root)
            self.assertEqual(explicit_state, overridden.state_root)

    def test_env_roots_override_the_config_file(self):
        config = load_config(
            {"layers": {"project": {"roots": ["no-such-directory"]}}},
            env={"MINDIE_KNOWLEDGE_PROJECT_ROOTS": str(support.FIXTURES / "project")},
            base_dir=support.FIXTURES,
        )
        self.assertTrue(config.mount("project").present)

    def test_empty_env_value_disables_a_layer(self):
        config = support.build_config(env={"MINDIE_KNOWLEDGE_SHARED_ROOTS": ""})
        self.assertNotIn("shared", config.available_layers())
        self.assertIn("disabled by MINDIE_KNOWLEDGE_SHARED_ROOTS", config.absent_layers()["shared"])

    def test_layer_allowlist_env_narrows_the_mount_set(self):
        config = support.build_config(env={"MINDIE_KNOWLEDGE_LAYERS": "shared"})
        self.assertEqual(["shared"], config.available_layers())
        self.assertIn("MINDIE_KNOWLEDGE_LAYERS", config.absent_layers()["project"])

    def test_identity_comes_from_the_environment(self):
        config = support.build_config(
            env={
                "MINDIE_KNOWLEDGE_CONTRIBUTOR": "example-handle",
                "MINDIE_KNOWLEDGE_ORIGIN_REPO": "example-org/example-repo",
            }
        )
        self.assertEqual("example-handle", config.identity["contributor"])
        self.assertEqual("example-org/example-repo", config.identity["origin_repo"])



class ConfigFiles(unittest.TestCase):
    def test_relative_environment_roots_without_config_use_client_cwd(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "notes").mkdir()
            with mock.patch("mindie_knowledge.server.layers.Path.cwd", return_value=root):
                config = load_config({}, env={"MINDIE_KNOWLEDGE_PROJECT_ROOTS": "notes",
                                              "MINDIE_KNOWLEDGE_CANDIDATE_ROOT": "candidate"})
        self.assertEqual((root / "notes",), config.mount("project").roots)
        self.assertTrue(config.mount("project").present)
        self.assertEqual((root / "candidate",), config.mount("candidate").roots)

    def test_relative_roots_resolve_against_the_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "mindie-knowledge.json"
            path.write_text(
                json.dumps(
                    {
                        "layers": {
                            "shared": {"roots": [str(support.FIXTURES / "shared")]},
                            "project": {"roots": ["project-knowledge"]},
                        }
                    }
                )
            )
            (pathlib.Path(tmp) / "project-knowledge").mkdir()
            config = load_config(path=path, env={})
            self.assertTrue(config.mount("shared").present)
            self.assertTrue(config.mount("project").present)
            self.assertEqual(path, config.config_path)

    def test_missing_config_file_warns_and_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(path=pathlib.Path(tmp) / "absent.json", env={})
            self.assertIsNone(config.config_path)
            self.assertTrue(any("config file not found" in w for w in config.warnings))

    def test_malformed_config_file_raises_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "mindie-knowledge.json"
            path.write_text("{not json")
            with self.assertRaises(ConfigError) as ctx:
                load_config(path=path, env={})
            self.assertIn("cannot read config", str(ctx.exception))

    def test_unknown_layer_in_config_is_a_warning_not_a_failure(self):
        config = load_config(
            {"layers": {"shared": {"roots": ["shared"]}, "archive": {"roots": ["shared"]}}},
            env={},
            base_dir=support.FIXTURES,
        )
        self.assertTrue(any("unknown layer" in w for w in config.warnings))
        self.assertTrue(config.mount("shared").present)


class PackagedCorpusDefault(unittest.TestCase):
    def test_default_shared_root_is_the_packaged_markdown_corpus(self):
        roots = default_shared_roots({})
        self.assertEqual((corpus_root(),), roots)
        self.assertEqual(
            tuple(p.resolve() for p in roots),
            tuple(p.resolve() for p in resolve_shared_from_corpus(corpus_root())),
        )

    def test_corpus_env_still_resolves_through_resolve_shared_from_corpus(self):
        roots = default_shared_roots({"MINDIE_KNOWLEDGE_CORPUS": str(support.REPO)})
        self.assertEqual(
            tuple(p.resolve() for p in roots),
            tuple(p.resolve() for p in resolve_shared_from_corpus(support.REPO)),
        )
        self.assertEqual((support.REPO / "corpus",), roots)

    def test_shared_source_names_the_commons_repo(self):
        source = shared_source()
        self.assertEqual(SOURCE_REPO, source["source_repo"])
        self.assertIn("source_ref", source)


class ReferenceConfiguration(unittest.TestCase):
    def test_old_review_policy_does_not_become_a_runtime_filter(self):
        config = load_config({"policy": {"default_statuses": ["verified"], "stale_after_days": 1},
                              "identity": {"redaction_profile": "old-profile"}},
                             env={"MINDIE_KNOWLEDGE_STALE_AFTER_DAYS": "soon", "MINDIE_KNOWLEDGE_REDACTION_PROFILE": "old-profile"})
        self.assertNotIn("policy", config.describe())
        self.assertNotIn("redaction_profile", config.identity)
        self.assertEqual([], config.warnings)

    def test_runtime_and_publishing_configuration_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            publishing = {"enabled": False, "repository": "example/corpus"}
            config = load_config({"backend": "memory", "state_root": "instance",
                                  "publishing": publishing}, env={}, base_dir=base)
        self.assertEqual("memory", config.backend)
        self.assertEqual(base / "instance", config.state_root)
        self.assertEqual(publishing, config.publishing)

    def test_json_configuration_does_not_need_yaml(self):
        with mock.patch("mindie_knowledge.server.layers.yaml", None):
            config = load_config({}, env={})
        self.assertEqual([], config.warnings)

    def test_explicit_yaml_configuration_reports_missing_parser(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "service.yaml"
            path.write_text("backend: memory\n", encoding="utf-8")
            with mock.patch("mindie_knowledge.server.layers.yaml", None):
                with self.assertRaisesRegex(ConfigError, "PyYAML is required"):
                    load_config(path=path, env={})


if __name__ == "__main__":
    unittest.main()
