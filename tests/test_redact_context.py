"""r2 false-positive relaxations: MindIE refs and Python numeric slices."""

from __future__ import annotations

import unittest

from test_tools_support import synthetic_ipv4, synthetic_ipv6

from mindie_knowledge import redact
from mindie_knowledge.loop.transcript_redaction import redact as public_redact, install_scanner


def rules_hit(text: str) -> set[str]:
    return {f.rule for f in redact.scan_text(text)}


PUBLIC_REF = "mindie://vllm-ascend/c3f998fdce9eecfb@937d2219f062ff5b"
HEX16_A = "c3f998fdce9eecfb"
HEX16_B = "937d2219f062ff5b"
HEX64_A = "a" * 64
HEX64_B = "b" * 64


class PinnedSourceUrlEntropyTests(unittest.TestCase):
    def test_reviewed_skill_and_gitcode_sources_keep_path_boundaries(self):
        urls = (
            'https://github.com/obra/superpowers/blob/b7a8f76985f1e93e75dd2f2a3b424dc731bd9d37/skills/requesting-code-review/SKILL.md#L8-L22',
            'https://gitcode.com/Ascend/msprof-analyze/blob/639eb8702c1884813dfab59ddb371f7838caab6d/msprof_analyze/cluster_analyse/operator_mfu.py#L41-L170',
            'https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/920beta2/maintenref/troubleshooting/docs/zh/error_codes/EI0002-Communication_Error_Timeout.md',
            'https://gitcode.com/cann/ops-transformer/blob/master/attention/fused_infer_attention_score/docs/aclnnFusedInferAttentionScoreV5.md',
        )
        for url in urls:
            self.assertNotIn('credential-high-entropy', rules_hit(url))
            self.assertNotIn('credential-high-entropy', rules_hit('[source](' + url + ')'))

    def test_graded_metadata_locators_and_public_ascend_symbols_remain_clean(self):
        from mindie_knowledge.graded.privacy import metadata_findings

        meta = {
            "path": "0day/Qwen3_5/unit-vllm-ascend-gdn-idle-dp-zero-pad-workspace-mutator",
            "id_path": {
                "unit-vector-reduction-fp32-accumulator-and-ub-alignment-sop":
                "Reduction/unit-vector-reduction-fp32-accumulator-and-ub-alignment-sop",
            },
            "conditions": {"qualification_gates": ["G3_contract_c4_sequential_ablation"]},
            "provenance": {
                "source_locator": "https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/80RC3alpha003/opdevg/Ascendcopdevg/atlas_ascendc_10_0001.html",
                "evidence_refs": [
                    "cannbot-skills@1fa2cbbc9fdddb4377ca26734e9b94d91c42b975:tools/asys-toolkit/SKILL.md#L71-L107",
                    "mindie-003-execution@9204f6be8c973adc88c0238456ec60872cc18c072866c85d6c4ad9bbbb797dbd:agy-dsh-waves/wave7/inputs/offline_transcript_digest.md",
                    "knowledge-operators@worktree-snapshot-20261021:Reduction/unit-vectorized-bf16-fp32-accumulation-cast-fusion-mutator.md",
                ],
            },
        }
        self.assertEqual(metadata_findings(meta, "index/routing-manifest.json"), [])
        self.assertNotIn("credential-high-entropy", rules_hit("aclnnFusedInferAttentionScoreV2+ and aclnnFusedInferAttentionScoreV2/V5"))

    def test_source_url_does_not_hide_secret_segments_parameters_or_userinfo(self):
        base = 'https://github.com/obra/superpowers/blob/' + 'b7a8f76985f1e93e75dd2f2a3b424dc731bd9d37/'
        secret = 'SyntheticOnlySecretValue9876543210ABCxyz'
        for value in (base + secret + '/SKILL.md', base + 'SKILL.md?token=' + secret,
                      base.replace('https://', 'https://user:password@')):
            self.assertTrue(any(rule.startswith('credential-') for rule in rules_hit(value)), value)
        self.assertIn('credential-high-entropy', rules_hit(base.replace('/blob/', '/tree/') + 'SKILL.md'))


class MindieRefUsernameAtHostTests(unittest.TestCase):
    def test_complete_tokens_are_not_username_at_host(self):
        tokens = (
            f"mindie://vllm-ascend/{HEX16_A}@{HEX16_B}",
            f"mindie://vllm-ascend/{HEX64_A}@{HEX64_B}",
            f"mindie://vllm-ascend/{HEX16_A}@{HEX64_B}",
            f"mindie://vllm-ascend/{HEX64_A}@{HEX16_B}",
            PUBLIC_REF,
        )
        wrappers = (
            "{}",
            "'{}'",
            '"{}"',
            "`{}`",
            "[ref]({})",
            "<{}>",
            "see {}.",
        )
        for token in tokens:
            for wrap in wrappers:
                text = wrap.format(token)
                self.assertNotIn("username-at-host", rules_hit(text), text)

    def test_malformed_and_non_ref_still_detected(self):
        good = f"mindie://vllm-ascend/{HEX16_A}@{HEX16_B}"
        still_host = (
            f"mindie://Vllm-ascend/{HEX16_A}@{HEX16_B}",
            f"mindie://vllm_ascend/{HEX16_A}@{HEX16_B}",
            f"mindie://vllm-ascend/{HEX16_A[:15]}@{HEX16_B}",
            f"mindie://vllm-ascend/{HEX16_A}@{HEX16_B[:15]}",
            f"x{good}",
            f"evil-{good}",
            f"_{good}",
            f"https://example.com/{good}",
            f"{good}/extra",
            f"{good}?x=1",
            f"{good}#tag",
            f"{good}:22",
            f"{good}.suffix",
            f"{good}/path",
            f"{good}?q=1",
            f"{good}#frag",
            f"{good}:8080",
            f"{good}suffix",
            f"{HEX16_A}@{HEX16_B}",
            "ssh exampleuser@npu07",
        )
        for text in still_host:
            hit = rules_hit(text)
            self.assertTrue(
                {"username-at-host", "email-address"} & hit,
                f"{text!r} -> {hit}",
            )
        self.assertIn(
            "credential-url-userinfo",
            rules_hit("https://" + "u:p" + "@" + "example.com/x"),
        )


class PythonSliceIpv6Tests(unittest.TestCase):
    def test_attached_numeric_slices_skip_ipv6(self):
        clean = (
            "x[:, ::2]",
            "w[::2]",
            "x[1::2]",
            "x[1:10:2]",
            "module.array[:, ::2]",
            'torch.randn(4096, dtype=dtype, device="npu:0")[::2]',
            "arr[..., ::2]",
            "y[-1::2]",
        )
        for text in clean:
            self.assertNotIn("ipv6-address", rules_hit(text), text)

    def test_ambiguous_and_real_ipv6_still_detected(self):
        addr = synthetic_ipv6()
        still = (
            "plain ::2 in prose",
            "[::2]",
            'endpoint="[::2]"',
            "ssh user@[::2]",
            "http://[::2]:8080",
            "host [::2]",
            "取[::2]",
            f"```python\naddr = \"{addr}\"\n```",
            f"bind = {addr}",
        )
        for text in still:
            self.assertIn("ipv6-address", rules_hit(text), text)


class FindingOffsetsAndMaskTests(unittest.TestCase):
    def test_constructor_without_offsets_remains_valid(self):
        finding = redact.Finding(path="<text>", rule="ipv6-address", value="::2", hint="x")
        self.assertIsNone(finding.start)
        self.assertIsNone(finding.end)

    def test_scan_text_assigns_original_offsets(self):
        text = "取 w[::2] 与 http://[::2]:8080"
        slice_value = "::2"
        first = text.find(slice_value)
        second = text.find(slice_value, first + 1)
        self.assertNotEqual(first, -1)
        self.assertNotEqual(second, -1)
        self.assertNotEqual(first, second)
        findings = [f for f in redact.scan_text(text) if f.rule == "ipv6-address"]
        self.assertEqual(len(findings), 1)
        hit = findings[0]
        self.assertEqual(hit.value, slice_value)
        self.assertEqual(hit.start, second)
        self.assertEqual(hit.end, second + len(slice_value))
        self.assertEqual(text[hit.start:hit.end], slice_value)
        self.assertNotEqual(hit.start, first)

    def test_mask_text_replaces_only_protected_span(self):
        text = "取 w[::2] 与 http://[::2]:8080"
        masked, rules = public_redact(text, executable=install_scanner(), key=b"synthetic-redaction-test-key")
        self.assertIn("ipv6-address", rules)
        self.assertIn("w[::2]", masked)
        self.assertNotIn("http://[::2]:8080", masked)
        self.assertIn("<redacted:ipv6-address:", masked)
        self.assertTrue(masked.startswith("取 "))


class PackageVersionIpv4Tests(unittest.TestCase):
    def test_observed_firmware_version_kept_address_still_masked(self):
        # Captured by npu-smi during the independent five-stage A2 run.
        version = "7.7.0.10.220"
        address = synthetic_ipv4()
        text = f"Firmware Version: {version}; peer {address}."
        findings = [f for f in redact.scan_text(text) if f.rule == "ipv4-address"]
        self.assertEqual([f.value for f in findings], [address])
        masked, rules = public_redact(
            text, executable=install_scanner(), key=b"synthetic-redaction-test-key"
        )
        self.assertIn(f"Firmware Version: {version};", masked)
        self.assertNotIn(address, masked)
        self.assertIn("ipv4-address", rules)
        self.assertTrue(masked.endswith("."))

    def test_package_grammar_versions_survive(self):
        clean = (
            # Public pip-check shape: the four-part token is a version.
            "opencv-python-headless 5.0.0.93 requires numpy>=2; "
            'python_version >= "3.9", but you have numpy 1.26.4.',
            "prometheus-fastapi-instrumentator 8.0.2.1 has requirement "
            "starlette<2.0.0,>=1.0.0, but you have starlette 0.50.0.",
        )
        for text in clean:
            self.assertNotIn("ipv4-address", rules_hit(text), text)

    def test_masquerade_and_ambiguous_forms_stay_detected(self):
        addr = synthetic_ipv4()
        still = (
            # An endpoint word is not a distribution name.
            "host 5.0.0.93 requires numpy>=2",
            # The same digits outside package grammar are still an address.
            "ping 5.0.0.93",
            f"http://{addr}/",
            f"ssh user@{addr}",
            f"subnet {addr}/24",
            f"range {synthetic_ipv4(1)}-{synthetic_ipv4(9)}",
            f"listen {addr}:8080",
            f"host: {addr}",
            # Requirement-pin and URL/path text is not pip-check grammar.
            "pkg==5.0.0.93:80",
            "package==5.0.0.93",
            "package==5.0.0.93/path",
            "http://package==5.0.0.93/",
            "http://example.com/?package==5.0.0.93",
            # A CIDR continuation is not an exact version token.
            "pkg 5.0.0.93/24 requires numpy>=2",
            # Bare version-looking text stays ambiguous and reported.
            "driver 7.0.0.1",
        )
        for text in still:
            self.assertIn("ipv4-address", rules_hit(text), text)

    def test_same_digits_version_kept_network_use_masked(self):
        text = (
            "opencv-python-headless 5.0.0.93 requires numpy>=2, "
            "but you have numpy 1.26.4; also http://5.0.0.93/ answered"
        )
        version_at = text.find("5.0.0.93")
        host_at = text.find("5.0.0.93", version_at + 1)
        self.assertNotEqual(host_at, -1)
        findings = [f for f in redact.scan_text(text) if f.rule == "ipv4-address"]
        self.assertEqual(len(findings), 1)
        hit = findings[0]
        self.assertEqual(hit.value, "5.0.0.93")
        self.assertEqual(hit.start, host_at)
        self.assertEqual(hit.end, host_at + len("5.0.0.93"))
        self.assertEqual(text[hit.start:hit.end], "5.0.0.93")
        masked, rules = public_redact(text, executable=install_scanner(), key=b"synthetic-redaction-test-key")
        self.assertIn("ipv4-address", rules)
        self.assertIn("opencv-python-headless 5.0.0.93 requires", masked)
        self.assertNotIn("http://5.0.0.93/", masked)
        self.assertIn("<redacted:ipv4-address:", masked)


if __name__ == "__main__":
    unittest.main()
