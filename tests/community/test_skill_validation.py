"""Deterministic Skill package validation (no model, no dispatch)."""

import pytest

from mindie_knowledge.community.common import CommunityError
from mindie_knowledge.community.skill_validation import (
    check_skill_package_path,
    validate_openai_yaml,
    validate_reference_files,
    validate_skill_markdown,
)

from .conftest import make_entry

GOOD_SKILL_MD = """---
name: npu-device-numbering
description: Map physical NPU devices to container-logical numbering.
metadata:
  mindie_source_entries: "ENTRYID"
---

# Method

Check the physical mapping first, then number logically from zero.
Reference entry ENTRYID for the full case history.
"""


def test_validate_skill_markdown_roundtrip():
    doc = make_entry()
    text = GOOD_SKILL_MD.replace("ENTRYID", doc["entry_id"])
    meta = validate_skill_markdown(text)
    assert meta["name"] == "npu-device-numbering"
    assert meta["source_entries"] == [doc["entry_id"]]
    with pytest.raises(ValueError, match="absolute paths"):
        validate_skill_markdown(text + "\nSee /Users/alice/secrets for more.\n")
    with pytest.raises(ValueError, match="frontmatter"):
        validate_skill_markdown("# no frontmatter\n")


def test_openai_yaml_nested_policy_required():
    validate_openai_yaml("policy:\n  allow_implicit_invocation: false\n")
    for bad in ("allow_implicit_invocation: false\n",
                "interface:\n  display_name: Test\n",
                "policy:\n  allow_implicit_invocation: true\n"):
        with pytest.raises(ValueError):
            validate_openai_yaml(bad)


def test_package_paths_scoped_to_prefix():
    prefix = "plugins/mindie-agent/skills"
    check_skill_package_path(f"{prefix}/slug-a/SKILL.md", prefix)
    check_skill_package_path(f"{prefix}/slug-a/agents/openai.yaml", prefix)
    check_skill_package_path(f"{prefix}/slug-a/references/case.md", prefix)
    for bad in ("skills/slug-a/SKILL.md", f"{prefix}/slug-a/run.py",
                f"{prefix}/slug-a/hooks/hook.sh", f"{prefix}/SKILL.md",
                ".github/workflows/x.yml"):
        with pytest.raises(CommunityError):
            check_skill_package_path(bad, prefix)


def test_reference_files_bounded():
    assert validate_reference_files({"case.md": "detail"}) == {"case.md": "detail"}
    with pytest.raises(CommunityError):
        validate_reference_files({"../escape.md": "x"})
    with pytest.raises(CommunityError):
        validate_reference_files({"x.md": "x" * (64 * 1024 + 1)})
