"""r2 false-positive relaxations: MindIE refs and Python numeric slices."""

from __future__ import annotations

import unittest

from test_tools_support import synthetic_ipv6

from mindie_knowledge import redact
from mindie_knowledge.loop.engine import mask_text


def rules_hit(text: str) -> set[str]:
    return {f.rule for f in redact.scan_text(text)}


PUBLIC_REF = "mindie://vllm-ascend/c3f998fdce9eecfb@937d2219f062ff5b"
HEX16_A = "c3f998fdce9eecfb"
HEX16_B = "937d2219f062ff5b"
HEX64_A = "a" * 64
HEX64_B = "b" * 64


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
        masked, rules = mask_text(text)
        self.assertIn("ipv6-address", rules)
        self.assertIn("w[::2]", masked)
        self.assertNotIn("http://[::2]:8080", masked)
        self.assertIn("[redacted:ipv6-address]", masked)
        self.assertTrue(masked.startswith("取 "))


if __name__ == "__main__":
    unittest.main()
