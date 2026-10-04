"""Strict validation of ``mindie-contribution/1`` batches and feedback files.

Core stages already-scanned outbound bytes; this module re-checks everything
at the trust boundary before any Git mutation: schema, path allowlist, per-file
sha256, the batch revision digest, vote shape, and the deterministic PR/commit
text templates (zero model involvement).
"""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from mindie_knowledge.redact import Allowlist, scan_text
from mindie_knowledge.materials.file_source import FileContents, FileText

from .common import (
    MAX_BATCH_BYTES,
    MAX_DETAIL,
    MAX_FILE_BYTES,
    MAX_TITLE,
    MAX_VOTE_REASON,
    SCHEMA_BATCH,
    SCHEMA_FEEDBACK,
    CommunityError,
    bounded_text,
    canonical,
    digest,
    sha256_text,
)

ALLOWED_PREFIXES = {"feedback": ".json"}
RATINGS = ("up", "down")


def check_path(path: Any) -> str:
    """Allow only data-file paths; reject traversal, dotfiles, workflows."""
    if not isinstance(path, str) or not path:
        raise CommunityError("file path must be nonempty text")
    pure = PurePosixPath(path)
    if pure.is_absolute() or str(path) != str(pure):
        raise CommunityError(f"file path {path!r} is not a clean relative POSIX path")
    parts = pure.parts
    if re.fullmatch(r"tasks/[0-9a-f]{64}/(?:index\.md|blocks/[A-Za-z0-9_-]{1,128}\.md)", path):
        return path
    if len(parts) != 2 or parts[0] not in ALLOWED_PREFIXES:
        raise CommunityError(
            f"file path {path!r} is outside the approved prefixes "
            "(tasks/<id>/index.md, tasks/<id>/blocks/*.md, feedback/*.json)"
        )
    name = parts[1]
    if (
        not name
        or name.startswith(".")
        or name != PurePosixPath(name).name
        or not name.endswith(ALLOWED_PREFIXES[parts[0]])
    ):
        raise CommunityError(f"file path {path!r} has a disallowed name")
    if ".." in parts or any(part in (".git", ".github") for part in parts):
        raise CommunityError(f"file path {path!r} is disallowed")
    return str(pure)


def validate_vote(vote: Any, index: int) -> dict[str, Any]:
    if not isinstance(vote, Mapping):
        raise CommunityError(f"votes[{index}] must be an object")
    out: dict[str, Any] = {}
    for field in ("vote_id", "root_id", "entry_id", "revision"):
        out[field] = bounded_text(vote.get(field), f"votes[{index}].{field}", 256)
    if out["revision"] and len(out["revision"]) != 64:
        raise CommunityError(f"votes[{index}].revision must be a sha256 digest")
    rating = vote.get("rating")
    if rating not in RATINGS:
        raise CommunityError(f"votes[{index}].rating must be one of {RATINGS}")
    out["rating"] = rating
    reason = vote.get("reason", "")
    if reason is None:
        reason = ""
    if not isinstance(reason, str) or len(reason) > MAX_VOTE_REASON:
        raise CommunityError(f"votes[{index}].reason must be at most {MAX_VOTE_REASON} characters")
    # The optional reason stays untrusted free text: it is stored and republished
    # verbatim, scanned like everything else, and never interpreted.
    out["reason"] = reason.strip()
    unknown = set(vote) - {"vote_id", "root_id", "entry_id", "revision", "rating", "reason"}
    if unknown:
        raise CommunityError(f"votes[{index}] has unknown fields: {sorted(unknown)}")
    return out


def vote_key(vote: Mapping[str, Any]) -> tuple[str, str, str]:
    """One current vote per opaque root + entry + revision (contract note 19)."""
    return (vote["root_id"], vote["entry_id"], vote["revision"])


def validate_feedback(text: str, path: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CommunityError(f"{path}: not valid JSON: {exc}") from None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_FEEDBACK:
        raise CommunityError(f"{path} must declare schema {SCHEMA_FEEDBACK}")
    votes = data.get("votes")
    if not isinstance(votes, list) or len(votes) > 500:
        raise CommunityError(f"{path}: votes must be a list of at most 500")
    seen: set[str] = set()
    seen_keys: set[tuple[str, str, str]] = set()
    out = []
    for index, vote in enumerate(votes):
        checked = validate_vote(vote, index)
        if checked["vote_id"] in seen:
            raise CommunityError(f"{path}: duplicate vote_id {checked['vote_id']!r}")
        key = vote_key(checked)
        if key in seen_keys:
            raise CommunityError(
                f"{path}: duplicate vote for the same root/entry/revision"
            )
        seen.add(checked["vote_id"])
        seen_keys.add(key)
        out.append(checked)
    unknown = set(data) - {"schema", "votes"}
    if unknown:
        raise CommunityError(f"{path}: unknown feedback fields: {sorted(unknown)}")
    return {"schema": SCHEMA_FEEDBACK, "votes": out}


def merge_votes(existing: Iterable[Mapping[str, Any]], new: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Append new authorized votes; the same root/entry/revision updates in place.

    Keying on the (root_id, entry_id, revision) tuple — not only vote_id —
    means a revision change is preserved as a distinct vote while cross-file
    or cross-batch duplicates of the same vote can never multiply counts.
    """
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for vote in existing:
        merged[vote_key(vote)] = dict(vote)
    for vote in new:
        merged[vote_key(vote)] = dict(vote)
    return [merged[key] for key in sorted(merged)]


def render_feedback(votes: list[Mapping[str, Any]]) -> str:
    ordered = sorted((dict(v) for v in votes), key=vote_key)
    return json.dumps({"schema": SCHEMA_FEEDBACK, "votes": ordered}, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def batch_revision(files: list[Mapping[str, Any]], domain: str, base_commit: str | None, entry_refs: list[str]) -> str:
    """The candidate digest: sorted file identities plus batch metadata."""
    return digest(
        {
            "schema": SCHEMA_BATCH,
            "domain": domain,
            "base_commit": base_commit,
            "entry_refs": sorted(entry_refs),
            "files": sorted(
                (
                    {"path": f["path"], "sha256": f["sha256"], "base_sha256": f.get("base_sha256"), **({"delete": True} if f.get("delete") is True else {})}
                    for f in files
                ),
                key=lambda f: f["path"],
            ),
        }
    )


def validate_batch(batch: Any) -> dict[str, Any]:
    if not isinstance(batch, Mapping):
        raise CommunityError("batch must be an object")
    if batch.get("schema") != SCHEMA_BATCH:
        raise CommunityError(f"batch must declare schema {SCHEMA_BATCH}")
    batch_id = bounded_text(batch.get("batch_id"), "batch_id", 128)
    domain = bounded_text(batch.get("domain"), "domain", 64)
    base_commit = batch.get("base_commit")
    if base_commit is not None:
        base_commit = bounded_text(base_commit, "base_commit", 64)
    entry_refs = batch.get("entry_refs", [])
    if not isinstance(entry_refs, list):
        raise CommunityError("entry_refs must be a bounded list")
    entry_refs = [bounded_text(ref, "entry_refs[]", 256) for ref in entry_refs]
    summary = bounded_text(batch.get("summary"), "summary", MAX_TITLE)
    files = batch.get("files")
    if not isinstance(files, list) or not files:
        raise CommunityError("batch files must be a nonempty list")

    total = 0
    seen_paths: set[str] = set()
    checked_files = []
    package_ids = {f['path'].split('/')[1] for f in files
                   if isinstance(f, Mapping) and isinstance(f.get('path'), str) and f['path'].startswith('tasks/')}
    one_package = len(package_ids) == 1 and all(
        isinstance(f, Mapping) and isinstance(f.get('path'), str) and f['path'].startswith('tasks/') for f in files)
    for index, item in enumerate(files):
        if not isinstance(item, Mapping):
            raise CommunityError(f"files[{index}] must be an object")
        unknown = set(item) - {"path", "content", "sha256", "base_sha256", "delete"}
        if unknown:
            raise CommunityError(f"files[{index}] has unknown fields: {sorted(unknown)}")
        path = check_path(item.get("path"))
        if path in seen_paths:
            raise CommunityError(f"duplicate path {path!r}")
        seen_paths.add(path)
        content = item.get("content")
        if "delete" in item and item["delete"] is not True:
            raise CommunityError(f"{path}: delete must be true when present")
        if item.get("delete") is True:
            if not path.startswith("tasks/") or path.endswith("/index.md"):
                raise CommunityError("only superseded task block files can be deleted by a contribution")
            if content is not None or item.get("sha256") is not None:
                raise CommunityError(f"{path}: a deletion cannot contain replacement bytes")
            base_sha = item.get("base_sha256")
            if not isinstance(base_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", base_sha):
                raise CommunityError(f"{path}: deletion requires the exact previous file digest")
            checked_files.append(dict(path=path, content=None, sha256=None,
                                      base_sha256=base_sha, delete=True))
            continue
        if not isinstance(content, str):
            raise CommunityError(f"{path}: content must be a UTF-8 string")
        raw = content.encode("utf-8")
        if len(raw) > MAX_FILE_BYTES:
            raise CommunityError(f"{path}: exceeds the per-file platform envelope")
        total += len(raw)
        if total > MAX_BATCH_BYTES:
            # A complete task package is the indivisible publication unit.
            # The grouping budget may be exceeded by one platform-legal task.
            if not one_package:
                raise CommunityError("batch exceeds the per-flush grouping budget")
        sha = item.get("sha256")
        if not isinstance(sha, str) or sha != sha256_text(content):
            raise CommunityError(f"{path}: sha256 does not match content")
        base_sha = item.get("base_sha256")
        if base_sha is not None and (not isinstance(base_sha, str) or len(base_sha) != 64):
            raise CommunityError(f"{path}: base_sha256 must be null or a sha256 digest")
        if path.startswith("feedback/"):
            validate_feedback(content, path)
        checked_files.append(item.with_metadata(path=path, base_sha256=base_sha)
                             if isinstance(item, FileText) else
                             dict(path=path, content=content, sha256=sha, base_sha256=base_sha))

    revision = batch.get("revision")
    expected = batch_revision(checked_files, domain, base_commit, entry_refs)
    if revision != expected:
        raise CommunityError("batch revision does not match the digest of files and metadata")
    # Package writes include every current file together. Validate its structure
    # with the same parser the file authority and consumer use; never publish a
    # detached block or a navigation index referring to missing material.
    task_files = {}
    for item in checked_files:
        if item["path"].startswith("tasks/"):
            _, task_id, relative = item["path"].split("/", 2)
            task_files.setdefault(task_id, {})
            if item.get("delete") is not True:
                task_files[task_id][relative] = item
    task_revisions = {}
    if task_files:
        from mindie_knowledge.materials import validate_package_files
        for task_id, package_files in task_files.items():
            try:
                package = validate_package_files(FileContents(package_files), domain)
            except ValueError as exc:
                raise CommunityError(f"invalid task package {task_id}: {exc}") from exc
            if not package["ready"]:
                raise CommunityError("task package has unfinished material indexes")
            task_revisions[task_id] = package["revision"]
            expected_ref = f"mindie://{domain}/{task_id}@{package['revision']}"
            if expected_ref not in entry_refs:
                raise CommunityError("task package requires its exact revision reference")
            if package.get("task_id") != task_id:
                raise CommunityError("task package identity differs from its public path")
    has_entries = any(f["path"].startswith(("tasks/",)) for f in checked_files)
    return {
        "batch_id": batch_id,
        "revision": revision,
        "domain": domain,
        "base_commit": base_commit,
        "entry_refs": entry_refs,
        "files": checked_files,
        "summary": summary,
        "votes_only": not has_entries,
        "task_revisions": task_revisions,
        "explicit_retry": batch.get("explicit_retry") is True,
    }


def scan_outbound(batch: Mapping[str, Any], pr_title: str, pr_body: str, commit_message: str) -> None:
    """Re-scan every outbound byte at the boundary, PR/commit text included.

    Findings are reported masked (never the raw value) and fail the batch
    closed; suspected private content stays local.
    """
    allow = Allowlist()
    findings = []
    for item in batch["files"]:
        if item.get("delete") is True:
            continue
        findings.extend(scan_text(item["content"], allow, path=item["path"])[:5])
        if findings:
            break  # A known unsafe file blocks the entire write immediately.
    for label, text in (("pr-title", pr_title), ("pr-body", pr_body), ("commit-message", commit_message)):
        findings.extend(scan_text(text, allow, path=label))
    if findings:
        detail = "; ".join(f"{f.path}: [{f.rule}] {f.masked()}" for f in findings[:5])
        raise CommunityError(f"outbound redaction findings: {detail}"[:MAX_DETAIL])


def contribution_branch(domain: str, batch_id: str) -> str:
    safe_domain = "".join(c if c.isalnum() or c == "-" else "-" for c in domain.lower())[:32]
    safe_batch = "".join(c if c.isalnum() or c == "-" else "-" for c in batch_id.lower())[:48]
    return f"mindie-contrib/{safe_domain}/{safe_batch}"


def render_commit_message(batch: Mapping[str, Any]) -> str:
    kinds = sorted({f["path"].split("/", 1)[0] for f in batch["files"]})
    lines = [
        f"mindie: {batch['domain']} contribution {batch['revision'][:12]}",
        "",
        f"batch: {batch['batch_id']}",
        f"revision: {batch['revision']}",
        f"areas: {', '.join(kinds)}",
        f"files: {len(batch['files'])}",
    ]
    return "\n".join(lines) + "\n"


def render_pr_title(batch: Mapping[str, Any]) -> str:
    title = f"[mindie] {batch['domain']}: {batch['summary']}"
    return title[:MAX_TITLE]


def render_pr_body(batch: Mapping[str, Any]) -> str:
    """Deterministic template: batch identity plus the file list, no prose."""
    lines = [
        "Automated MindIE community contribution. Details are in the diff.",
        "",
        f"- batch: `{batch['batch_id']}`",
        f"- revision: `{batch['revision']}`",
        f"- domain: `{batch['domain']}`",
    ]
    if batch.get("base_commit"):
        lines.append(f"- base commit: `{batch['base_commit']}`")
    if batch["entry_refs"]:
        lines.append(f"- entry refs: {', '.join('`' + ref + '`' for ref in batch['entry_refs'][:20])}")
    lines += ["", "Files:"]
    ordered = sorted(batch['files'], key=lambda f: f['path'])
    for item in ordered[:40]:
        change = "deleted" if item.get("delete") is True else ("added" if item["base_sha256"] is None else "modified")
        lines.append(f"- `{item['path']}` ({change})")
    if len(ordered) > 40:
        lines.append(f'- {len(ordered) - 40} additional files are included in the complete Git diff.')
    lines += ["", "Generated by the MindIE community contribution path; no model wrote this text."]
    return "\n".join(lines) + "\n"


def canonical_batch_files(batch: Mapping[str, Any]) -> str:
    return canonical([{k: f[k] for k in ("path", "sha256", "base_sha256")} for f in batch["files"]])
