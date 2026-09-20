"""Deterministic Skill package validation — no model, no dispatch, no PRs.

Reusable checks for the *external* Grok Bot software's optional Skill
proposals and for maintainers reviewing them. This module only validates
bytes against the native package rules; it never generates content, never
calls a model, and never touches a repository. The deferred
ascend-profiling-analysis Skill is never inspected by this package.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Mapping

import yaml

from .common import CommunityError

SLUG_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")
MAX_SKILL_MD_BYTES = 64 * 1024
MAX_REFERENCE_BYTES = 64 * 1024
MAX_REFERENCES = 8
DEFAULT_SKILL_PREFIX = "plugins/mindie-agent/skills"
#: Frontmatter metadata key carrying comma-joined source entry ids (string
#: value, per the supported SKILL.md metadata mapping).
SOURCE_METADATA_KEY = "mindie_source_entries"


def check_skill_package_path(name: str, prefix: str = DEFAULT_SKILL_PREFIX) -> None:
    """Allowed Skill package paths: <prefix>/<slug>/{SKILL.md,
    agents/openai.yaml, references/*.md} — no executables, hooks, workflows."""
    pure = PurePosixPath(name)
    if (
        pure.is_absolute()
        or str(pure) != name
        or "\\" in name
        or any(ord(character) < 32 for character in name)
        or ".." in pure.parts
    ):
        raise CommunityError(f"{name!r}: Skill package path must be canonical and relative")
    parts = pure.parts
    prefix_parts = PurePosixPath(prefix).parts
    if parts[: len(prefix_parts)] != prefix_parts:
        raise CommunityError(f"{name}: Skill packages live under {prefix}/<slug>/ only")
    tail_parts = parts[len(prefix_parts):]
    if len(tail_parts) < 2 or not SLUG_RE.fullmatch(tail_parts[0]):
        raise CommunityError(f"{name}: Skill packages live under {prefix}/<slug>/ only")
    tail = tail_parts[-1]
    if tail == "SKILL.md" and len(tail_parts) == 2:
        return
    if tail == "openai.yaml" and tail_parts[-2] == "agents" and len(tail_parts) == 3:
        return
    if tail_parts[-2] == "references" and tail.endswith(".md") and len(tail_parts) == 3:
        return
    raise CommunityError(
        f"{name}: not an allowed Skill package path (SKILL.md, agents/openai.yaml, references/*.md)"
    )


def validate_skill_markdown(text: str) -> dict[str, Any]:
    """Strict SKILL.md shape: name/description frontmatter, metadata refs."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("SKILL.md must be nonempty")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized.encode("utf-8")) > MAX_SKILL_MD_BYTES:
        raise ValueError("SKILL.md exceeds the size limit")
    if not normalized.startswith("---\n"):
        raise ValueError("SKILL.md must start with a YAML frontmatter block")
    closing = normalized.find("\n---\n", 4)
    if closing == -1:
        raise ValueError("SKILL.md frontmatter is not closed")
    meta = yaml.safe_load(normalized[4:closing])
    if not isinstance(meta, dict):
        raise ValueError("SKILL.md frontmatter must be a mapping")
    name = meta.get("name")
    if not isinstance(name, str) or not SLUG_RE.fullmatch(name.strip()):
        raise ValueError("SKILL.md frontmatter needs a slug-shaped name")
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("SKILL.md frontmatter needs a description")
    if len(description) > 1024:
        raise ValueError("SKILL.md description is too long")
    metadata = meta.get("metadata") or {}
    if not isinstance(metadata, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()
    ):
        raise ValueError("frontmatter metadata must map strings to strings")
    raw_refs = metadata.get(SOURCE_METADATA_KEY, "")
    refs = [r.strip() for r in raw_refs.split(",") if r.strip()]
    body = normalized[closing + 5 :].strip()
    if not body:
        raise ValueError("SKILL.md body must be nonempty")
    if re.search(r"/(?:home|Users)/[^\s]+", body):
        raise ValueError("SKILL.md must not embed author absolute paths")
    return {"name": name.strip(), "description": description.strip(),
            "source_entries": refs, "body": body}


def validate_openai_yaml(text: str) -> None:
    """The native agents/openai.yaml policy: implicit invocation explicitly off.

    The harness default is TRUE when the policy is absent, so a missing or
    misplaced key silently yields an implicit Skill — reject both.
    """
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("agents/openai.yaml must be a mapping")
    policy = data.get("policy")
    if not isinstance(policy, dict) or policy.get("allow_implicit_invocation") is not False:
        raise ValueError(
            "agents/openai.yaml must set policy.allow_implicit_invocation: false explicitly"
        )


def validate_reference_files(references: Mapping[str, Any]) -> dict[str, str]:
    """Bounded references mapping: markdown names only, capped size/count."""
    if not isinstance(references, Mapping) or len(references) > MAX_REFERENCES:
        raise CommunityError("references must be a mapping of at most 8 files")
    checked: dict[str, str] = {}
    for name, content in references.items():
        pure = PurePosixPath(str(name))
        if (
            not isinstance(name, str)
            or str(pure) != name
            or len(pure.parts) != 1
            or "\\" in name
            or any(ord(character) < 32 for character in name)
            or not name.endswith(".md")
        ):
            raise CommunityError(f"reference name {name!r} is not an allowed markdown file")
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_REFERENCE_BYTES:
            raise CommunityError(f"reference {name!r} exceeds the size limit")
        checked[pure.name] = content
    return checked
