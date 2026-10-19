"""[Role] Prepare a deterministic, inspectable local publication proposal from a C6 distillation decision.

[Invariants]
- A decision is untrusted data. Every proposed unit is revalidated as C5, its mirrored
  target fields and stable IDs are checked, and the complete proposed corpus must build
  a fresh RoutingIndex without duplicate IDs/paths or inheritance cycles.
- ``create`` never overwrites a current unit, ``merge_into_existing`` never rewrites a
  different source, and changed conditions or overlapping error fingerprints become a
  ``propose_semantic_merge_review`` item with no write.
- A merge write binds the target's current ``content_sha256`` for a later
  compare-and-swap. Original provenance evidence and exact source references survive.
- Local proposal authorization (``authorized_publication``) is not remote consent.
  ``ready_for_publication`` is always false; gitleaks review remains required.
- The deterministic redaction scanner runs over YAML metadata and Markdown bodies.
  Findings are reported as rule identifiers only; matched values never enter an error
  or the proposal.

[Timeouts & Locks]
- Pure in-memory preparation. No waits, locks, timers, model calls or external tools.

[Side-Effect Policy]
- Never reads or writes the filesystem and never opens a network connection. It only
  returns a proposal; applying it is a separate, explicitly authorized operation.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any

from ..redact import REDACTION_PROFILE, scan_text
from .index import RoutingIndex
from .units import GradedUnit, UNIT_ID, canonical

DECISION_SCHEMA = "mindie-distill-decision/1"
PROPOSAL_SCHEMA = "mindie-graded-publication-proposal/1"
RECEIPT_SCHEMA = "mindie-graded-publication-receipt/1"
ACTIONS = {"noop", "propose_graded_units"}
OPERATIONS = {"create", "merge_into_existing", "propose_semantic_merge_review"}
SESSION_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
STABLE_ID = re.compile(r"[a-z][a-z0-9]*(?:[-._:][a-z0-9]+)*\Z")
MAX_STABLE_ID = 256
MAX_PATH = 512
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}

LIMITATIONS = (
    "Schema validity does not establish semantic truth, completeness, or reproducibility.",
    "A local proposal is not publication approval; remote consent is not granted.",
    "Gitleaks review is required in addition to the deterministic regex privacy scan.",
    "Regex privacy scanning reports rule identifiers only and is not proof of de-identification.",
    "Evidence references are stable identifiers; their underlying sources were not re-verified.",
    "A merge proposal preserves existing provenance but does not prove the merged rule correct.",
)


class PublicationError(ValueError):
    """A C6 decision cannot be prepared as a safe local proposal."""

    code = "graded_publication_rejected"


class PrivacyRejected(PublicationError):
    """A proposed unit contains content the public scanner refuses to publish."""

    code = "graded_publication_privacy_rejected"

    def __init__(self, rule_ids):
        self.rule_ids = tuple(sorted(set(rule_ids)))
        super().__init__(
            "publication privacy scan rejected proposed units; rules: "
            + ", ".join(self.rule_ids)
        )


class PublicationProposal(dict):
    """A JSON-serializable mapping that also permits attribute inspection."""

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def to_dict(self) -> dict:
        return deepcopy(dict(self))

    def as_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _fail(message: str) -> None:
    raise PublicationError(message)


def _stable_id(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_STABLE_ID
        or not STABLE_ID.fullmatch(value)
    ):
        _fail(f"{field} must be an exact stable identifier")
    return value


def _safe_path(domain_path: Any, unit_id: str) -> str:
    if not isinstance(domain_path, str) or not domain_path:
        _fail("domain_path must be a normalized repository-relative path")
    if len(domain_path) > MAX_PATH or "\\" in domain_path:
        _fail("domain_path must be a normalized repository-relative path")
    if any(ord(character) < 32 or ord(character) == 127 for character in domain_path):
        _fail("domain_path contains control characters")
    pure = PurePosixPath(domain_path)
    if (
        pure.is_absolute()
        or pure.as_posix() != domain_path
        or any(part in {"", ".", ".."} for part in pure.parts)
        or any(part.startswith(".") for part in pure.parts)
    ):
        _fail("domain_path must be a normalized repository-relative path")
    for part in pure.parts:
        if part.endswith((" ", ".")) or part.lower() in _WINDOWS_RESERVED:
            _fail("domain_path is not portable across repositories")
    path = f"{domain_path}/{unit_id}.md"
    if len(path) > MAX_PATH:
        _fail("unit path is too long")
    if re.search(r'[<>:"|?*]', path):
        _fail("unit path contains characters unsafe in a repository")
    return path


def _effective_path(unit: GradedUnit) -> str:
    return unit.path or f"{unit.metadata['domain_path']}/{unit.id}.md"


def _unit_text(unit: GradedUnit) -> str:
    return unit.render()


def _render_sha256(unit: GradedUnit) -> str:
    return hashlib.sha256(_unit_text(unit).encode("utf-8")).hexdigest()


def _evidence_inputs(value: Any) -> list[str]:
    if value is None:
        _fail("evidence_inputs must be a list of stable identifiers")
    if not isinstance(value, list):
        _fail("evidence_inputs must be a list of stable identifiers")
    identifiers: list[str] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            identifier = item
        elif isinstance(item, dict) and isinstance(item.get("id"), str):
            identifier = item["id"]
        else:
            _fail("evidence_inputs entries must be stable identifiers")
        _stable_id(identifier, "evidence_inputs")
        if identifier in seen:
            _fail("evidence_inputs must not contain duplicate identifiers")
        seen.add(identifier)
        identifiers.append(identifier)
    return identifiers


def _frontmatter(entry: dict) -> dict:
    value = entry.get("frontmatter")
    if not isinstance(value, dict):
        _fail("unit frontmatter must be an object")
    return value


def _markdown_body(entry: dict) -> str:
    value = entry.get("markdown_body")
    if not isinstance(value, str) or not value.strip():
        _fail("unit markdown_body must be nonempty text")
    return value


def _mirror(entry: dict, frontmatter: dict) -> None:
    for decision_field, metadata_field in (
        ("target_tier", "tier"),
        ("category", "category"),
        ("evidence_strength", "evidence_strength"),
        ("domain_path", "domain_path"),
    ):
        value = entry.get(decision_field)
        if not isinstance(value, str) or not value:
            _fail(f"{decision_field} must mirror frontmatter.{metadata_field}")
        if value != frontmatter.get(metadata_field):
            _fail(f"{decision_field} must mirror frontmatter.{metadata_field}")
    target = entry.get("target_unit_id")
    if target not in (None, ""):
        _stable_id(target, "target_unit_id")
        if target != frontmatter.get("id"):
            _fail("target_unit_id must mirror frontmatter.id")


def _parse_unit(entry: dict, path: str) -> GradedUnit:
    try:
        unit = GradedUnit.from_parts(_frontmatter(entry), _markdown_body(entry), path)
    except (TypeError, ValueError) as exc:
        # C5 messages describe structural expectations and never echo a value.
        reason = str(exc)
        if len(reason) > 240 or not re.fullmatch(r"[A-Za-z0-9 .,;:()/'_+=>-]+", reason):
            reason = "structural validation failed"
        _fail(f"proposed unit failed C5 validation: {reason}")
    if not UNIT_ID.fullmatch(unit.id):
        _fail("proposed unit id is not an exact stable unit identifier")
    return unit


def _same_conditions(left: dict, right: dict) -> bool:
    return canonical(left) == canonical(right)


def _fingerprints(unit: GradedUnit) -> set[str]:
    values = unit.metadata["triggers"].get("error_signatures", [])
    return {value.strip().lower() for value in values if value.strip()}


def _source_identity(unit: GradedUnit) -> tuple[str, str, str]:
    provenance = unit.metadata["provenance"]
    return (
        provenance["source_type"],
        provenance["source_locator"],
        provenance["source_digest"],
    )


def _merged_provenance(target: GradedUnit, proposed: GradedUnit) -> dict:
    original = target.metadata["provenance"]
    incoming = proposed.metadata["provenance"]
    merged = deepcopy(original)
    for key, value in incoming.items():
        if key in {"source_type", "source_locator", "source_digest", "evidence_refs", "supersedes"}:
            continue
        merged[key] = deepcopy(value)
    merged["source_type"] = original["source_type"]
    merged["source_locator"] = original["source_locator"]
    merged["source_digest"] = original["source_digest"]
    merged["evidence_refs"] = list(dict.fromkeys([
        *original.get("evidence_refs", []),
        *incoming.get("evidence_refs", []),
    ]))
    supersedes = list(dict.fromkeys([
        *original.get("supersedes", []),
        *incoming.get("supersedes", []),
    ]))
    if supersedes:
        merged["supersedes"] = supersedes
    else:
        merged.pop("supersedes", None)
    return merged


def _merged_unit(target: GradedUnit, proposed: GradedUnit, path: str) -> GradedUnit:
    frontmatter = deepcopy(proposed.metadata)
    frontmatter["provenance"] = _merged_provenance(target, proposed)
    try:
        merged = GradedUnit.from_parts(frontmatter, proposed.body, path)
        revision = target.revision + (target.content_sha256 != merged.content_sha256)
        return GradedUnit.from_parts({**merged.metadata, "revision": revision}, merged.body, path)
    except (TypeError, ValueError):
        _fail("merged unit failed C5 validation after provenance preservation")


def _scan_proposed(units: list[GradedUnit], drop_reason: str) -> None:
    rules: set[str] = set()
    for unit in units:
        from .privacy import metadata_findings
        rules.update(finding.rule for finding in metadata_findings(unit.metadata, unit.path or unit.id))
        rules.update(finding.rule for finding in scan_text(unit.body, path=unit.path or unit.id))
    if drop_reason:
        rules.update(finding.rule for finding in scan_text(drop_reason, path="decision.drop_reason"))
    if rules:
        raise PrivacyRejected(rules)


def _routing_summary(index: RoutingIndex, proposed_ids: list[str]) -> dict:
    manifest = index.manifest()
    rows = {row["id"]: row for row in manifest["units"]}
    proposed = []
    for unit_id in sorted(set(proposed_ids)):
        row = rows[unit_id]
        proposed.append({
            "id": unit_id,
            "path": row["path"],
            "content_sha256": row["content_sha256"],
            "inherits_from_closure": list(index.closure[unit_id]),
        })
    return {
        "schema_version": "mindie-routing-manifest/1",
        "generation": manifest["generation"],
        "unit_count": manifest["unit_count"],
        "proposed": proposed,
    }


def _write_entry(operation: str, unit: GradedUnit, path: str,
                 expected_content_sha256: str | None) -> dict:
    content = _unit_text(unit)
    return {
        "operation": operation,
        "unit_id": unit.id,
        "path": path,
        "content": content,
        "content_sha256": unit.content_sha256,
        "render_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "expected_content_sha256": expected_content_sha256,
        "frontmatter": deepcopy(unit.metadata),
        "markdown_body": unit.body,
    }


def _unchanged_entry(operation: str, unit: GradedUnit, path: str) -> dict:
    return {
        "operation": operation,
        "unit_id": unit.id,
        "path": path,
        "content_sha256": unit.content_sha256,
        "render_sha256": _render_sha256(unit),
        "reason": "identical_current_bytes",
    }


def _review_entry(unit: GradedUnit, target: GradedUnit, path: str, reasons: list[str]) -> dict:
    content = _unit_text(unit)
    target_path = _effective_path(target)
    return {
        "operation": "propose_semantic_merge_review",
        "unit_id": unit.id,
        "target_unit_id": target.id,
        "path": path,
        "target_path": target_path,
        "reasons": list(reasons),
        "reason": "; ".join(reasons),
        "content": content,
        "content_sha256": unit.content_sha256,
        "render_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "expected_content_sha256": target.content_sha256,
        "frontmatter": deepcopy(unit.metadata),
        "markdown_body": unit.body,
    }


def _prepare_units(decision: dict, index: RoutingIndex) -> tuple[list, list, list, list]:
    writes: list[tuple[dict, GradedUnit]] = []
    reviews: list[dict] = []
    unchanged: list[dict] = []
    proposed_units: list[GradedUnit] = []
    seen_targets: set[str] = set()

    for entry in decision["units"]:
        if not isinstance(entry, dict):
            _fail("each decision unit must be an object")
        operation = entry.get("operation")
        if operation not in OPERATIONS:
            _fail("unit operation must be create, merge_into_existing, or propose_semantic_merge_review")
        frontmatter = _frontmatter(entry)
        _mirror(entry, frontmatter)
        unit_id = frontmatter.get("id")
        if not isinstance(unit_id, str) or not UNIT_ID.fullmatch(unit_id):
            _fail("unit frontmatter.id must be an exact stable unit identifier")
        path = _safe_path(frontmatter.get("domain_path"), unit_id)
        unit = _parse_unit(entry, path)

        target_id = entry.get("target_unit_id")
        if operation == "create":
            if target_id not in (None, "") and target_id != unit.id:
                _fail("create target_unit_id must equal frontmatter.id or be empty")
            if unit.id in seen_targets:
                _fail("decision proposes the same target unit more than once")
            seen_targets.add(unit.id)
            current = index.units.get(unit.id)
            if current is not None:
                if _effective_path(current) == path and current.content_sha256 == unit.content_sha256:
                    unchanged.append(_unchanged_entry(operation, current, path))
                    proposed_units.append(current)
                else:
                    _fail(f"create conflicts with existing stable unit ID {unit.id}")
                continue
            writes.append((_write_entry(operation, unit, path, None), unit))
            proposed_units.append(unit)
            continue

        if not isinstance(target_id, str) or not target_id:
            _fail(f"{operation} requires an existing target_unit_id")
        _stable_id(target_id, "target_unit_id")
        if target_id != unit.id:
            _fail("target_unit_id must equal frontmatter.id")
        if target_id in seen_targets:
            _fail("decision proposes the same target unit more than once")
        seen_targets.add(target_id)
        target = index.units.get(target_id)
        if target is None:
            _fail(f"target_unit_id {target_id} does not name a current C5 unit")

        if operation == "propose_semantic_merge_review":
            reviews.append(_review_entry(
                unit, target, path, ["decision requested semantic merge review"],
            ))
            proposed_units.append(unit)
            continue

        # An exact retry is unchanged even when the target now carries the same
        # error fingerprints as the retried proposal. Idempotency wins before the
        # semantic-review checks below.
        if _effective_path(target) == path and target.content_sha256 == unit.content_sha256:
            unchanged.append(_unchanged_entry(operation, target, path))
            proposed_units.append(target)
            continue

        for decision_field, metadata_field in (
            ("target_tier", "tier"),
            ("category", "category"),
            ("evidence_strength", "evidence_strength"),
        ):
            if unit.metadata[metadata_field] != target.metadata[metadata_field]:
                _fail(f"{decision_field} must match the existing C5 target unit")

        reasons = []
        if not _same_conditions(unit.metadata["conditions"], target.metadata["conditions"]):
            reasons.append("conditions differ from target")
        if canonical(unit.metadata["inherits_from"]) != canonical(target.metadata["inherits_from"]):
            reasons.append("inheritance differs from target")
        if _source_identity(unit) != _source_identity(target):
            reasons.append("provenance source differs from target")
        if reasons:
            reviews.append(_review_entry(unit, target, path, sorted(reasons)))
            proposed_units.append(unit)
            continue

        merged = _merged_unit(target, unit, path)
        if _effective_path(target) == path and _unit_text(target) == _unit_text(merged):
            unchanged.append(_unchanged_entry(operation, merged, path))
            proposed_units.append(merged)
            continue
        if _fingerprints(unit) & _fingerprints(target):
            reviews.append(_review_entry(unit, target, path, ["error fingerprints overlap target"]))
            proposed_units.append(unit)
            continue
        writes.append((_write_entry(operation, merged, path, target.content_sha256), merged))
        proposed_units.append(merged)

    return writes, reviews, unchanged, proposed_units


def prepare_decision(decision: dict, index: RoutingIndex, *,
                     authorized_publication: bool = False) -> PublicationProposal:
    """Prepare a local C6 publication proposal without writing anywhere.

    ``authorized_publication`` records local operator authorization only. It never
    sets ``ready_for_publication`` and never implies gitleaks or remote consent.
    """
    if type(authorized_publication) is not bool:
        _fail("authorized_publication must be a boolean")
    if not isinstance(decision, dict):
        _fail("a distillation decision must be an object")
    declared_schemas = [decision[key] for key in ("schema_version", "schema") if key in decision]
    if not declared_schemas or any(value != DECISION_SCHEMA for value in declared_schemas):
        _fail(f"decision schema must be {DECISION_SCHEMA}")
    action = decision.get("action")
    if action not in ACTIONS:
        _fail("decision action must be noop or propose_graded_units")
    session_digest = decision.get("session_digest")
    if not isinstance(session_digest, str) or not SESSION_DIGEST.fullmatch(session_digest):
        _fail("session_digest must be an exact lowercase SHA256")
    evidence_inputs = _evidence_inputs(decision.get("evidence_inputs"))
    drop_reason = decision.get("drop_reason", "")
    if drop_reason is None:
        drop_reason = ""
    if not isinstance(drop_reason, str):
        _fail("drop_reason must be text")
    units = decision.get("units")
    if not isinstance(units, list):
        _fail("decision units must be a list")
    if action == "noop":
        if units:
            _fail("noop action must not carry proposed units")
    elif not units:
        _fail("propose_graded_units requires at least one unit")

    if not hasattr(index, "units"):
        _fail("index must expose the current routing units")
    current_ids = list(index.units)
    if len(set(current_ids)) != len(current_ids):
        _fail("index contains duplicate stable unit IDs")

    writes, reviews, unchanged, proposed_units = _prepare_units(decision, index)
    _scan_proposed(proposed_units, drop_reason)

    candidate_units = dict(index.units)
    for entry, unit in writes:
        candidate_units[unit.id] = unit
    candidate_ids = [entry["unit_id"] for entry, _ in writes]
    candidate_ids.extend(item["unit_id"] for item in unchanged)
    try:
        candidate_index = RoutingIndex(list(candidate_units.values()))
    except (TypeError, ValueError, RecursionError) as exc:
        message = str(exc)
        if len(message) > 240 or not re.fullmatch(r"[A-Za-z0-9 .,;:()/'_+=>-]+", message):
            message = "routing graph validation failed"
        _fail(f"proposed units do not form a valid routing graph: {message}")

    routing = _routing_summary(candidate_index, candidate_ids)
    write_count = len(writes)
    review_count = len(reviews)
    unchanged_count = len(unchanged)
    if action == "noop":
        status = "noop"
    elif review_count:
        status = "needs_review"
    elif write_count:
        status = "proposed"
    elif unchanged_count:
        status = "unchanged"
    else:
        status = "noop"

    local_authorization = "authorized" if authorized_publication else "not_authorized"
    privacy = {
        "scanner": "mindie_knowledge.redact.scan_text",
        "profile": REDACTION_PROFILE,
        "status": "passed",
        "rule_ids": [],
        "surfaces": ["frontmatter", "markdown_body"],
        "gitleaks_review_required": True,
        "gitleaks_status": "required",
    }
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "status": status,
        "session_digest": session_digest,
        "write_count": write_count,
        "review_count": review_count,
        "unchanged_count": unchanged_count,
        "authorized_publication": authorized_publication,
        "remote_write": False,
        "ready_for_publication": False,
        "gitleaks_review": "required",
    }
    proposal = PublicationProposal({
        "schema": PROPOSAL_SCHEMA,
        "schema_version": PROPOSAL_SCHEMA,
        "decision_schema": DECISION_SCHEMA,
        "action": action,
        "session_digest": session_digest,
        "drop_reason": drop_reason,
        "evidence_inputs": evidence_inputs,
        "status": status,
        "authorized_publication": authorized_publication,
        "local_authorization": local_authorization,
        "remote_consent_granted": False,
        "ready_for_publication": False,
        "ready_for_review": bool(writes or reviews),
        "gitleaks_review_required": True,
        "gitleaks_review": "required",
        "writes": [entry for entry, _ in writes],
        "reviews": reviews,
        "unchanged": unchanged,
        "privacy": privacy,
        "review_requirements": [
            "gitleaks-review-required",
            "remote-consent-not-granted",
        ],
        "publication_review": {
            "gitleaks": "required",
            "remote_consent": "not_granted",
        },
        "limitations": list(LIMITATIONS),
        "evidence_limitations": list(LIMITATIONS),
        "routing": routing,
        "receipt": receipt,
    })
    return proposal
