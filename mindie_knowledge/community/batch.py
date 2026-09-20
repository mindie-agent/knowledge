"""Strict validation of ``mindie-contribution/1`` batches and feedback files.

Core stages already-scanned outbound bytes; this module re-checks everything
at the trust boundary before any Git mutation: schema, path allowlist, per-file
sha256, the batch revision digest, vote shape, and the deterministic PR/commit
text templates (zero model involvement).
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from mindie_knowledge.redact import Allowlist, scan_text

from .common import (
    MAX_BATCH_BYTES,
    MAX_BATCH_FILES,
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

ALLOWED_PREFIXES = {"cases": ".md", "topics": ".md", "feedback": ".json"}
RATINGS = ("up", "down")


def check_path(path: Any) -> str:
    """Allow only data-file paths; reject traversal, dotfiles, workflows."""
    if not isinstance(path, str) or not path:
        raise CommunityError("file path must be nonempty text")
    pure = PurePosixPath(path)
    if pure.is_absolute() or str(path) != str(pure):
        raise CommunityError(f"file path {path!r} is not a clean relative POSIX path")
    parts = pure.parts
    if len(parts) != 2 or parts[0] not in ALLOWED_PREFIXES:
        raise CommunityError(
            f"file path {path!r} is outside the approved prefixes "
            "(cases/*.md, topics/*.md, feedback/*.json)"
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
    out = []
    for index, vote in enumerate(votes):
        checked = validate_vote(vote, index)
        if checked["vote_id"] in seen:
            raise CommunityError(f"{path}: duplicate vote_id {checked['vote_id']!r}")
        seen.add(checked["vote_id"])
        out.append(checked)
    unknown = set(data) - {"schema", "votes"}
    if unknown:
        raise CommunityError(f"{path}: unknown feedback fields: {sorted(unknown)}")
    return {"schema": SCHEMA_FEEDBACK, "votes": out}


def merge_votes(existing: Iterable[Mapping[str, Any]], new: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Append new authorized votes; a repeated vote_id updates, never doubles."""
    merged: dict[str, dict[str, Any]] = {}
    for vote in existing:
        merged[vote["vote_id"]] = dict(vote)
    for vote in new:
        merged[vote["vote_id"]] = dict(vote)
    return [merged[key] for key in sorted(merged)]


def render_feedback(votes: list[Mapping[str, Any]]) -> str:
    ordered = sorted((dict(v) for v in votes), key=lambda v: v["vote_id"])
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
                    {"path": f["path"], "sha256": f["sha256"], "base_sha256": f.get("base_sha256")}
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
    if not isinstance(entry_refs, list) or len(entry_refs) > MAX_BATCH_FILES:
        raise CommunityError("entry_refs must be a bounded list")
    entry_refs = [bounded_text(ref, "entry_refs[]", 256) for ref in entry_refs]
    summary = bounded_text(batch.get("summary"), "summary", MAX_TITLE)
    files = batch.get("files")
    if not isinstance(files, list) or not files:
        raise CommunityError("batch files must be a nonempty list")
    if len(files) > MAX_BATCH_FILES:
        raise CommunityError(f"batch holds more than {MAX_BATCH_FILES} files")

    total = 0
    seen_paths: set[str] = set()
    checked_files = []
    for index, item in enumerate(files):
        if not isinstance(item, Mapping):
            raise CommunityError(f"files[{index}] must be an object")
        unknown = set(item) - {"path", "content", "sha256", "base_sha256"}
        if unknown:
            raise CommunityError(f"files[{index}] has unknown fields: {sorted(unknown)}")
        path = check_path(item.get("path"))
        if path in seen_paths:
            raise CommunityError(f"duplicate path {path!r}")
        seen_paths.add(path)
        content = item.get("content")
        if not isinstance(content, str):
            raise CommunityError(f"{path}: content must be a UTF-8 string")
        raw = content.encode("utf-8")
        if len(raw) > MAX_FILE_BYTES:
            raise CommunityError(f"{path}: exceeds the {MAX_FILE_BYTES}-byte file limit")
        total += len(raw)
        if total > MAX_BATCH_BYTES:
            raise CommunityError("batch exceeds the total byte limit")
        sha = item.get("sha256")
        if not isinstance(sha, str) or sha != sha256_text(content):
            raise CommunityError(f"{path}: sha256 does not match content")
        base_sha = item.get("base_sha256")
        if base_sha is not None and (not isinstance(base_sha, str) or len(base_sha) != 64):
            raise CommunityError(f"{path}: base_sha256 must be null or a sha256 digest")
        if path.startswith("feedback/"):
            validate_feedback(content, path)
        checked_files.append(
            {"path": path, "content": content, "sha256": sha, "base_sha256": base_sha}
        )

    revision = batch.get("revision")
    expected = batch_revision(checked_files, domain, base_commit, entry_refs)
    if revision != expected:
        raise CommunityError("batch revision does not match the digest of files and metadata")
    has_entries = any(f["path"].startswith(("cases/", "topics/")) for f in checked_files)
    return {
        "batch_id": batch_id,
        "revision": revision,
        "domain": domain,
        "base_commit": base_commit,
        "entry_refs": entry_refs,
        "files": checked_files,
        "summary": summary,
        "votes_only": not has_entries,
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
        findings.extend(scan_text(item["content"], allow, path=item["path"]))
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
    for item in sorted(batch["files"], key=lambda f: f["path"]):
        change = "added" if item["base_sha256"] is None else "modified"
        lines.append(f"- `{item['path']}` ({change})")
    lines += ["", "Generated by the MindIE community contribution path; no model wrote this text."]
    return "\n".join(lines) + "\n"


def canonical_batch_files(batch: Mapping[str, Any]) -> str:
    return canonical([{k: f[k] for k in ("path", "sha256", "base_sha256")} for f in batch["files"]])
