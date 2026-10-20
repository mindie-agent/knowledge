"""[Role] Read one exact publication-contract/2 Git snapshot as ordinary graded Markdown.

[Invariants] The contract/2 tree is authoritative only through parsed C5 units.
Derived routing files are validated data, never executable authority. All paths
are normalized repository-relative paths; a unit path is exactly
``<domain_path>/<unit_id>.md``. A complete inherited graph, every stable ID, the
persisted revision and the persisted content digest are verified before any
consumer install. Missing blobs are an unavailable read, never a deletion.
[Timeouts & Locks] Callers supply the deadline and cancellation token. Reads use
one bounded ``git cat-file --batch`` process; no SQLite transaction or model is
opened here.
[Side-Effect Policy] Reads Git objects and writes exact candidate bytes only in
the caller's private staging directory. It never writes a store, repository,
network endpoint or executable file.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import re
import time
from pathlib import Path, PurePosixPath

from ..gitbin import resolve_git
from ..gitread import CatFileBatch, _check_cancel, iter_file_records, run_stdout_to_file
from ..loop.documents import MAX_FILE_BYTES
from .index import MAX_UNIT_BYTES, RoutingIndex
from .units import GradedUnit, is_graded_unit_path, relative_path

ROUTING_MANIFEST_PATH = "index/routing-manifest.json"
SHARD_TOPOLOGY_PATH = "index/shard-topology.json"
ROUTING_MANIFEST_SCHEMA = "mindie-routing-manifest/1"
SHARD_TOPOLOGY_SCHEMA = "mindie-shard-topology/1"
DERIVED_PATHS = {ROUTING_MANIFEST_PATH, SHARD_TOPOLOGY_PATH}
FEEDBACK_RE = re.compile(r"feedback/[^/]+\.json\Z")
METADATA_FILES = {
    "README.md", "README", "AGENTS.md", "LICENSE", "LICENSE.md", "NOTICE",
    "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md", "CHANGELOG.md",
}
METADATA_PREFIXES = ("docs/", ".github/")


@dataclass(frozen=True)
class TreeRecord:
    path: str
    mode: str
    kind: str
    object_id: str
    size: int


@dataclass(frozen=True)
class GradedSnapshot:
    units: tuple[GradedUnit, ...]
    files: dict[str, str]
    feedback: tuple[dict, ...]
    feedback_files: int
    manifest: dict | None
    topology: dict | None


def _metadata_path(path: str) -> bool:
    # Root-level Markdown is documentation: a graded unit path always contains
    # its <domain_path>/<unit_id>.md directory component.
    if path.endswith(".md") and "/" not in path:
        return True
    return path in METADATA_FILES or path.startswith(METADATA_PREFIXES)


def classify_path(path: str) -> str | None:
    """Classify one contract/2 path; reject the retired task layout explicitly."""
    if path.startswith("tasks/"):
        raise ValueError(
            f"{path}: publication-contract/2 does not admit the legacy tasks/ layout"
        )
    if path in DERIVED_PATHS:
        return "derived"
    if FEEDBACK_RE.fullmatch(path):
        return "feedback"
    if _metadata_path(path):
        return "metadata"
    if path.endswith(".md"):
        return "unit"
    return None


def _safe(path: str) -> str:
    relative_path(path)
    if any(part in {"", ".", ".."} for part in PurePosixPath(path).parts):
        raise ValueError(f"{path}: unsafe semantic path")
    return path



def _matches_blob(raw: bytes, size: int, object_id: str) -> bool:
    if len(raw) != size:
        return False
    hashing = hashlib.sha1 if len(object_id) == 40 else hashlib.sha256
    return hashing(b"blob " + str(size).encode("ascii") + b"\0" + raw).hexdigest() == object_id


def _parse_record(raw: bytes, prefix: str) -> tuple[str, TreeRecord] | None:
    try:
        head, path = raw.decode("utf-8", "strict").split("\t", 1)
        mode, kind, object_id, size = head.split()
    except (UnicodeError, ValueError) as exc:
        raise ValueError("unparseable git tree listing") from exc
    if prefix:
        if not path.startswith(prefix + "/"):
            return None
        path = path[len(prefix) + 1:]
    if not path or not size.isdigit():
        raise ValueError("git tree entry without a safe path or size")
    return path, TreeRecord(_safe(path), mode, kind, object_id, int(size))


def _scan_tree(repo, revision, prefix, listing_path, *, deadline, env, cancel):
    remaining = None if deadline is None else max(0, deadline - time.monotonic())
    try:
        run_stdout_to_file(
            [resolve_git(), "-C", str(repo), "ls-tree", "-r", "-z", "--long", revision,
             "--", prefix or "."],
            listing_path, timeout=remaining, env=env, cancel=cancel,
        )
        records = []
        with closing(iter_file_records(listing_path)) as stream:
            for raw in stream:
                _check_cancel(cancel)
                if not raw:
                    continue
                parsed = _parse_record(raw, prefix)
                if parsed is None:
                    continue
                path, record = parsed
                kind = classify_path(path)
                if kind in (None, "metadata"):
                    continue
                if kind == "unit" and not is_graded_unit_path(path):
                    raise ValueError(f"{path}: unsafe or noncanonical graded unit path")
                if record.mode != "100644" or record.kind != "blob":
                    raise ValueError(f"{path}: publication requires a plain non-executable data blob")
                maximum = MAX_UNIT_BYTES if kind == "unit" else MAX_FILE_BYTES
                if record.size > maximum:
                    raise ValueError(f"{path}: exceeds the per-file platform envelope")
                records.append((kind, record))
        return records
    finally:
        try:
            listing_path.unlink()
        except OSError:
            pass


def _read_staged(reader, staging, record, repo, env, *, deadline, cancel):
    target = staging.joinpath(*record.path.split("/"))
    if target.is_symlink():
        raise ValueError(f"{record.path}: feed staging contains an unsafe symlink")
    if target.is_file():
        raw = target.read_bytes()
        if _matches_blob(raw, record.size, record.object_id):
            return raw, reader
    if reader is None:
        reader = CatFileBatch(repo, env=env)
    maximum = MAX_UNIT_BYTES if classify_path(record.path) == "unit" else MAX_FILE_BYTES
    raw = reader.read(record.object_id, deadline=deadline, max_bytes=maximum, cancel=cancel)
    if raw is None:
        raise OSError(f"{record.path}: listed Git blob is unavailable")
    if not _matches_blob(raw, record.size, record.object_id):
        raise ValueError(f"{record.path}: Git blob identity mismatch")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(target)
    return raw, reader


def _unit(raw: bytes, path: str) -> GradedUnit:
    if len(raw) > MAX_UNIT_BYTES:
        raise ValueError(f"{path}: graded unit exceeds {MAX_UNIT_BYTES} bytes")
    text = raw.decode("utf-8", "strict")
    if "\r" in text or "\x00" in text:
        raise ValueError(f"{path}: canonical UTF-8/LF bytes required")
    if not is_graded_unit_path(path):
        raise ValueError(f"{path}: unsafe or noncanonical graded unit path")
    unit = GradedUnit.from_text(text, path)
    expected = f"{unit.metadata['domain_path']}/{unit.id}.md"
    if path != expected:
        raise ValueError(f"{path}: unit path must be exactly {expected}")
    return unit


def _feedback_votes(raw: bytes, path: str) -> list[dict]:
    # The feedback worker owns the schema and validator. Import it lazily so a
    # contract/2 corpus without feedback remains readable before it lands.
    try:
        from .feedback import validate_feedback
    except ImportError as exc:
        raise ValueError(
            f"{path}: graded feedback validation is unavailable; "
            "mindie_knowledge.graded.feedback is required"
        ) from exc
    text = raw.decode("utf-8", "strict")
    if "\r" in text or "\x00" in text:
        raise ValueError(f"{path}: canonical UTF-8/LF bytes required")
    checked = validate_feedback(text, path)
    votes = checked.get("votes") if isinstance(checked, dict) else checked
    if not isinstance(votes, list) or any(not isinstance(vote, dict) for vote in votes):
        raise ValueError(f"{path}: feedback validator did not return a vote list")
    return votes


def _json(raw: bytes, path: str) -> dict:
    text = raw.decode("utf-8", "strict")
    if "\r" in text or "\x00" in text:
        raise ValueError(f"{path}: canonical UTF-8/LF bytes required")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{path}: duplicate derived JSON field {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=unique)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: derived data is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path}: derived data must be an object")
    return value


def _unit_map(units) -> dict[str, GradedUnit]:
    return {unit.id: unit for unit in units}


def _require_schema(value: dict, path: str, field: str, supported: str) -> None:
    declared = value.get(field)
    if declared != supported:
        raise ValueError(f"{path}: unsupported derived schema {declared!r}")


def _validate_manifest(value: dict, units: dict[str, GradedUnit], index: RoutingIndex) -> None:
    path = ROUTING_MANIFEST_PATH
    if "schema_version" in value:
        _require_schema(value, path, "schema_version", ROUTING_MANIFEST_SCHEMA)
    if "schema" in value:
        _require_schema(value, path, "schema", ROUTING_MANIFEST_SCHEMA)
    if "schema_version" not in value and "schema" not in value:
        raise ValueError(f"{path}: derived schema is missing")
    if "unit_count" in value and (type(value["unit_count"]) is not int or value["unit_count"] != len(units)):
        raise ValueError(f"{path}: unit_count differs from the actual units")
    if "generation" in value and value["generation"] != index.generation:
        raise ValueError(f"{path}: generation differs from the actual units")
    if "id_path" in value:
        expected = {ident: unit.path or f"{unit.metadata['domain_path']}/{ident}.md"
                    for ident, unit in units.items()}
        if value["id_path"] != expected:
            raise ValueError(f"{path}: id_path differs from the actual units")
    if "closure" in value:
        expected = {ident: list(parents) for ident, parents in index.closure.items()}
        if value["closure"] != expected:
            raise ValueError(f"{path}: closure differs from the actual units")
    rows = value.get("units")
    if rows is None:
        return
    if not isinstance(rows, list):
        raise ValueError(f"{path}: units must be a list")
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{path}: every manifest unit row must be an object")
        ident = row.get("id")
        if not isinstance(ident, str) or ident not in units or ident in seen:
            raise ValueError(f"{path}: manifest unit identity differs from the actual units")
        seen.add(ident)
        unit = units[ident]
        expected_path = unit.path or f"{unit.metadata['domain_path']}/{ident}.md"
        checks = {"path": expected_path, "revision": unit.revision}
        for field, expected in checks.items():
            if field in row and row[field] != expected:
                raise ValueError(f"{path}: manifest {field} differs from the actual unit {ident}")
    if seen != set(units):
        raise ValueError(f"{path}: manifest does not cover exactly the actual units")


def _validate_topology(value: dict, units: dict[str, GradedUnit], index: RoutingIndex,
                       manifest: dict | None) -> None:
    path = SHARD_TOPOLOGY_PATH
    _require_schema(value, path, "schema", SHARD_TOPOLOGY_SCHEMA)
    if "unit_count" in value and (type(value["unit_count"]) is not int or value["unit_count"] != len(units)):
        raise ValueError(f"{path}: unit_count differs from the actual units")
    if "closure" in value:
        expected = {ident: list(parents) for ident, parents in index.closure.items()}
        if value["closure"] != expected:
            raise ValueError(f"{path}: closure differs from the actual units")
    rows = value.get("units")
    if rows is None:
        return
    if not isinstance(rows, dict) or set(rows) != set(units):
        raise ValueError(f"{path}: topology unit identities differ from the actual units")
    for ident, row in rows.items():
        if not isinstance(row, dict):
            raise ValueError(f"{path}: topology unit rows must be objects")
        unit = units[ident]
        expected_path = unit.path or f"{unit.metadata['domain_path']}/{ident}.md"
        if "path" in row and row["path"] != expected_path:
            raise ValueError(f"{path}: topology path differs from the actual unit {ident}")


def validate_derived(manifest: dict | None, topology: dict | None,
                     units: dict[str, GradedUnit], index: RoutingIndex) -> None:
    """Validate supported derived data against the authoritative units."""
    if manifest is not None:
        _validate_manifest(manifest, units, index)
    if topology is not None:
        _validate_topology(topology, units, index, manifest)


def load_snapshot(repo, revision, *, prefix="", staging, env=None, deadline=None, cancel=None) -> GradedSnapshot:
    """Read a complete contract/2 tree; reuse exact staged blobs when present."""
    staging = Path(staging)
    staging.mkdir(parents=True, exist_ok=True)
    listing_path = staging / "listing.tmp"
    records = _scan_tree(repo, revision, prefix, listing_path, deadline=deadline, env=env, cancel=cancel)
    units = []
    files = {}
    feedback = []
    feedback_files = 0
    manifest = topology = None
    reader = None
    try:
        for kind, record in records:
            _check_cancel(cancel)
            raw, reader = _read_staged(reader, staging, record, repo, env,
                                       deadline=deadline, cancel=cancel)
            files[record.path] = hashlib.sha256(raw).hexdigest()
            if kind == "unit":
                unit = _unit(raw, record.path)
                units.append(unit)
            elif kind == "feedback":
                feedback.extend(_feedback_votes(raw, record.path))
                feedback_files += 1
            elif kind == "derived":
                value = _json(raw, record.path)
                if record.path == ROUTING_MANIFEST_PATH:
                    manifest = value
                else:
                    topology = value
    finally:
        if reader is not None:
            reader.close()
    index = RoutingIndex(units)
    unit_map = _unit_map(units)
    validate_derived(manifest, topology, unit_map, index)
    from .feedback import validate_bindings
    validate_bindings(feedback, unit_map)
    return GradedSnapshot(tuple(units), files, tuple(feedback), feedback_files, manifest, topology)


def install_snapshot(store, snapshot: GradedSnapshot, *, feed_ident, source_revision):
    """Use the coordinator's single GradedRepository consumer API for contract/2."""
    installer = getattr(getattr(store, "graded", None), "install_feed", None)
    if installer is None:
        raise OSError(
            "graded feed installer is unavailable: GradedRepository.install_feed "
            "is required for publication-contract/2"
        )
    installed = installer(
        snapshot.units, feed_ident=feed_ident, source_revision=source_revision,
        files=dict(snapshot.files), feedback=list(snapshot.feedback),
    )
    if not isinstance(installed, dict) or type(installed.get("entries")) is not int:
        raise ValueError("graded feed installer returned an invalid receipt")
    return installed
