"""Validated KnowledgeUnit records for the CANNBot/ACLGraph corpus.

[Role]
Define a dependency-free `mindie-graded-unit/1`-compatible record with tier,
category, authoring stack, graph-runtime applicability, conditions, inheritance,
evidence strength, provenance, body, and token accounting.
[Invariants]
Tiers are L1..L4. Categories are axiom|measurement|sop|workflow|mutator.
Authoring stacks are ascendc|cannbot_dsl|triton_ascend|framework_aclgraph.
Applicability is aclgraph_native, aclgraph_adapted_from_torchair,
operator_kernel_agnostic, or torchair_legacy_only. IDs are unique, parents
resolve, inheritance is acyclic, and no unit is a Harness SKILL.md.
token_count is a strict upper bound (one BPE token covers >= 1 UTF-8 byte).
Every live unit carries repo-relative path, full commit, and content hash.
[Timeouts & Locks]
Pure validation and bounded JSONL writes; no locks or waits.
[Side-Effect Policy]
Validation is read-only. `write_units` writes only the caller's units.jsonl.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

TIERS = ("L1", "L2", "L3", "L4")
CATEGORIES = ("axiom", "measurement", "sop", "workflow", "mutator")
AUTHORING_STACKS = ("ascendc", "cannbot_dsl", "triton_ascend", "framework_aclgraph")
APPLICABILITY = (
    "aclgraph_native",
    "aclgraph_adapted_from_torchair",
    "operator_kernel_agnostic",
    "torchair_legacy_only",
)
EVIDENCE = (
    "hardware_spec",
    "reproduced_empirical",
    "reference_doc",
    "derived",
)
_ID_RE = re.compile(r"^unit-[a-z0-9][a-z0-9-]{2,110}$")
_SHA1_HEX_RE = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}\Z")


class KnowledgeUnitError(ValueError):
    """A unit or unit set violates the graded-unit contract."""


@dataclass(frozen=True, slots=True)
class KnowledgeUnit:
    unit_id: str
    title: str
    tier: str
    category: str
    authoring_stack: str
    graph_runtime_applicability: str
    body: str
    conditions: Mapping[str, Any] = field(default_factory=dict)
    inherits_from: tuple[str, ...] = ()
    evidence_strength: str = "reference_doc"
    provenance: Mapping[str, str] = field(default_factory=dict)
    token_count: int = 0
    adaptation_notes: str = ""
    schema: str = "mindie-graded-unit/1"
    revision: int = 1
    status: str = "active"
    supersedes: tuple[str, ...] = ()
    domain_path: str = ""
    triggers: Mapping[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.unit_id

    @property
    def tokens(self) -> int:
        return self.token_count

    @property
    def content(self) -> str:
        return self.body

    @property
    def source_repo(self) -> str:
        return str(self.provenance.get("source_repo", ""))

    @property
    def source_commit(self) -> str:
        return str(self.provenance.get("source_commit", ""))

    @property
    def source_path(self) -> str:
        return str(self.provenance.get("source_path", ""))

    def __post_init__(self) -> None:
        if not _ID_RE.match(self.unit_id):
            raise KnowledgeUnitError(f"invalid unit id: {self.unit_id!r}")
        if not self.title.strip() or not self.body.strip():
            raise KnowledgeUnitError("title and body are required")
        if self.tier not in TIERS:
            raise KnowledgeUnitError(f"invalid tier: {self.tier!r}")
        if self.category not in CATEGORIES:
            raise KnowledgeUnitError(f"invalid category: {self.category!r}")
        if self.authoring_stack not in AUTHORING_STACKS:
            raise KnowledgeUnitError(f"invalid authoring_stack: {self.authoring_stack!r}")
        if self.graph_runtime_applicability not in APPLICABILITY:
            raise KnowledgeUnitError("invalid graph_runtime_applicability")
        if self.evidence_strength not in EVIDENCE:
            raise KnowledgeUnitError(f"invalid evidence_strength: {self.evidence_strength!r}")
        if not isinstance(self.conditions, Mapping):
            raise KnowledgeUnitError("conditions must be a mapping")
        if self.schema != "mindie-graded-unit/1":
            raise KnowledgeUnitError(f"unsupported schema: {self.schema!r}")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise KnowledgeUnitError("revision must be an integer >= 1")
        if self.status not in ("active", "draft", "superseded", "archived"):
            raise KnowledgeUnitError(f"invalid status: {self.status!r}")
        if not isinstance(self.supersedes, tuple):
            raise KnowledgeUnitError("supersedes must be a tuple")
        if not isinstance(self.triggers, Mapping):
            raise KnowledgeUnitError("triggers must be a mapping")
        if not isinstance(self.provenance, Mapping):
            raise KnowledgeUnitError("provenance mapping is required")
        for key in ("source_repo", "source_path", "source_commit"):
            value = self.provenance.get(key)
            if not isinstance(value, str) or not value.strip():
                raise KnowledgeUnitError(f"provenance.{key} is required")
        if not _SHA1_HEX_RE.fullmatch(self.source_commit):
            raise KnowledgeUnitError("provenance.source_commit must be a full 40-hex SHA")
        if Path(self.source_path).is_absolute():
            raise KnowledgeUnitError("provenance.source_path must be repo-relative")
        digest = self.provenance.get("content_sha256", "")
        if digest and not _SHA256_HEX_RE.fullmatch(str(digest)):
            raise KnowledgeUnitError("provenance.content_sha256 must be 64 lowercase hex")
        if ".." in str(self.provenance["source_path"]).split("/"):
            raise KnowledgeUnitError("provenance.source_path must be repo-relative")
        if len(self.inherits_from) != len(set(self.inherits_from)):
            raise KnowledgeUnitError("inherits_from contains duplicates")
        if self.unit_id in self.inherits_from:
            raise KnowledgeUnitError("unit may not inherit from itself")
        # A BPE token always covers at least one UTF-8 byte, so the byte length
        # is a strict upper bound for both cl100k_base and o200k_base.
        upper_bound = len(self.body.encode("utf-8"))
        if isinstance(self.token_count, bool) or not isinstance(self.token_count, int):
            raise KnowledgeUnitError("token_count must be an integer")
        if self.token_count == 0:
            object.__setattr__(self, "token_count", upper_bound)
        elif self.token_count < upper_bound:
            raise KnowledgeUnitError(
                f"token_count {self.token_count} is below the strict byte upper bound {upper_bound}"
            )

    def to_dict(self) -> dict[str, Any]:
        provenance = dict(sorted(self.provenance.items()))
        provenance.setdefault(
            "source_locator",
            f"{self.source_repo}@{self.source_commit}:{self.source_path}",
        )
        provenance.setdefault("source_digest", provenance.get("content_sha256", ""))
        return {
            "schema": self.schema,
            "id": self.unit_id,
            "unit_id": self.unit_id,
            "title": self.title,
            "tier": self.tier,
            "category": self.category,
            "authoring_stack": self.authoring_stack,
            "graph_runtime_applicability": self.graph_runtime_applicability,
            "body": self.body,
            "conditions": dict(sorted(self.conditions.items())),
            "inherits_from": list(self.inherits_from),
            "evidence_strength": self.evidence_strength,
            "provenance": provenance,
            "revision": self.revision,
            "status": self.status,
            "supersedes": list(self.supersedes),
            "domain_path": self.domain_path,
            "triggers": dict(sorted(self.triggers.items())),
            "token_count": self.token_count,
            "tokens": self.token_count,
            "adaptation_notes": self.adaptation_notes,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "KnowledgeUnit":
        known = {field.name for field in cls.__dataclass_fields__.values()}
        values = {key: value for key, value in payload.items() if key in known}
        values.setdefault("unit_id", payload.get("id", ""))
        values.setdefault("token_count", payload.get("tokens", 0))
        try:
            return cls(**values)
        except (TypeError, ValueError) as exc:
            raise KnowledgeUnitError(f"invalid unit payload: {exc}") from exc


def validate_unit(unit: KnowledgeUnit) -> KnowledgeUnit:
    """Return the unit after re-validating all invariants."""
    return KnowledgeUnit.from_dict(unit.to_dict())


def validate_units(units: Iterable[KnowledgeUnit]) -> tuple[KnowledgeUnit, ...]:
    """Validate uniqueness, parent closure, acyclicity, and stack coverage."""
    records = tuple(
        unit if isinstance(unit, KnowledgeUnit) else KnowledgeUnit.from_dict(unit)
        for unit in units
    )
    ids = [unit.unit_id for unit in records]
    if len(ids) != len(set(ids)):
        raise KnowledgeUnitError("duplicate unit ids")
    known = set(ids)
    for unit in records:
        missing = set(unit.inherits_from) - known
        if missing:
            raise KnowledgeUnitError(f"{unit.unit_id} has missing parents: {sorted(missing)}")
    parents = {unit.unit_id: unit.inherits_from for unit in records}
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> None:
        if node in done:
            return
        if node in visiting:
            raise KnowledgeUnitError(f"inheritance cycle at {node}")
        visiting.add(node)
        for parent in parents.get(node, ()):
            visit(parent)
        visiting.remove(node)
        done.add(node)

    for unit_id in ids:
        visit(unit_id)
    return records


def write_units(units: Iterable[KnowledgeUnit], directory: str | Path) -> Path:
    """Write validated units as JSONL; never emit a SKILL.md file."""
    records = validate_units(units)
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    target = root / "units.jsonl"
    lines = [json.dumps(unit.to_dict(), ensure_ascii=True, sort_keys=True) for unit in records]
    try:
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except TimeoutError:
        raise
    except OSError as exc:
        raise KnowledgeUnitError(f"cannot write units: {exc}") from exc
    return target


__all__ = [
    "APPLICABILITY",
    "AUTHORING_STACKS",
    "CATEGORIES",
    "EVIDENCE",
    "KnowledgeUnit",
    "KnowledgeUnitError",
    "TIERS",
    "validate_unit",
    "validate_units",
    "write_units",
]
