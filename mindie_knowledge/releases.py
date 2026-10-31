"""Validate public release declarations and select without I/O or installation.

[Role] Validate public release declarations and select a compatible release without I/O.
[Invariants] Selection uses the declared harness range and immutable source identity; a manifest claim is never accepted as download proof.
[Timeouts & Locks] Pure version arithmetic; no network, installer or lock wait occurs in this module.
[Side-Effect Policy] Returns selected release facts only; installation, download and token use belong to the native installer.

Callers fetch Release facts/metadata from their fixed official repository and
verify the selected Git object before invoking the native installer. A manifest
claim is not proof of its download origin or of the bytes at its source SHA.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re

from semantic_version import Version


RELEASE_SCHEMA = "mindie-plugin-release/1"
COMPATIBILITY_SCHEMA = "mindie-harness-compatibility/1"
REPOSITORIES = {
    "codex": "mindie-agent/mindie-agent-codex",
    "claude-code": "mindie-agent/mindie-agent-cc",
    "kimi-code": "mindie-agent/mindie-agent-kimi",
    "dsh": "mindie-agent/mindie-agent-dsh",
    "antigravity": "mindie-agent/mindie-agent-agy",
}
_ID = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")


class ReleaseValidationError(ValueError):
    """Static failure code; never echo arbitrary catalog text to a caller."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise ReleaseValidationError(code)


def _object(value, keys, code):
    if not isinstance(value, dict) or set(value) != set(keys):
        _fail(code)


def _version(value, code):
    if not isinstance(value, str) or len(value) > 128:
        _fail(code)
    try:
        return Version(value)
    except ValueError:
        _fail(code)


def _strings(value, code, *, identifiers=False):
    if (not isinstance(value, list) or not 1 <= len(value) <= 128
            or any(not isinstance(item, str) or not 1 <= len(item) <= 1024
                   or any(ord(char) < 32 for char in item) for item in value)
            or len(set(value)) != len(value)):
        _fail(code)
    if identifiers and any(not _ID.fullmatch(item) for item in value):
        _fail(code)


@dataclass(frozen=True)
class _Interval:
    minimum: Version
    maximum: Version
    maximum_inclusive: bool
    include_prerelease: bool

    def matches(self, version):
        if version.prerelease and not self.include_prerelease:
            return False
        return (version >= self.minimum and
                (version < self.maximum or
                 self.maximum_inclusive and version <= self.maximum))

    def covers(self, other):
        return (
            self.minimum <= other.minimum
            and (self.maximum > other.maximum or
                 self.maximum == other.maximum
                 and (self.maximum_inclusive or not other.maximum_inclusive))
            and (self.include_prerelease or not other.include_prerelease)
        )


def _intervals(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        _fail("invalid_supported_intervals")
    result = []
    for item in value:
        _object(item, ("minimum", "maximum", "maximum_inclusive", "include_prerelease"),
                "invalid_supported_interval")
        minimum = _version(item["minimum"], "invalid_interval_version")
        maximum = _version(item["maximum"], "invalid_interval_version")
        inclusive, prerelease = item["maximum_inclusive"], item["include_prerelease"]
        if (type(inclusive) is not bool or type(prerelease) is not bool
                or minimum.build or maximum.build
                or minimum > maximum or minimum == maximum and not inclusive
                or (minimum.prerelease or maximum.prerelease) and not prerelease):
            _fail("invalid_supported_interval")
        interval = _Interval(minimum, maximum, inclusive, prerelease)
        if interval in result:
            _fail("duplicate_supported_interval")
        result.append(interval)
    return result


def validate_compatibility(value, *, harness_name):
    """Validate a source declaration and each used interface's support envelope.

    Each release interval must fit inside one interval of every used interface.
    Split declarations at interface boundaries instead of relying on general
    range-expression/SAT reasoning. Single-version closed intervals are valid.
    """
    if not isinstance(harness_name, str) or harness_name not in REPOSITORIES:
        _fail("unknown_harness")
    _object(value, ("schema", "harness", "supported", "used_interfaces", "interface_registry"),
            "invalid_compatibility_metadata")
    if value["schema"] != COMPATIBILITY_SCHEMA or value["harness"] != harness_name:
        _fail("compatibility_identity_mismatch")
    supported = _intervals(value["supported"])
    _strings(value["used_interfaces"], "invalid_used_interfaces", identifiers=True)
    registry = value["interface_registry"]
    if (not isinstance(registry, dict) or not 1 <= len(registry) <= 128
            or any(not isinstance(key, str) or not _ID.fullmatch(key) for key in registry)):
        _fail("invalid_interface_registry")
    declared = {}
    for ident, entry in registry.items():
        _object(entry, ("supported", "code_paths", "test_ids", "evidence_refs"),
                "invalid_interface_entry")
        declared[ident] = _intervals(entry["supported"])
        for field in ("code_paths", "test_ids", "evidence_refs"):
            _strings(entry[field], "invalid_interface_" + field)
    for ident in value["used_interfaces"]:
        if ident not in declared:
            _fail("unregistered_interface")
        if any(not any(bound.covers(interval) for bound in declared[ident])
               for interval in supported):
            _fail("interface_support_does_not_cover_release")
    return deepcopy(value)


def validate_release_metadata(value, *, repository, harness_name):
    """Validate the official Release asset; no network provenance is inferred."""
    if not isinstance(harness_name, str) or harness_name not in REPOSITORIES:
        _fail("unknown_harness")
    if repository != REPOSITORIES[harness_name]:
        _fail("untrusted_repository")
    _object(value, ("schema", "version", "source", "compatibility"),
            "invalid_release_metadata")
    if value["schema"] != RELEASE_SCHEMA:
        _fail("invalid_release_schema")
    _version(value["version"], "invalid_release_version")
    source = value["source"]
    _object(source, ("repository", "sha"), "invalid_release_source")
    if source["repository"] != repository:
        _fail("release_repository_mismatch")
    if not isinstance(source["sha"], str) or not _SHA.fullmatch(source["sha"]):
        _fail("invalid_source_sha")
    validate_compatibility(value["compatibility"], harness_name=harness_name)
    return deepcopy(value)


def supports_harness(compatibility, *, harness_name, harness_version):
    """Check one declared Harness version without inventing a Release record."""
    validated = validate_compatibility(compatibility, harness_name=harness_name)
    version = _version(harness_version, "invalid_harness_version")
    return any(interval.matches(version) for interval in _intervals(validated["supported"]))


def select_release(releases, *, repository, harness_name, harness_version,
                   installed_version=None):
    """Return highest compatible stable Release, never downgrade or mutate.

    Inputs are normalized official Release records with id, tag_name, draft,
    prerelease and metadata. The transport owns authentication, complete
    pagination and metadata-asset identity. Draft/prerelease Release records
    need no metadata. Explicit metadata=None means no declaration was published,
    so that release is ineligible; a damaged declaration fails the selection.
    """
    if not isinstance(harness_name, str) or harness_name not in REPOSITORIES:
        _fail("unknown_harness")
    if repository != REPOSITORIES[harness_name]:
        _fail("untrusted_repository")
    harness = _version(harness_version, "invalid_harness_version")
    installed = (_version(installed_version, "invalid_installed_version")
                 if installed_version is not None else None)
    if not isinstance(releases, list) or len(releases) > 10000:
        _fail("invalid_release_catalog")
    eligible, versions, ids, undeclared = [], set(), set(), 0
    for record in releases:
        if not isinstance(record, dict):
            _fail("invalid_release_record")
        if (type(record.get("id")) is not int or record["id"] <= 0
                or type(record.get("draft")) is not bool
                or type(record.get("prerelease")) is not bool):
            _fail("invalid_release_record")
        if record["id"] in ids:
            _fail("duplicate_release_id")
        ids.add(record["id"])
        if record["draft"] or record["prerelease"]:
            continue
        _object(record, ("id", "tag_name", "draft", "prerelease", "metadata"),
                "invalid_release_record")
        if record["metadata"] is None:
            undeclared += 1
            continue
        metadata = validate_release_metadata(record["metadata"], repository=repository,
                                             harness_name=harness_name)
        version = _version(metadata["version"], "invalid_release_version")
        if record["tag_name"] not in (str(version), "v" + str(version)):
            _fail("release_tag_version_mismatch")
        if version.prerelease:
            continue
        # SemVer build metadata is not precedence. Equal-precedence releases
        # cannot silently pick different immutable source objects.
        precedence = (version.major, version.minor, version.patch)
        if precedence in versions:
            _fail("ambiguous_release_version")
        versions.add(precedence)
        if any(interval.matches(harness)
               for interval in _intervals(metadata["compatibility"]["supported"])):
            eligible.append((version, record))
    if not eligible:
        return dict(status="no_compatible_release", candidate=None,
                    skipped_undeclared_releases=undeclared)
    version, selected = max(eligible, key=lambda item: item[0])
    if installed is not None and version <= installed:
        return dict(status="up_to_date", candidate=None,
                    reason="installed_at_least_selected",
                    selected_version=str(version), skipped_undeclared_releases=undeclared)
    return dict(status="candidate", candidate=deepcopy(selected),
                skipped_undeclared_releases=undeclared)
