"""[Role] Parse and validate C5 graded knowledge without changing its claims.

[Invariants] Stable unit IDs are independent of paths and content revisions.
Tier describes use; evidence strength and source provenance remain separate.
[Timeouts & Locks] Pure parsing and hashing; no I/O, locks or model calls.
[Side-Effect Policy] Reject invalid claims; never promote or rewrite evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any

import yaml

SCHEMA = "mindie-graded-unit/1"
UNIT_ID = re.compile(r"unit-[a-z0-9]+(?:-[a-z0-9]+)*\Z")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
TIER_CATEGORIES = {"L1": {"axiom", "measurement"}, "L2": {"sop"}, "L3": {"workflow"}, "L4": {"mutator"}}
STRENGTHS = {"hardware_spec", "reproduced_empirical", "single_observation", "hypothesis"}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def semantic_digest(metadata: dict, body: str) -> str:
    content = {key: value for key, value in metadata.items() if key not in {"domain_path", "revision", "content_digest"}}
    return hashlib.sha256(canonical([content, body]).encode()).hexdigest()


def _strings(value: Any, field: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value) or (nonempty and not value):
        raise ValueError(f"{field} requires {'nonempty ' if nonempty else ''}text list")
    return value


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value or path.as_posix() != value:
        raise ValueError("domain and document paths must be normalized repository-relative paths")
    return value


def is_graded_unit_path(path: str) -> bool:
    """One canonical <domain_path>/<unit_id>.md path, never private/reserved."""
    if not isinstance(path, str):
        return False
    pure = PurePosixPath(path)
    return bool(
        len(pure.parts) >= 2
        and pure.as_posix() == path
        and not pure.is_absolute()
        and all(part not in {".", ".."} and not part.startswith(".") for part in pure.parts)
        and pure.parts[0] not in {"tasks", "feedback", "index", "docs", "private"}
        and not any(ord(char) < 32 or char in '\\<>:"|?*' for char in path)
        and pure.suffix == ".md"
        and UNIT_ID.fullmatch(pure.stem)
    )


def locator_parts(value):
    """Recognize a C5 Git locator so redaction does not mistake it for SSH."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._/-]*)@([0-9a-f]{7,40}):([^:]+)", value)
    if not match:
        return None
    repository, revision, path = match.groups()
    relative_path(repository)
    relative_path(path)
    return {"repository": repository, "revision": revision, "path": path}


def _condition_value(value: Any) -> bool:
    if isinstance(value, (str, int, float, bool)):
        return not isinstance(value, str) or bool(value.strip())
    if isinstance(value, list):
        return bool(value) and all(isinstance(item, (str, int, float, bool)) and _condition_value(item) for item in value)
    if isinstance(value, dict):
        if "exact_set" in value:
            members = value["exact_set"]
            return (set(value) == {"exact_set"} and isinstance(members, list) and bool(members)
                    and all(_condition_value(item) for item in members)
                    and len({canonical(item) for item in members}) == len(members))
        return bool(value) and all(isinstance(key, str) and key and _condition_value(item) for key, item in value.items())
    return False


# Source admission and legacy-state upgrades parse frontmatter. Use PyYAML's
# installed safe backend with the same strict mapping constructor below.
class _UniqueLoader(yaml.CSafeLoader if yaml.__with_libyaml__ else yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError("frontmatter keys must be unique strings")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


@dataclass(frozen=True)
class GradedUnit:
    metadata: dict
    body: str
    path: str = ""

    @property
    def id(self) -> str:
        return self.metadata["id"]

    @property
    def content_sha256(self) -> str:
        # Location-only moves retain the observed claim revision and references.
        return semantic_digest(self.metadata, self.body)

    @property
    def revision(self) -> int:
        return self.metadata["revision"]

    @classmethod
    def from_parts(cls, frontmatter: dict, body: str, path: str = "") -> "GradedUnit":
        if not isinstance(frontmatter, dict):
            raise ValueError("graded frontmatter must be an object")
        metadata = json.loads(canonical(frontmatter))
        if metadata.get("schema") != SCHEMA:
            raise ValueError(f"expected {SCHEMA}")
        if not isinstance(metadata.get("id"), str) or not UNIT_ID.fullmatch(metadata["id"]):
            raise ValueError("stable id must use unit- followed by lowercase words")
        tier, category = metadata.get("tier"), metadata.get("category")
        if category not in TIER_CATEGORIES.get(tier, set()):
            raise ValueError("tier/category do not describe the same use")
        strength = metadata.get("evidence_strength")
        if strength not in STRENGTHS or (tier == "L1" and strength not in {"hardware_spec", "reproduced_empirical"}):
            raise ValueError("L1 requires hardware_spec or reproduced_empirical evidence; tier is not confidence")
        for key in ("title", "domain_path"):
            if not isinstance(metadata.get(key), str) or not metadata[key].strip():
                raise ValueError(f"{key} is required")
        relative_path(metadata["domain_path"])
        if path:
            relative_path(path)
        parents = _strings(metadata.get("inherits_from"), "inherits_from")
        if any(not UNIT_ID.fullmatch(parent) for parent in parents) or len(parents) != len(set(parents)):
            raise ValueError("inherits_from requires unique stable unit IDs")
        conditions = metadata.get("conditions")
        if not isinstance(conditions, dict) or not _condition_value(conditions):
            raise ValueError("conditions must explicitly declare applicability")
        triggers = metadata.get("triggers")
        if not isinstance(triggers, dict):
            raise ValueError("triggers must name error_signatures and keywords")
        for key in ("error_signatures", "keywords"):
            _strings(triggers.get(key), f"triggers.{key}")
        provenance = metadata.get("provenance")
        if not isinstance(provenance, dict):
            raise ValueError("provenance is required")
        for key in ("source_type", "source_locator"):
            if not isinstance(provenance.get(key), str) or not provenance[key].strip():
                raise ValueError(f"provenance.{key} is required")
        if not isinstance(provenance.get("source_digest"), str) or not SHA256.fullmatch(provenance["source_digest"]):
            raise ValueError("provenance.source_digest requires an exact SHA256")
        _strings(provenance.get("evidence_refs"), "provenance.evidence_refs", nonempty=strength == "reproduced_empirical" or tier == "L4")
        _strings(provenance.get("supersedes", []), "provenance.supersedes")
        if strength in {"hardware_spec", "reproduced_empirical"} and provenance.get("experiment_status") in {"compile_failed", "infra_failed", "measurement_invalid", "in_flight"}:
            raise ValueError("failed or incomplete execution cannot establish hardware or reproduced evidence")
        if not isinstance(body, str) or not body.strip():
            raise ValueError("a graded unit requires a nonempty rule body")
        revision = metadata.get("revision", 1)
        if type(revision) is not int or revision < 1:
            raise ValueError("revision must be a positive integer")
        body = body.strip() + "\n"
        metadata["revision"] = revision
        # Proposal producers do not calculate cryptographic digests. Persisted
        # Markdown is checked separately by from_text before it is admitted.
        metadata["content_digest"] = "sha256:" + semantic_digest(metadata, body)
        return cls(metadata, body, path)

    @classmethod
    def from_text(cls, text: str, path: str = "") -> "GradedUnit":
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            raise ValueError("graded unit requires YAML frontmatter")
        end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        if end is None:
            raise ValueError("unterminated graded frontmatter")
        try:
            metadata = yaml.load("\n".join(lines[1:end]), Loader=_UniqueLoader)
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid graded frontmatter: {exc}") from exc
        unit = cls.from_parts(metadata, "\n".join(lines[end + 1:]), path)
        if "revision" not in metadata or metadata.get("content_digest") != unit.metadata["content_digest"]:
            raise ValueError("persisted C5 unit requires revision and exact content_digest")
        return unit

    def render(self) -> str:
        return "---\n" + yaml.safe_dump(self.metadata, allow_unicode=True, sort_keys=False) + "---\n" + self.body
