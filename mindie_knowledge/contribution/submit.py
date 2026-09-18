"""Idempotent local public-copy + fork branch/PR submit.

Capture must already have succeeded. Failures here leave a recoverable
pending record and never rewrite the candidate.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from mindie_knowledge.contribution.consent import Consent, ContributionPaused

from mindie_knowledge.contribution.documents import (
    DIGEST_PREFIX,
    MarkdownDocument,
    branch_for_digest,
    public_filename,
)
from mindie_knowledge.contribution.errors import DocumentRejected, TransportError
from mindie_knowledge.contribution.gitops import commit_public_file, run_git
from mindie_knowledge.contribution.github import GitHubError, GitHubTransport, create_pull
from mindie_knowledge.contribution.pending import (
    STATUS_AWAITING,
    STATUS_BLOCKED,
    STATUS_PR_OPEN,
    PendingRecord,
    load_pending,
    save_pending,
)
from mindie_knowledge.contribution.public import PublicCopy, prepare_public_copy


@dataclass(frozen=True)
class SubmitConfig:
    upstream: str
    fork: str
    default_branch: str = "main"
    knowledge_prefix: str = "corpus"
    push_remote: str | None = None


def prepare_candidate(
    candidate_path: Path,
    *,
    state_root: Path,
    public_root: Path,
    consent: Consent | None = None,
) -> PendingRecord:
    """Write a public copy and a pending record. Does not mutate ``candidate_path``."""

    original = Path(candidate_path).read_text(encoding="utf-8")
    existing_stat = Path(candidate_path).stat()
    copy = prepare_public_copy(original, public_root=public_root)
    after_stat = Path(candidate_path).stat()
    if (existing_stat.st_mtime_ns, existing_stat.st_size) != (after_stat.st_mtime_ns, after_stat.st_size):
        raise RuntimeError("candidate file was mutated while preparing a public copy")
    if Path(candidate_path).read_text(encoding="utf-8") != original:
        raise RuntimeError("candidate file was mutated while preparing a public copy")
    if copy.blocked or copy.path is None:
        try:
            parsed = MarkdownDocument.from_text(original)
            digest = parsed.digest
            title = parsed.title
        except DocumentRejected:
            digest = DIGEST_PREFIX + hashlib.sha256(original.encode("utf-8")).hexdigest()
            title = ""
        record = load_pending(state_root, digest) or PendingRecord(
            content_digest=digest,
            title=title,
            public_relpath="",
            status=STATUS_BLOCKED,
            candidate_relpath=str(candidate_path.name),
            consent_workspace_id=consent.workspace_id if consent else None,
            consent_revision=consent.revision if consent else None,
        )
        record.status = STATUS_BLOCKED
        record.last_error = copy.reason or "redaction blocked public copy"
        return save_pending(state_root, record)
    document = copy.document
    existing = load_pending(state_root, document.digest)
    if existing is not None and existing.status in {STATUS_PR_OPEN, STATUS_AWAITING, "submitted", "merged", "closed"}:
        return existing
    relpath = copy.path.name
    record = existing or PendingRecord(
        content_digest=document.digest,
        title=document.title,
        public_relpath=relpath,
        branch=branch_for_digest(document.digest),
        candidate_relpath=str(candidate_path.name),
        notes=["candidate left unchanged; public copy is redacted"],
        consent_workspace_id=consent.workspace_id if consent else None,
        consent_revision=consent.revision if consent else None,
    )
    record.title = document.title
    record.public_relpath = relpath
    record.branch = record.branch or branch_for_digest(document.digest)
    if record.status == STATUS_BLOCKED:
        record.status = "pending"
        record.last_error = None
    elif record.status not in {STATUS_PR_OPEN, STATUS_AWAITING, "submitted"}:
        record.status = "pending"
    return save_pending(state_root, record)


def _pr_body(record: PendingRecord) -> str:
    return (
        f"{record.title}\n\n"
        f"Content digest (idempotency only, not a Git identity): `{record.content_digest}`\n"
        f"Public review does not prove hardware facts.\n"
    )


def submit_pending(
    record: PendingRecord,
    *,
    state_root: Path,
    public_root: Path,
    git_repo: Path,
    github: GitHubTransport,
    config: SubmitConfig,
    authorize: Callable[[], bool] | None = None,
    git_env: Mapping[str, str] | None = None,
) -> PendingRecord:
    """Write the public copy onto a fork branch and open or reuse a PR."""

    def require_consent() -> None:
        if authorize is not None and not authorize():
            raise ContributionPaused("contribution authorization changed; pending content remains local")

    if authorize is not None and not authorize():
        return record

    if record.status == STATUS_BLOCKED:
        return record
    if record.status == STATUS_PR_OPEN and record.pr_number:
        return record
    public_file = Path(public_root) / record.public_relpath
    if not public_file.is_file():
        record.status = STATUS_AWAITING
        record.last_error = "public copy is missing"
        return save_pending(state_root, record)
    text = public_file.read_text(encoding="utf-8")
    public_copy = prepare_public_copy(text, public_root=public_root)
    if public_copy.blocked or public_copy.document.digest != record.content_digest:
        record.status = STATUS_BLOCKED
        record.last_error = "public copy changed or failed redaction; prepare the candidate again"
        return save_pending(state_root, record)
    repo_relpath = f"{config.knowledge_prefix.rstrip('/')}/{public_filename(record.content_digest, record.title)}"
    try:
        require_consent()
        if run_git(git_repo, ["status", "--porcelain"]).stdout.strip():
            raise TransportError("contribution checkout has uncommitted changes")
        start_ref = config.default_branch
        if config.push_remote:
            require_consent()
            run_git(git_repo, ["fetch", "upstream", config.default_branch], env=git_env)
            start_ref = "FETCH_HEAD"
        head = commit_public_file(
            git_repo,
            branch=record.branch or branch_for_digest(record.content_digest),
            relpath=repo_relpath,
            content=text,
            message=f"Contribute: {record.title}",
            start_ref=start_ref,
        )
        if config.push_remote:
            require_consent()
            run_git(git_repo, ["push", config.push_remote, f"HEAD:refs/heads/{record.branch}"], env=git_env)
        require_consent()
        pull = create_pull(
            github,
            upstream=config.upstream,
            fork=config.fork,
            branch=record.branch or branch_for_digest(record.content_digest),
            base=config.default_branch,
            title=record.title,
            body=_pr_body(record),
            authorize=authorize,
        )
    except ContributionPaused:
        return record
    except (TransportError, GitHubError, OSError) as exc:
        if isinstance(exc, GitHubError):
            from mindie_knowledge.contribution.github import transport_message

            record.last_error = transport_message(exc)
        else:
            record.last_error = str(exc)
        record.status = STATUS_AWAITING
        return save_pending(state_root, record)
    record.head_sha = head
    record.pr_number = int(pull["number"])
    html = pull.get("html_url")
    record.pr_url = html if isinstance(html, str) else None
    record.status = STATUS_PR_OPEN
    record.last_error = None
    return save_pending(state_root, record)


def after_capture(
    candidate_path: Path,
    *,
    state_root: Path,
    public_root: Path,
    git_repo: Path | None = None,
    github: GitHubTransport | None = None,
    config: SubmitConfig | None = None,
) -> dict[str, object]:
    """Non-blocking join point for local capture. Never fails the capture."""

    record = prepare_candidate(candidate_path, state_root=state_root, public_root=public_root)
    result: dict[str, object] = {"blocked_capture": False, "pending": record.to_dict()}
    if record.status == STATUS_BLOCKED:
        return result
    if git_repo is None or github is None or config is None:
        record.status = STATUS_AWAITING
        record.last_error = record.last_error or "submit transport not configured"
        save_pending(state_root, record)
        result["pending"] = record.to_dict()
        return result
    submitted = submit_pending(
        record,
        state_root=state_root,
        public_root=public_root,
        git_repo=git_repo,
        github=github,
        config=config,
    )
    result["pending"] = submitted.to_dict()
    return result


def prepare_public_copy_from_path(candidate_path: Path, public_root: Path) -> PublicCopy:
    original = Path(candidate_path).read_bytes()
    copy = prepare_public_copy(original.decode("utf-8"), public_root=public_root)
    if Path(candidate_path).read_bytes() != original:
        raise RuntimeError("candidate file was mutated while preparing a public copy")
    return copy
