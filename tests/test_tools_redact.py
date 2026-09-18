"""tools/redact.py — ruleset r2, scanner and allowlist."""

from __future__ import annotations

import json
import re
import unittest

from test_tools_support import (
    EXAMPLE_ENTRY,
    FIXTURES,
    TempDir,
    calibration_sentence,
    run_tool,
    synthetic_container_command,
    synthetic_credential,
    synthetic_email,
    synthetic_employee_id,
    synthetic_hostname,
    synthetic_ipv4,
    synthetic_ipv4_range,
    synthetic_ipv6,
    synthetic_mac,
    synthetic_mac_path,
    synthetic_ticket,
    synthetic_user_path,
    pem_header,
    versionlike,
)

from mindie_knowledge import redact


def rules_hit(text: str, allow: redact.Allowlist | None = None) -> set[str]:
    return {f.rule for f in redact.scan_text(text, allow)}


class ProfileTests(unittest.TestCase):
    def test_profile_is_declared_once_and_matches_schema_pattern(self):
        self.assertRegex(redact.REDACTION_PROFILE, r"^r[0-9]+$")
        self.assertEqual(redact.REDACTION_PROFILE, "r2")
        proc = run_tool("redact", "--profile")
        self.assertEqual(proc.stdout.strip(), "r2")

    def test_rule_ids_unique(self):
        self.assertEqual(len(redact.RULE_IDS), len(set(redact.RULE_IDS)))


class DetectionTests(unittest.TestCase):
    def test_calibration_sentence_from_v1_corpus_is_caught(self):
        findings = redact.scan_text(calibration_sentence())
        self.assertTrue(findings, "the v1 applicability leak class must be detected")
        self.assertEqual({f.rule for f in findings}, {"ipv4-address"})
        self.assertEqual(findings[0].value, synthetic_ipv4_range())

    def test_ipv4_forms(self):
        self.assertIn("ipv4-address", rules_hit(synthetic_ipv4()))
        self.assertIn("ipv4-address", rules_hit("range " + synthetic_ipv4(1) + "-" + synthetic_ipv4(9)))
        self.assertIn("ipv4-address", rules_hit("subnet " + synthetic_ipv4(0) + "/24"))

    def test_ipv6_and_mac(self):
        self.assertIn("ipv6-address", rules_hit("bind to " + synthetic_ipv6()))
        self.assertIn("mac-address", rules_hit("nic " + synthetic_mac()))

    def test_hostnames(self):
        self.assertIn("hostname-internal-domain", rules_hit("on " + synthetic_hostname()))
        self.assertIn("hostname-numbered", rules_hit("ran on node-01 and worker3"))
        self.assertIn("hostname-assignment", rules_hit("host: " + "npu" + "-" + "rack2"))

    def test_hostname_numbered_does_not_match_prefix_word_number_prose(self):
        clean = (
            "worker-pool-size-32",
            "compute-bound-step-3",
            "master-branch-2026",
            "node-count-8",
            "remote-code-parity-sync-27a5373f7a",
        )
        for text in clean:
            self.assertEqual(rules_hit(text), set(), text)
        self.assertEqual(rules_hit("gpu-node-01"), set())

    def test_hostname_numbered_still_matches_prefix_plus_digits(self):
        for text in (
            "node-01",
            "worker3",
            "remote_131",
            "remote-131",
            "k8s-3",
            "master01",
            "remote_131_cann_9",
            "snapshot remote_000_cann_9_0_0_2026-06-02",
        ):
            self.assertIn("hostname-numbered", rules_hit(text), text)

    def test_internal_machine_identifiers(self):
        # r2: the token shapes that carried a machine slot in the hardware
        # measurement source files. The numbers are synthetic (000).
        self.assertIn("hostname-numbered", rules_hit("snapshot remote_000_cann_9_0_0_2026-06-02"))
        self.assertIn("internal-machine-identifier", rules_hit("Single-card 910B4 on remote 000, NPU 4"))
        self.assertIn("internal-machine-identifier", rules_hit("source microbenchmark_000_npu4_2026-06-03"))
        findings = redact.scan_text("source microbenchmark_000_npu4_2026-06-03")
        self.assertEqual([f.value for f in findings], ["000"], "the device index is method, not identity")

    def test_method_detail_survives_machine_identifier_rule(self):
        # What makes a measurement checkable must not be reported: device
        # model, device index, software versions, workload shape, timing API.
        clean = (
            "single-card 910B4, NPU index 4, torch_npu 2.10.0, 8192x8192 dense matmul "
            "timed with torch.npu.Event; vendor platform_config snapshot, CANN 9.0.0; "
            "node 4 of 8; ascend-microbenchmark:2026-06-03/npu4; remote control"
        )
        self.assertEqual(rules_hit(clean), set())

    def test_user_paths(self):
        self.assertIn("user-path", rules_hit("see " + synthetic_user_path()))
        self.assertIn("user-path", rules_hit("see " + synthetic_mac_path()))
        self.assertIn("user-path", rules_hit("key in /root/" + ".ssh/id_ed25519"))
        self.assertIn("user-path-windows", rules_hit("C:\\Users\\" + "exampleuser" + "\\code"))
        self.assertIn("user-tilde-path", rules_hit("cd ~" + "exampleuser" + "/work"))

    def test_user_path_placeholders_are_allowed(self):
        self.assertEqual(rules_hit("/home/<user>/work and /Users/<user>/code and /root/ alone"), set())
        self.assertEqual(rules_hit("/home/$USER/work and /Users/${USER}/code and ~/x"), set())

    def test_people(self):
        self.assertIn("email-address", rules_hit("mail " + synthetic_email()))
        self.assertIn("username-at-host", rules_hit("ssh " + "exampleuser" + "@" + "npu" + "07"))
        self.assertIn("username-assignment", rules_hit("user=" + "exampleuser"))
        self.assertIn("username-flag", rules_hit("--user " + "exampleuser"))
        self.assertIn("employee-id", rules_hit("by " + synthetic_employee_id()))
        self.assertIn("ticket-id", rules_hit("tracked in " + synthetic_ticket()))

    def test_credentials(self):
        self.assertIn("credential-assignment", rules_hit(synthetic_credential()))
        self.assertIn("credential-known-format", rules_hit("ghp_" + "A" * 36))
        self.assertIn("credential-known-format", rules_hit(pem_header()))
        self.assertIn("credential-url-userinfo", rules_hit("https://" + "u:p" + "@" + "example.com/x"))
        self.assertIn("credential-bearer", rules_hit("Authorization: Bearer " + "abcdEFGH1234"))
        blob = "Zq9" * 12
        self.assertIn("credential-high-entropy", rules_hit("token " + blob))

    def test_containers(self):
        self.assertIn("container-command", rules_hit(synthetic_container_command()))
        self.assertIn("container-name-flag", rules_hit("--name " + "vaws_" + "sess_" + "12"))
        self.assertIn("container-name-assignment", rules_hit("container: " + "vaws-" + "a3-01"))
        self.assertEqual(rules_hit("docker exec -it <container> bash"), set())


class FalsePositiveGuardTests(unittest.TestCase):
    """Things that legitimately appear in entries and must not be flagged."""

    def test_example_entry_and_fixtures_are_clean(self):
        proc = run_tool("redact", "--check", str(EXAMPLE_ENTRY), str(FIXTURES))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_hashes_uuids_versions_and_topologies(self):
        clean = [
            "sha256:32d1e6611f47083c885205b4f4ef398ea238c0e7eaa60a3e3d961c49ce5b166a",
            "1416a279-1215-4adf-a978-82b40a3be0bc",
            "torch 2.5.1 torch_npu 2.5.1.post1 cann 8.0.RC1 driver 24.1.0 vllm 0.9.0",
            "tp2 tp4 tp8 tp16 dp4 ep16 npu0 /dev/davinci0",
            "std::vector at 12:30:45",
            "gloo makedeviceforhostname name or service not known hostname",
            "Map the container hostname to a loopback address in /etc/hosts",
            "SHA-256 UTF-8 RFC-1918 example-run-0000 ExampleSoC-A cpython-311-example",
            "commit 3f2a9c1e0b7d6a5c4e8f9a0b1c2d3e4f5a6b7c8d",
        ]
        for text in clean:
            self.assertEqual(rules_hit(text), set(), text)

    def test_public_by_construction_values_are_allowed(self):
        for text in (
            "127.0.0.1", "0.0.0.0", "::1", "localhost",
            "192.0.2.10", "198.51.100.7-9", "203.0.113.0/24",
            "2001:db8::1", "someone@example.com", "example.internal",
        ):
            self.assertEqual(rules_hit(text), set(), text)


class AllowlistTests(unittest.TestCase):
    def test_exact_and_regex_terms(self):
        version = versionlike("1")  # looks like an IPv4 address; a benign 4-part version
        self.assertIn("ipv4-address", rules_hit("driver " + version))
        allow = redact.Allowlist(terms=[version])
        self.assertEqual(rules_hit("driver " + version, allow), set())
        allow = redact.Allowlist(patterns=[r"7\.0\.0\.\d+"])
        self.assertEqual(rules_hit("driver " + versionlike("3"), allow), set())

    def test_allow_file(self):
        allow = redact.Allowlist.from_args(files=[str(FIXTURES / "allowlist.yaml")])
        self.assertEqual(rules_hit("driver " + versionlike("9"), allow), set())
        self.assertIn("ipv4-address", rules_hit("driver " + versionlike("9").replace("0.9", "1.9"), allow))

    def test_allowlist_does_not_hide_other_hits(self):
        allow = redact.Allowlist(terms=[versionlike("1")])
        text = "driver " + versionlike("1") + " on " + synthetic_ipv4()
        self.assertEqual(rules_hit(text, allow), {"ipv4-address"})

    def test_invalid_regex_is_a_clean_error(self):
        proc = run_tool("redact", "--allow", "re:(", str(EXAMPLE_ENTRY))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid regular expression", proc.stderr)


class TreeScanTests(unittest.TestCase):
    def test_ip_inside_rule_body_reported_with_path(self):
        doc = {"observations": [{"note": calibration_sentence()}]}
        findings = redact.scan_tree(doc)
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0].path, "observations[0].note")
        self.assertEqual(findings[0].rule, "ipv4-address")

    def test_keys_are_scanned_too(self):
        findings = redact.scan_tree({synthetic_ipv4(): "x"})
        self.assertEqual(len(findings), 1)
        self.assertIn("<key", findings[0].path)

    def test_nested_lists_and_non_strings(self):
        tree = {"a": [1, None, True, {"b": [synthetic_user_path()]}]}
        findings = redact.scan_tree(tree)
        self.assertEqual([f.path for f in findings], ["a[3].b[0]"])


class CliTests(unittest.TestCase):
    def _leaky_file(self, tmp):
        path = tmp / "leaky.md"
        path.write_text("# Observation\n\n" + calibration_sentence() +
                        " Logs live in " + synthetic_user_path() + ".\n", encoding="utf-8")
        return path

    def test_report_mode_exits_zero_check_mode_exits_one(self):
        with TempDir() as tmp:
            path = self._leaky_file(tmp)
            report = run_tool("redact", str(path))
            check = run_tool("redact", "--check", str(path))
        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertEqual(check.returncode, 1, check.stderr)
        self.assertIn("[ipv4-address]", check.stdout)
        self.assertIn("[user-path]", check.stdout)
        self.assertIn("<text>", check.stdout)

    def test_matches_are_masked_unless_requested(self):
        with TempDir() as tmp:
            path = self._leaky_file(tmp)
            masked = run_tool("redact", "--check", str(path))
            shown = run_tool("redact", "--check", "--show-matches", str(path))
        self.assertNotIn(synthetic_ipv4(), masked.stdout)
        self.assertNotIn(synthetic_user_path(), masked.stdout)
        self.assertRegex(masked.stdout, r"\(\d+ chars\)")
        self.assertIn(synthetic_ipv4_range(), shown.stdout)
        self.assertIn(synthetic_user_path(), shown.stdout)

    def test_json_output(self):
        with TempDir() as tmp:
            path = self._leaky_file(tmp)
            proc = run_tool("redact", "--check", "--format", "json", str(path))
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["redaction_profile"], "r2")
        self.assertEqual({f["rule"] for f in payload["findings"]}, {"ipv4-address", "user-path"})

    def test_clean_input_check_passes(self):
        proc = run_tool("redact", "--check", str(EXAMPLE_ENTRY))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stderr, re.compile(r"0 finding\(s\)"))


if __name__ == "__main__":
    unittest.main()
