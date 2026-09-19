"""Historical Markdown/sidecar/feed/Skill-reference → current domain Store.

Independent of the ordinary MCP surface. Does not publish, does not scrape
private task directories, and does not talk to GitHub or a model.

    python -m mindie_knowledge.content_migration plan|status|apply|undo ...

``apply`` is a dry-run unless ``--commit`` is passed. Source files are never
modified. Plan and dry-run inspect an existing ledger read-only. Each committed
write is reserved on disk before mutation; undo refuses entries whose
publication, use, feedback, feed membership, or source attribution changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

SCHEMA = "mindie-content-migration/1"
DEFAULT_DOMAIN = "vllm-ascend"
MAX_FILES = 500
MAX_FILE_BYTES = 1_048_576
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_SCAN_VISITS = 4000
SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".mindie-local",
    "node_modules",
    "migration-jobs",
}
SECRET_KEY = re.compile(
    r"(password|secret|token|api[_-]?key|credential|private[_-]?key|authorization)",
    re.I,
)
HEADING = re.compile(r"^#\s+(.+?)\s*$", re.M)
SECTION = re.compile(r"^##\s+(.+?)\s*$", re.M)
LIST_ITEM = re.compile(r"^-\s+([^:\n]+):\s*(.*)$")
NESTED_SOURCE = re.compile(r"^-\s+source:\s+([^:\n]+):\s*(.*)$")
GITHUB_BLOB = re.compile(
    r"https://github\.com/([^/]+/[^/]+)/blob/([^/]+)/(\S+)",
)
OPERATIONAL_SKILL_NAMES = {
    "skill.md",
    "openai.yaml",
    "independent-maintenance.md",
}


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_json(path: Path, payload: Any) -> None:
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def posix(path: Path) -> str:
    return path.as_posix()


def domain_ok(domain: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9-]{0,63}", domain or ""))


def _read_bounded(path: Path, limit: int) -> bytes:
    if path.is_symlink():
        raise ValueError(f"symlink is not a migration source: {path}")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"file exceeds the {limit}-byte migration read budget: {path.name}")
    return raw


def _safe_relative(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    resolved.relative_to(root.resolve())
    for parent in (resolved, *resolved.parents):
        if parent == root.resolve():
            break
        if parent.is_symlink():
            raise ValueError("migration paths cannot traverse symlinks")
    return resolved


def looks_secret(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(SECRET_KEY.search(str(k)) or looks_secret(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(looks_secret(item) for item in value)
    return False


def stringify_conditions(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, str] = {}
    for key, item in value.items():
        name = str(key).strip()
        if not name:
            continue
        if item is None:
            continue
        if isinstance(item, str):
            text = item.strip()
            if not text or text.lower() == "unknown":
                continue
            out[name] = text
            continue
        if isinstance(item, (int, float, bool)):
            out[name] = str(item)
            continue
        encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if encoded and encoded.lower() != "unknown":
            out[name] = encoded
    return out


def parse_markdown(raw: str) -> tuple[str, str]:
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    match = HEADING.search(text)
    if match:
        title = match.group(1).strip()
        body = (text[: match.start()] + text[match.end() :]).strip()
        return title, body
    body = text.strip()
    title = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return title, body


def section_body(text: str, heading: str) -> str:
    normalized = (text or "").replace("\r\n", "\n")
    matches = list(SECTION.finditer(normalized))
    wanted = heading.strip().casefold()
    for index, match in enumerate(matches):
        if match.group(1).strip().casefold() == wanted:
            end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
            return normalized[match.end() : end].strip()
    return ""


def parse_list_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in (text or "").splitlines():
        nested = NESTED_SOURCE.match(line.strip())
        if nested:
            key, value = nested.group(1).strip(), nested.group(2).strip()
            if key and value:
                fields[f"source.{key}"] = value
            continue
        item = LIST_ITEM.match(line.strip())
        if not item:
            continue
        key, value = item.group(1).strip(), item.group(2).strip()
        if key and value:
            fields[key] = value
    return fields


def github_blob_url(repository: str, revision: str, relative: str) -> str:
    return (
        f"https://github.com/{repository}/blob/{revision}/"
        f"{quote(relative.lstrip('/'), safe='/')}"
    )


def classify(relative: str, sidecar: Mapping[str, Any] | None) -> str:
    posix_name = relative.replace("\\", "/")
    lower = posix_name.casefold()
    name = Path(posix_name).name.casefold()
    parts = lower.split("/")
    if "maintenance" in parts or posix_name.startswith("maintenance/"):
        return "skip_maintenance"
    if name in OPERATIONAL_SKILL_NAMES or name == "agents":
        return "skip_operational"
    if "skills" in parts and name in OPERATIONAL_SKILL_NAMES:
        return "skip_operational"
    if sidecar and sidecar.get("private") is True:
        return "skip_private"
    layer = str((sidecar or {}).get("layer") or "")
    if layer == "candidate":
        return "skip_private"
    declared = str((sidecar or {}).get("kind") or "").strip()
    if declared in {"knowledge", "experience"}:
        return declared
    if posix_name.startswith("topics/") or "/topics/" in f"/{posix_name}":
        return "knowledge"
    if posix_name.startswith("cases/") or "/cases/" in f"/{posix_name}":
        return "experience"
    if posix_name.startswith("corpus/") or "/references/" in f"/{posix_name}":
        return "knowledge"
    if posix_name.endswith(".entry.json"):
        return "reviewed"
    return "unknown"


def load_sidecar(markdown: Path, budget: int) -> tuple[dict[str, Any] | None, str | None, str | None]:
    sidecar = markdown.with_suffix(".meta.json")
    if not sidecar.is_file() or sidecar.is_symlink():
        return None, None, None
    try:
        raw = _read_bounded(sidecar, min(budget, 256 * 1024))
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return None, None, f"unreadable sidecar: {exc}"
    if not isinstance(payload, dict):
        return None, None, "sidecar is not a JSON object"
    return payload, sha256_bytes(raw), None


def extract_source(
    *,
    sidecar: Mapping[str, Any] | None,
    body: str,
    relative: str,
    origin_repository: str | None,
    source_revision: str | None,
    repo_relative: str | None,
) -> tuple[dict[str, Any], list[str]]:
    source: dict[str, Any] = {}
    notes: list[str] = []
    if sidecar and isinstance(sidecar.get("source"), Mapping):
        for key, value in sidecar["source"].items():
            if value is None or value == "":
                continue
            source[str(key)] = value
    fields = parse_list_fields(section_body(body, "Source") or body)
    if "url" in fields and "url" not in source:
        source["url"] = fields["url"]
    if "version" in fields and "revision" not in source:
        source["revision"] = fields["version"]
        notes.append("mapped Source.version to source.revision")
    if "revision" in fields and "revision" not in source:
        source["revision"] = fields["revision"]
    for key, value in fields.items():
        if key.startswith("source.") and key.split(".", 1)[1] not in source:
            source[key.split(".", 1)[1]] = value
    blob = GITHUB_BLOB.search(body or "")
    if blob and "url" not in source:
        source["url"] = blob.group(0).rstrip(").,]")
        if "revision" not in source:
            source["revision"] = blob.group(2)
            notes.append("mapped GitHub blob path to source.url/revision")
    if origin_repository and (source_revision or source.get("revision")):
        revision = str(source.get("revision") or source_revision)
        path = repo_relative or relative
        if "url" not in source and path:
            source["url"] = github_blob_url(origin_repository, revision, path)
            source.setdefault("revision", revision)
            notes.append("constructed GitHub blob URL from --origin-repository")
    elif source_revision and "revision" not in source:
        source["revision"] = source_revision
    if sidecar and sidecar.get("sha256") and "sha256" not in source:
        source["sha256"] = sidecar["sha256"]
    return source, notes


def extract_conditions(sidecar: Mapping[str, Any] | None, body: str) -> tuple[dict[str, str], dict[str, str], list[dict[str, str]]]:
    sidecar_conditions = stringify_conditions((sidecar or {}).get("conditions"))
    body_conditions = stringify_conditions(parse_list_fields(section_body(body, "Conditions")))
    if not body_conditions:
        source_fields = parse_list_fields(section_body(body, "Source"))
        inferred: dict[str, str] = {}
        for key in ("provider", "kind", "version", "date"):
            if source_fields.get(key):
                inferred[key] = source_fields[key]
        topics = re.search(r"^Topics:\s*(.+)$", body or "", re.M)
        if topics:
            inferred["topics"] = topics.group(1).strip()
        body_conditions = stringify_conditions(inferred)
    conflicts: list[dict[str, str]] = []
    for key in sorted(set(sidecar_conditions) & set(body_conditions)):
        if sidecar_conditions[key] != body_conditions[key]:
            conflicts.append(
                {
                    "field": f"conditions.{key}",
                    "sidecar": sidecar_conditions[key],
                    "body": body_conditions[key],
                }
            )
    merged = dict(body_conditions)
    merged.update(sidecar_conditions)
    return merged, body_conditions, conflicts


def unmapped_from_sidecar(sidecar: Mapping[str, Any] | None, kind: str) -> list[str]:
    if not sidecar:
        return []
    known = {
        "source",
        "conditions",
        "kind",
        "title",
        "slug",
        "layer",
        "uri",
        "private",
    }
    leftover = []
    for key in sidecar:
        if key in known:
            continue
        leftover.append(str(key))
    if sidecar.get("layer"):
        leftover.append("layer")
    if sidecar.get("uri"):
        leftover.append("legacy_ref")
    if kind == "knowledge" and sidecar.get("retrieval"):
        leftover.append("retrieval.aliases")
    return sorted(set(leftover))


def reviewed_payload(raw: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if not {"kind", "title", "content"} <= set(payload):
        return None
    return payload


def iter_sources(source_root: Path, *, max_files: int, max_file_bytes: int, max_total_bytes: int) -> list[dict[str, Any]]:
    root = source_root.resolve()
    if not root.is_dir():
        raise ValueError(f"source-root is not a directory: {source_root}")
    found: list[dict[str, Any]] = []
    total = 0
    visits = 0
    directories = 0
    for base, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            name
            for name in sorted(dirnames)
            if name not in SKIP_DIR_NAMES
            and not name.startswith(".")
            and not (Path(base) / name).is_symlink()
        ]
        directories += len(dirnames)
        if directories > max_files * 8:
            raise ValueError("source-root exceeds the directory scan budget")
        for name in sorted(filenames):
            visits += 1
            if visits > MAX_SCAN_VISITS:
                raise ValueError("source-root exceeds the file scan budget")
            path = Path(base) / name
            if path.is_symlink() or not path.is_file():
                continue
            if name.endswith(".meta.json") or name.startswith("."):
                continue
            if not (name.endswith(".md") or name.endswith(".entry.json")):
                continue
            relative = path.resolve().relative_to(root).as_posix()
            try:
                raw = _read_bounded(_safe_relative(root, path), max_file_bytes)
            except ValueError as exc:
                found.append(
                    {
                        "path": relative,
                        "action": "unmappable",
                        "reason": str(exc),
                    }
                )
                continue
            total += len(raw)
            if total > max_total_bytes:
                raise ValueError("source-root exceeds the total byte budget")
            if len(found) >= max_files:
                raise ValueError("source-root exceeds the document budget")
            found.append({"path": relative, "abs": path, "raw": raw})
    return found


def map_item(
    item: dict[str, Any],
    *,
    origin_repository: str | None,
    source_revision: str | None,
    repo_root: Path | None,
    include_candidates: bool,
    experience_fallback: bool,
) -> dict[str, Any]:
    relative = item["path"]
    if item.get("action") == "unmappable":
        return item
    raw: bytes = item["raw"]
    path: Path = item["abs"]
    source_hash = sha256_bytes(raw)
    report: dict[str, Any] = {
        "path": relative,
        "source_sha256": source_hash,
        "sidecar_sha256": None,
        "kind": None,
        "title": None,
        "action": "unmappable",
        "reason": "",
        "source": {},
        "conditions": {},
        "unmapped_fields": [],
        "conflicts": [],
        "notes": [],
        "legacy_ref": None,
        "content_id": None,
    }
    sidecar, sidecar_hash, sidecar_error = load_sidecar(path, MAX_FILE_BYTES)
    report["sidecar_sha256"] = sidecar_hash
    if sidecar_error:
        report["reason"] = sidecar_error
        return report
    if sidecar and looks_secret(sidecar):
        report["action"] = "skip_private"
        report["reason"] = "sidecar contains credential-like keys; not imported and not published"
        return report
    kind = classify(relative, sidecar)
    if kind == "skip_private" and include_candidates:
        kind = str((sidecar or {}).get("kind") or "unknown")
    if kind == "skip_maintenance":
        report["action"] = "skip_maintenance"
        report["kind"] = None
        report["reason"] = "maintenance diaries are operational records, not domain knowledge"
        return report
    if kind == "skip_operational":
        report["action"] = "skip_operational"
        report["reason"] = "Skill/operational documents are not domain entries"
        return report
    if kind == "skip_private":
        report["action"] = "skip_private"
        report["reason"] = "private/candidate notes are not imported or published"
        return report

    if relative.endswith(".entry.json") or kind == "reviewed":
        payload = reviewed_payload(raw)
        if payload is None:
            report["reason"] = "reviewed JSON is missing kind/title/content"
            return report
        if looks_secret(payload):
            report["action"] = "skip_private"
            report["reason"] = "reviewed JSON contains credential-like keys"
            return report
        report["kind"] = payload["kind"]
        report["title"] = str(payload["title"]).strip()
        report["source"] = dict(payload.get("source") or {})
        report["conditions"] = stringify_conditions(payload.get("conditions"))
        report["content"] = str(payload["content"])
        kind = payload["kind"]
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            report["reason"] = "source is not UTF-8 Markdown"
            return report
        title, body = parse_markdown(text)
        if sidecar and sidecar.get("title"):
            title = str(sidecar["title"]).strip()
        report["title"] = title
        report["content"] = body
        source, notes = extract_source(
            sidecar=sidecar,
            body=text,
            relative=relative,
            origin_repository=origin_repository,
            source_revision=source_revision,
            repo_relative=_repo_relative(path, repo_root),
        )
        conditions, _body_conditions, conflicts = extract_conditions(sidecar, text)
        report["source"] = source
        report["conditions"] = conditions
        report["conflicts"] = conflicts
        report["notes"] = notes
        report["unmapped_fields"] = unmapped_from_sidecar(sidecar, kind if kind != "unknown" else "knowledge")
        if sidecar and sidecar.get("uri"):
            report["legacy_ref"] = sidecar["uri"]
        if kind == "unknown":
            if source.get("url") and source.get("revision") and conditions:
                kind = "knowledge"
            elif experience_fallback or relative.startswith("cases/"):
                kind = "experience"
            else:
                report["reason"] = (
                    "no Store kind: knowledge needs source.url, source.revision and conditions; "
                    "not classified as experience without cases/ layout, sidecar kind, or --experience-fallback"
                )
                return report
        report["kind"] = kind

    if report["kind"] not in {"knowledge", "experience"}:
        report["reason"] = f"unsupported kind {report['kind']!r}"
        return report
    title = (report.get("title") or "").strip()
    content = (report.get("content") or "").strip()
    if not title or not content:
        report["reason"] = "title and nonempty body are required"
        return report
    if looks_secret(report.get("source")) or looks_secret(report.get("conditions")):
        report["action"] = "skip_private"
        report["reason"] = "source or conditions contain credential-like keys"
        return report
    if report["kind"] == "knowledge":
        source = report["source"] or {}
        if not source.get("url") or not source.get("revision") or not report["conditions"]:
            report["reason"] = (
                "knowledge requires source.url, source.revision and applicability conditions; "
                "left unmapped rather than coerced into experience"
            )
            return report
    from mindie_knowledge.loop.store import content_id

    report["content_id"] = content_id(
        report["kind"],
        title,
        content,
        report.get("source") or {},
        report.get("conditions") or {},
    )
    if report["conflicts"]:
        report["action"] = "conflict_retain"
        report["reason"] = (
            "sidecar and body conditions differ; both retained (sidecar keys win in Store.conditions, body stays in content)"
        )
        return report
    report["action"] = "add"
    report["reason"] = "mappable"
    return report


def _repo_relative(path: Path, repo_root: Path | None) -> str | None:
    if repo_root is None:
        return None
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return None


def job_id() -> str:
    return uuid.uuid4().hex


def job_dir(state_dir: Path, ident: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", ident):
        raise ValueError("invalid migration job reference")
    return Path(state_dir).resolve() / ident


def load_job(state_dir: Path, ident: str) -> dict[str, Any]:
    root = job_dir(state_dir, ident)
    record = read_json(root / "job.json")
    if not isinstance(record, dict) or record.get("schema") != SCHEMA or record.get("job") != ident:
        raise ValueError("migration job is missing or incompatible")
    return record


def save_job(record: dict[str, Any]) -> None:
    root = Path(record["root"])
    write_json(root / "job.json", record)


def store_baseline(store) -> str:
    from mindie_knowledge.loop.store import digest

    with store.lock:
        rows = store.db.execute("SELECT id, document FROM entries ORDER BY id").fetchall()
    return digest([[row[0], row[1]] for row in rows])


def inspect_store_readonly(store_root: Path, domain: str) -> dict[str, Any] | None:
    """Read an existing ledger without mkdir, chmod, WAL, or schema upgrade."""
    ledger = Path(store_root).expanduser().resolve() / domain / "ledger.sqlite3"
    if not ledger.is_file():
        return None
    uri = "file:" + ledger.as_posix() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise ValueError(f"cannot inspect existing Store read-only: {exc}") from exc
    try:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT id, document FROM entries ORDER BY id").fetchall()
        except sqlite3.Error as exc:
            raise ValueError(
                f"existing Store ledger is unreadable without creating or upgrading it: {exc}"
            ) from exc
        from mindie_knowledge.loop.store import digest

        payload = [[row["id"], row["document"]] for row in rows]
        return {
            "ids": {row["id"] for row in rows},
            "baseline": digest(payload),
            "documents": {row["id"]: row["document"] for row in rows},
        }
    finally:
        conn.close()


def annotate_against_ids(items: list[dict[str, Any]], present: set[str] | None) -> None:
    if not present:
        return
    for item in items:
        ident = item.get("content_id")
        if item.get("action") in {"add", "conflict_retain"} and ident in present:
            item["action"] = "duplicate_retain"
            item["reason"] = "identical Store identity already present; original entry retained"


def summarize(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        action = str(item.get("action") or "unmappable")
        counts[action] = counts.get(action, 0) + 1
    counts["total"] = len(items)
    return counts


def plan(
    *,
    source_root: Path,
    store_root: Path,
    domain: str,
    state_dir: Path,
    origin_repository: str | None = None,
    source_revision: str | None = None,
    repo_root: Path | None = None,
    include_candidates: bool = False,
    experience_fallback: bool = False,
    max_files: int = MAX_FILES,
) -> dict[str, Any]:
    if not domain_ok(domain):
        raise ValueError("invalid domain")
    source_root = Path(source_root).expanduser().resolve()
    store_root = Path(store_root).expanduser().resolve()
    state_dir = Path(state_dir).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    scanned = iter_sources(
        source_root,
        max_files=max_files,
        max_file_bytes=MAX_FILE_BYTES,
        max_total_bytes=MAX_TOTAL_BYTES,
    )
    items = [
        map_item(
            item,
            origin_repository=origin_repository,
            source_revision=source_revision,
            repo_root=Path(repo_root).resolve() if repo_root else None,
            include_candidates=include_candidates,
            experience_fallback=experience_fallback,
        )
        for item in scanned
    ]
    seen_ids: dict[str, str] = {}
    for item in items:
        ident = item.get("content_id")
        if not ident or item.get("action") not in {"add", "conflict_retain"}:
            continue
        if ident in seen_ids:
            item["action"] = "duplicate_retain"
            item["reason"] = f"identical identity already planned from {seen_ids[ident]}; original retained"
        else:
            seen_ids[ident] = item["path"]
    snapshot = inspect_store_readonly(store_root, domain)
    annotate_against_ids(items, snapshot["ids"] if snapshot else None)
    baseline = snapshot["baseline"] if snapshot else None
    ident = job_id()
    root = job_dir(state_dir, ident)
    root.mkdir(parents=True, exist_ok=False)
    record = {
        "schema": SCHEMA,
        "job": ident,
        "root": str(root),
        "status": "planned",
        "source_root": str(source_root),
        "store_root": str(store_root),
        "domain": domain,
        "origin_repository": origin_repository,
        "source_revision": source_revision,
        "repo_root": str(repo_root) if repo_root else None,
        "include_candidates": include_candidates,
        "experience_fallback": experience_fallback,
        "store_baseline": baseline,
        "applied_ids": [],
        "counts": summarize(items),
        "publish": False,
        "note": (
            "Default is inspection only. apply is dry-run unless --commit. "
            "Original files are never modified. Private notes are not published."
        ),
    }
    write_json(root / "plan.json", {"schema": SCHEMA, "job": ident, "items": items})
    save_job(record)
    return status_payload(record)


def status_payload(record: dict[str, Any]) -> dict[str, Any]:
    items = []
    plan_path = Path(record["root"]) / "plan.json"
    if plan_path.is_file():
        items = read_json(plan_path).get("items", [])
    payload = dict(record)
    payload["counts"] = summarize(items) if items else record.get("counts", {})
    payload["items"] = items
    apply_path = Path(record["root"]) / "apply.json"
    undo_path = Path(record["root"]) / "undo.json"
    if apply_path.is_file():
        payload["apply"] = read_json(apply_path)
    if undo_path.is_file():
        payload["undo"] = read_json(undo_path)
    return payload


def status(state_dir: Path, ident: str) -> dict[str, Any]:
    return status_payload(load_job(state_dir, ident))


def _current_source_hash(source_root: Path, relative: str) -> str | None:
    path = source_root / relative
    if not path.is_file():
        return None
    return sha256_bytes(path.read_bytes())


def _verify_sources(record: dict[str, Any], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source_root = Path(record["source_root"])
    problems = []
    for item in items:
        relative = item["path"]
        expected = item.get("source_sha256")
        if not expected:
            continue
        actual = _current_source_hash(source_root, relative)
        if actual is None:
            problems.append({"path": relative, "reason": "source file missing; original was not modified by this tool"})
            continue
        if actual != expected:
            problems.append({"path": relative, "reason": "source changed since plan; apply refused"})
        sidecar_expected = item.get("sidecar_sha256")
        sidecar = source_root / Path(relative).with_suffix(".meta.json")
        if sidecar_expected:
            if not sidecar.is_file():
                problems.append({"path": relative, "reason": "sidecar missing since plan; apply refused"})
            elif sha256_bytes(sidecar.read_bytes()) != sidecar_expected:
                problems.append({"path": relative, "reason": "sidecar changed since plan; apply refused"})
        elif sidecar.is_file():
            problems.append({"path": relative, "reason": "sidecar appeared since plan; apply refused"})
    return problems


def _entry_kwargs(item: dict[str, Any], producer: str) -> dict[str, Any]:
    return {
        "kind": item["kind"],
        "title": item["title"],
        "content": item["content"],
        "source": item.get("source") or {},
        "conditions": item.get("conditions") or {},
        "producers": [producer],
    }


def _progress_path(record: dict[str, Any]) -> Path:
    return Path(record["root"]) / "progress.json"


def _load_progress(record: dict[str, Any]) -> list[dict[str, Any]]:
    path = _progress_path(record)
    if not path.is_file():
        return []
    payload = read_json(path)
    items = payload.get("items") if isinstance(payload, dict) else payload
    return list(items or [])


def _save_progress(record: dict[str, Any], items: list[dict[str, Any]]) -> None:
    write_json(_progress_path(record), {"schema": SCHEMA, "job": record["job"], "items": items})


def _record_progress(record: dict[str, Any], items: list[dict[str, Any]], entry: dict[str, Any]) -> list[dict[str, Any]]:
    ident = entry.get("id")
    kept = [
        row
        for row in items
        if not (row.get("id") == ident and row.get("status") in {"reserved", entry.get("status")})
    ]
    kept.append(entry)
    _save_progress(record, kept)
    return kept


def _capture_ownership(store, ident: str) -> dict[str, Any] | None:
    from mindie_knowledge.loop.store import canonical

    try:
        doc = store.get(ident)
    except ValueError:
        return None
    with store.lock:
        published = (
            store.db.execute("SELECT 1 FROM publication WHERE entry_id=?", (ident,)).fetchone()
            is not None
        )
        uses = [
            row[0]
            for row in store.db.execute(
                "SELECT id FROM uses WHERE entry_id=? ORDER BY id", (ident,)
            )
        ]
        feedback = [
            row[0]
            for row in store.db.execute(
                "SELECT use_id FROM feedback WHERE entry_id=? ORDER BY use_id", (ident,)
            )
        ]
        feeds = [
            [row[0], row[1], int(row[2])]
            for row in store.db.execute(
                "SELECT feed, path, active FROM feed_entries WHERE entry_id=? ORDER BY feed, path",
                (ident,),
            )
        ]
    return {
        "document": canonical(doc),
        "published": published,
        "uses": uses,
        "feedback": feedback,
        "feeds": feeds,
    }


def _ownership_reasons(expected: Mapping[str, Any] | None, actual: Mapping[str, Any] | None) -> list[str]:
    if actual is None:
        return []
    if not expected:
        return ["missing apply-time ownership fingerprint; entry retained"]
    reasons = []
    if actual.get("document") != expected.get("document"):
        reasons.append("document, producers, or source attribution changed")
    if actual.get("published") != expected.get("published"):
        reasons.append("publication changed")
    if actual.get("uses") != expected.get("uses"):
        reasons.append("use records changed")
    if actual.get("feedback") != expected.get("feedback"):
        reasons.append("feedback changed")
    if actual.get("feeds") != expected.get("feeds"):
        reasons.append("feed membership changed")
    return reasons


def _remove_new_entry(store, ident: str, kind: str) -> None:
    with store.lock, store.db:
        store.db.execute("DELETE FROM entries WHERE id=?", (ident,))
        store.db.execute("DELETE FROM publication WHERE entry_id=?", (ident,))
    for name in (f"{ident}.md", f"{ident}.meta.json"):
        path = store.root / "content" / kind / name
        path.unlink(missing_ok=True)


def _written_rows(progress: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in progress if row.get("status") == "written" and row.get("id")]


def apply(state_dir: Path, ident: str, *, commit: bool = False) -> dict[str, Any]:
    record = load_job(state_dir, ident)
    if record["status"] in {"undone", "undo_partial"} and commit:
        raise ValueError("undone job cannot be re-applied; create a new plan")
    items = read_json(Path(record["root"]) / "plan.json")["items"]
    problems = _verify_sources(record, items)
    snapshot = inspect_store_readonly(Path(record["store_root"]), record["domain"])
    if snapshot is not None:
        annotate_against_ids(items, snapshot["ids"])
        if (
            record.get("store_baseline")
            and snapshot["baseline"] != record["store_baseline"]
            and record["status"] == "planned"
        ):
            problems.append(
                {
                    "path": ".",
                    "reason": "Store baseline changed since plan; re-run plan or refuse writes",
                }
            )
    if record["status"] == "applied":
        previous_apply = {}
        apply_path = Path(record["root"]) / "apply.json"
        if apply_path.is_file():
            previous_apply = read_json(apply_path)
        receipt = {
            **previous_apply,
            "schema": SCHEMA,
            "job": ident,
            "status": "refused" if problems else "unchanged",
            "commit": commit,
            "problems": problems,
            "note": "already applied; refusing to rewrite the job ledger",
        }
        write_json(apply_path, receipt)
        return {**status_payload(record), "apply": receipt}
    if problems:
        receipt = {
            "schema": SCHEMA,
            "job": ident,
            "status": "refused",
            "commit": commit,
            "problems": problems,
            "written": [],
        }
        write_json(Path(record["root"]) / "apply.json", receipt)
        if record["status"] not in {"applying", "apply_partial"}:
            record["status"] = "planned"
            save_job(record)
        return {**status_payload(record), "apply": receipt}
    progress = _load_progress(record)
    already = {
        row["id"]
        for row in progress
        if row.get("id") and row.get("status") in {"written", "skipped_existing"}
    }
    writable = [
        item
        for item in items
        if item.get("action") in {"add", "conflict_retain"}
        and item.get("content_id")
        and item["content_id"] not in already
    ]
    duplicates = [item for item in items if item.get("action") == "duplicate_retain"]
    receipt = {
        "schema": SCHEMA,
        "job": ident,
        "status": "dry-run" if not commit else "applied",
        "commit": commit,
        "problems": [],
        "would_write": [
            {"path": item["path"], "id": item["content_id"], "kind": item["kind"]}
            for item in writable
        ],
        "duplicate_retain": [item["content_id"] for item in duplicates],
        "already_written": sorted(already),
        "written": [],
    }
    if not commit:
        write_json(Path(record["root"]) / "apply.json", receipt)
        return {**status_payload(record), "apply": receipt}

    from mindie_knowledge.loop.store import Store, session_key

    store = Store(record["store_root"], record["domain"])
    producer = session_key("content-migration:" + ident)
    record["status"] = "applying"
    save_job(record)
    skipped_existing: list[str] = []
    try:
        for item in writable:
            ident_entry = item["content_id"]
            progress = _record_progress(
                record,
                progress,
                {"id": ident_entry, "kind": item["kind"], "path": item["path"], "status": "reserved"},
            )
            try:
                existing_doc = store.get(ident_entry)
            except ValueError:
                existing_doc = None
            if existing_doc is not None:
                ownership = _capture_ownership(store, ident_entry)
                progress = _record_progress(
                    record,
                    progress,
                    {
                        "id": ident_entry,
                        "kind": item["kind"],
                        "path": item["path"],
                        "status": "skipped_existing",
                        "ownership": ownership,
                    },
                )
                skipped_existing.append(ident_entry)
                continue
            doc = store.add(**_entry_kwargs(item, producer))
            if doc["id"] != ident_entry:
                raise ValueError("Store identity diverged from planned content_id")
            ownership = _capture_ownership(store, doc["id"])
            progress = _record_progress(
                record,
                progress,
                {
                    "id": doc["id"],
                    "kind": doc["kind"],
                    "path": item["path"],
                    "status": "written",
                    "ownership": ownership,
                },
            )
        written = _written_rows(progress)
        receipt["written"] = [
            {"id": row["id"], "kind": row["kind"], "path": row.get("path"), "replaced": False}
            for row in written
        ]
        receipt["skipped_existing"] = skipped_existing
        receipt["status"] = "applied"
        receipt["store_baseline_after"] = store_baseline(store)
        write_json(Path(record["root"]) / "written.json", {"schema": SCHEMA, "job": ident, "written": written})
        write_json(Path(record["root"]) / "apply.json", receipt)
        record["status"] = "applied"
        record["applied_ids"] = [row["id"] for row in written]
        record["store_baseline_after"] = receipt["store_baseline_after"]
        save_job(record)
        return {**status_payload(record), "apply": receipt}
    except Exception:
        written = _written_rows(progress)
        record["status"] = "apply_partial"
        record["applied_ids"] = [row["id"] for row in written]
        save_job(record)
        write_json(
            Path(record["root"]) / "apply.json",
            {
                **receipt,
                "status": "apply_partial",
                "written": [
                    {"id": row["id"], "kind": row["kind"], "path": row.get("path")}
                    for row in written
                ],
                "note": "Durable per-item progress kept; concurrent entries were not deleted.",
            },
        )
        write_json(Path(record["root"]) / "written.json", {"schema": SCHEMA, "job": ident, "written": written})
        raise
    finally:
        store.close()


def undo(state_dir: Path, ident: str) -> dict[str, Any]:
    record = load_job(state_dir, ident)
    if record["status"] not in {"applied", "apply_partial", "applying"}:
        raise ValueError("only an applied or partially applied job can be undone")
    progress = _load_progress(record)
    written = _written_rows(progress)
    if not written:
        written_path = Path(record["root"]) / "written.json"
        if written_path.is_file():
            written = [
                row
                for row in read_json(written_path).get("written", [])
                if row.get("id")
            ]
    if not written:
        raise ValueError("applied job is missing its write ledger; originals were not touched")
    from mindie_knowledge.loop.store import Store

    store = Store(record["store_root"], record["domain"])
    restored, refused = [], []
    try:
        for row in reversed(written):
            ident_entry = row["id"]
            actual = _capture_ownership(store, ident_entry)
            if actual is None:
                restored.append({"id": ident_entry, "status": "already_absent"})
                continue
            reasons = _ownership_reasons(row.get("ownership"), actual)
            if reasons:
                refused.append({"id": ident_entry, "reason": "; ".join(reasons) + "; entry retained"})
                continue
            _remove_new_entry(store, ident_entry, row.get("kind") or "knowledge")
            restored.append({"id": ident_entry, "status": "removed"})
    finally:
        store.close()
    receipt = {
        "schema": SCHEMA,
        "job": ident,
        "status": "undone" if not refused else "undo_partial",
        "restored": restored,
        "refused": refused,
        "note": "Source Markdown/sidecars were not modified. Changed publication/use/feedback/feed/attribution is retained.",
    }
    write_json(Path(record["root"]) / "undo.json", receipt)
    record["status"] = receipt["status"]
    save_job(record)
    return {**status_payload(record), "undo": receipt}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mindie_knowledge.content_migration",
        description="Plan, inspect, apply (dry-run by default) or undo historical knowledge migration into the current Store.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan_p = sub.add_parser("plan", help="inventory sources and record a dry mapping (does not write the Store)")
    plan_p.add_argument("--source-root", required=True, type=Path)
    plan_p.add_argument("--store-root", required=True, type=Path)
    plan_p.add_argument("--state-dir", required=True, type=Path)
    plan_p.add_argument("--domain", default=DEFAULT_DOMAIN)
    plan_p.add_argument("--origin-repository", help="owner/name used only to construct public GitHub blob URLs")
    plan_p.add_argument("--source-revision", help="explicit source revision recorded on constructed URLs")
    plan_p.add_argument("--repo-root", type=Path, help="repository root for blob URL paths; never a user home scan")
    plan_p.add_argument("--include-candidates", action="store_true", help="opt-in; still never publishes")
    plan_p.add_argument("--experience-fallback", action="store_true")
    plan_p.add_argument("--max-files", type=int, default=MAX_FILES)

    for name, help_text in (
        ("status", "show plan/apply/undo state"),
        ("apply", "verify hashes and either dry-run (default) or --commit writes"),
        ("undo", "reverse this job's Store writes; never deletes source files"),
    ):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("--state-dir", required=True, type=Path)
        item.add_argument("--job", required=True)
        if name == "apply":
            item.add_argument(
                "--commit",
                action="store_true",
                help="actually write Store entries; omit for the default dry-run",
            )

    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            result = plan(
                source_root=args.source_root,
                store_root=args.store_root,
                domain=args.domain,
                state_dir=args.state_dir,
                origin_repository=args.origin_repository,
                source_revision=args.source_revision,
                repo_root=args.repo_root,
                include_candidates=args.include_candidates,
                experience_fallback=args.experience_fallback,
                max_files=args.max_files,
            )
        elif args.command == "status":
            result = status(args.state_dir, args.job)
        elif args.command == "apply":
            result = apply(args.state_dir, args.job, commit=args.commit)
        else:
            result = undo(args.state_dir, args.job)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("apply", {}).get("status") == "refused":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
